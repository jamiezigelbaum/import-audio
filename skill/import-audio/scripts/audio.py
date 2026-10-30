#!/usr/bin/env python3
"""Audio import operations for one executor machine; no agent-harness runtime dependency."""
import argparse
import contextlib
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib


class AudioError(Exception):
    pass


IMPORT_FIELDS = ["original_name", "bytes", "imported_at", "destination", "sha256"]
DISCARD_FIELDS = ["filename", "bytes", "date", "sha256"]
DEFAULT_EXTENSIONS = ["wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "aif", "aiff", "caf", "wma", "webm", "mp4", "mov"]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_directory(path):
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for created in reversed(missing):
        sync_directory(created.parent)


def config_home():
    return Path(os.environ.get("AUDIO_IMPORT_HOME", str(Path.home() / ".config/import-audio"))).expanduser()


def load_config():
    path = config_home() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise AudioError(f"Invalid config {path}: {exc}")


def setting(config, env, key, default):
    """Environment variable, then config.toml, then the built-in default."""
    if env in os.environ:
        return os.environ[env]
    section, _, name = key.rpartition(".")
    table = config.get(section, {}) if section else config
    return table.get(name, default)


class Audio:
    def __init__(self, config=None):
        config = load_config() if config is None else config
        self.config = config
        self.state = Path(setting(config, "AUDIO_STATE_DIR", "state_dir",
                                  str(Path.home() / ".local/state/import-audio"))).expanduser()
        self.staging = Path(setting(config, "AUDIO_STAGING_DIR", "staging_dir", str(self.state / "audio-staging"))).expanduser()
        self.ledger = Path(os.environ.get("AUDIO_IMPORT_LEDGER", str(self.state / "audio-import-ledger.tsv")))
        self.discards = Path(os.environ.get("AUDIO_DISCARD_LEDGER", str(self.state / "audio-discard-ledger.tsv")))
        # Sources are folders to import from: a recorder's USB volume, a phone-sync folder, an inbox.
        sources = setting(config, "AUDIO_SOURCES", "sources", [])
        self.sources = [Path(v.strip()).expanduser() for v in
                        (sources.split(os.pathsep) if isinstance(sources, str) else sources) if str(v).strip()]
        extensions = setting(config, "AUDIO_EXTENSIONS", "extensions", DEFAULT_EXTENSIONS)
        self.extensions = tuple("." + e.strip().lower().lstrip(".") for e in
                                (extensions.split(",") if isinstance(extensions, str) else extensions) if e.strip())
        # Some recorders cut long takes at a fixed length (the DJI Mic 3 at 1800 s); 0 turns the flag off.
        self.split_seconds = float(setting(config, "AUDIO_SPLIT_SECONDS", "split_seconds", 0))

    @contextlib.contextmanager
    def lock(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state / "audio-import.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AudioError("Audio executor is busy; retry after the current command finishes.")
            yield

    @staticmethod
    def signature(stat):
        # No ctime: Dropbox's File Provider updates it (attributes, xattrs) during and after a
        # copy without changing the bytes. The signature only catches changes while hashing;
        # clear() rehashes the destination before deleting, because a same-size rewrite that
        # restores mtime keeps this signature.
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    @staticmethod
    def digest_snapshot(path):
        if path.is_symlink() or not path.is_file():
            raise AudioError(f"Not a regular non-symlink file: {path}")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            value = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        if Audio.signature(before) != Audio.signature(after) or Audio.signature(after) != Audio.signature(path.stat()):
            raise AudioError(f"File changed while hashing: {path}")
        return value, Audio.signature(after)

    @staticmethod
    def digest(path):
        return Audio.digest_snapshot(path)[0]

    @staticmethod
    def rows(path, fields):
        if not path.exists():
            return []
        with path.open(newline="") as stream:
            raw = list(csv.reader(stream, delimiter="\t"))
        result = []
        for index, row in enumerate(raw):
            if not row:
                continue
            if index == 0 and row[0] == fields[0]:
                continue
            # Legacy import rows have four fields; legacy discards have three.
            if len(row) not in (len(fields) - 1, len(fields)):
                raise AudioError(f"Malformed ledger; left untouched: {path}")
            result.append(dict(zip(fields, row + [""] * (len(fields) - len(row)))))
        return result

    @staticmethod
    def append(path, fields, row):
        rows = Audio.rows(path, fields)
        rows.append(row)
        make_directory(path.parent)
        fd, name = tempfile.mkstemp(prefix=".audio-ledger-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fields, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            sync_directory(path.parent)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def matches(rows, source, sha):
        return [r for r in rows if r.get("original_name", r.get("filename")) == source.name
                and r["bytes"] == str(source.stat().st_size) and r.get("sha256") == sha]

    def filed(self, source, sha, rows):
        for row in reversed(self.matches(rows, source, sha)):
            try:
                destination = self.destination(source, Path(row["destination"]))
                value, signature = self.digest_snapshot(destination)
                if value == sha:
                    return destination, signature
            except (AudioError, OSError):
                continue
        return False

    def destination(self, source, destination):
        if destination.is_symlink():
            raise AudioError("Destination must not be a symlink.")
        canonical = destination.resolve()
        if any(canonical == vol.resolve() or vol.resolve() in canonical.parents
               for vol in self.sources):
            raise AudioError("Destination cannot be inside an import source.")
        if canonical == source.resolve() or (canonical.exists() and canonical.samefile(source)):
            raise AudioError("Destination must be a separate copy, not the source or its hardlink.")
        return canonical

    def originals(self):
        if not self.sources:
            raise AudioError("No sources configured; set sources in config.toml or AUDIO_SOURCES.")
        volumes = [vol for vol in self.sources if vol.is_dir()]
        if not volumes:
            raise AudioError("No source is connected or present. Nothing was examined; this does not mean they are empty.")
        for vol in volumes:
            for source in sorted(vol.rglob("*")):
                if (source.is_file() and not source.is_symlink() and not source.name.startswith("._")
                        and source.suffix.lower() in self.extensions):
                    yield vol.name, source

    @staticmethod
    def metadata(source):
        stamp = re.search(r"_(\d{8})_(\d{6})_", source.name)
        date = (datetime.strptime("".join(stamp.groups()), "%Y%m%d%H%M%S") if stamp
                else datetime.fromtimestamp(source.stat().st_mtime))
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(source)], capture_output=True, text=True, check=True)
            duration = float(result.stdout.strip())
        except (OSError, ValueError, subprocess.CalledProcessError):
            duration = 0
        return date.strftime("%Y-%m-%d"), date.strftime("%H:%M"), duration

    def scan(self, stage=False, all_files=False):
        imports = self.rows(self.ledger, IMPORT_FIELDS)
        discards = self.rows(self.discards, DISCARD_FIELDS)
        failures = 0
        for device, source in self.originals():
            try:
                sha = self.digest(source)
                imported = self.filed(source, sha, imports)
                discarded = bool(self.matches(discards, source, sha))
                if (imported or discarded) and not all_files:
                    continue
                date, clock, duration = self.metadata(source)
                status = "imported" if imported else "discarded" if discarded else "new"
                if self.split_seconds and abs(duration - self.split_seconds) <= 0.1:
                    status += ",split-continues"
                values = [device, str(source), date, clock, f"{duration / 60:.1f}",
                          f"{source.stat().st_size / 1048576:.0f}", status]
                if stage:
                    if discarded:
                        continue
                    self.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
                    dest = self.staging / source.name
                    if dest.exists() or dest.is_symlink():
                        if self.digest(dest) != sha:
                            raise AudioError(f"Staging name collision; preserve both recordings and resolve: {source.name}")
                    else:
                        self.copy(source, dest, sha)
                    values.insert(1, str(dest))
                print("\t".join(values))
            except (AudioError, OSError) as exc:
                print(str(exc), file=sys.stderr)
                failures += 1
        if failures:
            raise AudioError(f"{failures} recording(s) failed; no clear should follow a failed stage.")

    def copy(self, source, dest, expected):
        make_directory(dest.parent)
        fd, name = tempfile.mkstemp(prefix=".audio-copy-", dir=dest.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
                output.flush()
                os.fsync(output.fileno())
            if self.digest(temporary) != expected or self.digest(source) != expected:
                raise AudioError("Checksum mismatch; partial copy removed.")
            shutil.copystat(source, temporary)
            # Atomic no-overwrite publication, including an existing symlink.
            os.link(temporary, dest)
            sync_directory(dest.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def file(self, source, dest, verify_existing=False):
        source = source.absolute()
        dest = self.destination(source, dest)
        # Parse before copying, so malformed historical state fails closed.
        self.rows(self.ledger, IMPORT_FIELDS)
        sha = self.digest(source)
        if verify_existing:
            if self.digest(dest) != sha:
                raise AudioError("Existing destination differs; no receipt recorded.")
        else:
            if dest.exists() or dest.is_symlink():
                raise AudioError(f"Refusing to overwrite existing file: {dest}")
            self.copy(source, dest, sha)
        self.append(self.ledger, IMPORT_FIELDS, dict(zip(IMPORT_FIELDS,
                    [source.name, str(source.stat().st_size), datetime.now(timezone.utc).isoformat(), str(dest), sha])))
        print(f"Verified and recorded: {dest}")

    def discard(self, source, confirm):
        if not confirm:
            raise AudioError("Discard requires the owner's sign-off in the confirmation table, then --confirm.")
        if source.resolve().parent != self.staging.resolve():
            raise AudioError("Discard accepts a staged copy only; deleting originals is a separate clear.")
        sha = self.digest(source)
        self.append(self.discards, DISCARD_FIELDS, dict(zip(DISCARD_FIELDS,
                    [source.name, str(source.stat().st_size), datetime.now(timezone.utc).isoformat(), sha])))
        source.unlink()
        sync_directory(source.parent)
        print("Recorded confirmed discard and removed its staged copy.")

    def clear(self, confirm):
        imports = self.rows(self.ledger, IMPORT_FIELDS)
        discards = self.rows(self.discards, DISCARD_FIELDS)
        for _, source in self.originals():
            sha = self.digest(source)
            discarded = bool(self.matches(discards, source, sha))
            destination = None if discarded else self.filed(source, sha, imports)
            if not discarded and not destination:
                print(f"SKIP (no matching hash receipt and intact destination, or confirmed hash discard): {source.name}")
                continue
            if confirm:
                if destination:
                    path, _ = destination
                    # Also covers explicitly reverified legacy copies whose original writer
                    # did not make durability part of its receipt contract.
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
                    sync_directory(path.parent)
                # Re-check the original immediately before unlink; all executor writes are serialized.
                if self.digest(source) != sha:
                    raise AudioError("Original changed before deletion; stopped.")
                if destination:
                    path, _ = destination
                    if path.is_symlink() or self.digest(path) != sha:
                        raise AudioError("Destination changed before deletion; stopped.")
                source.unlink()
                sync_directory(source.parent)
            print(f"{'DELETED' if confirm else 'would delete'}: {source.name}")

    def private_command(self, source, output):
        configured = setting(self.config, "AUDIO_PRIVATE_COMMAND", "private.command", "")
        if not configured:
            return ["bash", str(Path(__file__).with_name("_transcribe-local.sh")), str(source), str(output)]
        parts = shlex.split(configured) if isinstance(configured, str) else list(configured)
        if not any("{input}" in part for part in parts) or not any("{output}" in part for part in parts):
            raise AudioError("private.command must contain {input} and {output} placeholders.")
        return [part.replace("{input}", str(source)).replace("{output}", str(output)) for part in parts]

    def cloud_environment(self):
        env = dict(os.environ)
        for name, key in (("TRANSCRIBE_MODEL", "cloud.model"), ("FALLBACK_MODEL", "cloud.fallback_model"),
                          ("OPENAI_KEY_COMMAND", "cloud.key_command"), ("TRANSCRIBE_URL", "cloud.url")):
            value = setting(self.config, name, key, "")
            if value:
                env[name] = str(value)
        return env

    def transcribe(self, lane, source, output, prompt):
        self.digest(source)
        if output.exists() or output.is_symlink():
            raise AudioError("Transcript output already exists; choose a new output path.")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A private 0700 job directory per run, so separate or failed jobs never share files.
        with tempfile.TemporaryDirectory(prefix=".audio-import-", dir=self.state) as directory:
            job = Path(directory)
            result = job / "transcript.txt"
            if lane == "private":
                staged = job / ("input" + source.suffix)
                shutil.copyfile(source, staged)
                completed = subprocess.run(self.private_command(staged, result))
                if completed.returncode or not result.is_file():
                    raise AudioError("Private transcription failed; nothing published. Do not file.")
            else:
                subprocess.run(["bash", str(Path(__file__).with_name("_transcribe-cloud.sh")),
                                str(source), str(result), prompt], check=True, env=self.cloud_environment())
            if not result.read_text(encoding="utf-8").strip():
                raise AudioError("No nonblank speech transcript was produced; nothing published. Do not file.")
            self.copy(result, output, self.digest(result))
        print(f"wrote {output} [{lane}]")

    def show_config(self):
        print(f"config_home\t{config_home()}")
        print(f"state_dir\t{self.state}")
        print(f"staging_dir\t{self.staging}")
        print(f"sources\t{os.pathsep.join(map(str, self.sources)) or '(none configured)'}")
        print(f"extensions\t{','.join(self.extensions)}")
        for name in ("routing.md", "vocabulary.md"):
            path = config_home() / name
            print(f"{name}\t{path}{'' if path.exists() else ' (missing: copy it from examples/)'}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "stage"):
        commands.add_parser(command).add_argument("--all", action="store_true")
    commands.add_parser("clear").add_argument("--confirm", action="store_true")
    commands.add_parser("config")
    filing = commands.add_parser("file")
    filing.add_argument("--verify-existing", action="store_true")
    filing.add_argument("source", type=Path)
    filing.add_argument("destination", type=Path)
    discard = commands.add_parser("discard")
    discard.add_argument("--confirm", action="store_true")
    discard.add_argument("source", type=Path)
    for lane in ("cloud", "private"):
        transcribe = commands.add_parser(lane)
        transcribe.add_argument("source", type=Path)
        transcribe.add_argument("output", type=Path)
        if lane == "cloud":
            transcribe.add_argument("prompt", nargs="?", default="")
    args = parser.parse_args()
    config = load_config()
    # Optional guard: with executor_host set, every operation refuses to run on another machine,
    # so a second host can never grow its own staging area or ledger.
    owner = str(setting(config, "AUDIO_EXECUTOR_HOST", "executor_host", "")).lower().split(".")[0]
    if owner and socket.gethostname().lower().split(".")[0] != owner:
        raise AudioError(f"Audio operations run only on the configured executor '{owner}'; do not create state here.")
    audio = Audio(config)
    if args.command == "config":
        audio.show_config()
        return
    # Scan never creates a directory, ledger, or lock. Atomic ledger replacement makes its reads safe.
    with contextlib.nullcontext() if args.command == "scan" else audio.lock():
        if args.command in ("scan", "stage"):
            audio.scan(args.command == "stage", args.all)
        elif args.command == "clear":
            audio.clear(args.confirm)
        elif args.command == "file":
            audio.file(args.source, args.destination, args.verify_existing)
        elif args.command == "discard":
            audio.discard(args.source, args.confirm)
        else:
            audio.transcribe(args.command, args.source, args.output, getattr(args, "prompt", ""))


if __name__ == "__main__":
    try:
        main()
    except (AudioError, OSError, subprocess.CalledProcessError) as error:
        print(f"audio: {error}", file=sys.stderr)
        sys.exit(1)
