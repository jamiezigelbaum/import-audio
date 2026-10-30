"""Synthetic audio only: no devices, credentials, or network calls."""
import csv
import fcntl
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skill/import-audio/scripts"


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.home / ".local/state/import-audio"
        self.card = self.root / "Volumes/TX1"
        self.card.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "AUDIO_MIC_ROOT": str(self.card.parent),
                    "AUDIO_EXECUTOR_HOST": socket.gethostname(), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        for name in ("AUDIO_STATE_DIR", "AUDIO_STAGING_DIR", "AUDIO_IMPORT_LEDGER", "AUDIO_DISCARD_LEDGER",
                     "AUDIO_IMPORT_HOME", "AUDIO_MIC_VOLUMES", "AUDIO_PRIVATE_COMMAND", "OPENAI_API_KEY",
                     "OPENAI_KEY_COMMAND", "TRANSCRIBE_MODEL", "FALLBACK_MODEL", "TRANSCRIBE_URL"):
            self.env.pop(name, None)
        self.stub("ffprobe", "print('1800')")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body + "\n")
        path.chmod(0o755)

    def run_audio(self, command, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPTS / "audio.py"), command, *map(str, args)],
                                env=self.env, text=True, capture_output=True, timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def recording(self, name="TX02_MIC001_20260907_101000_orig.wav", data=b"synthetic audio"):
        source = self.card / name
        source.write_bytes(data)
        return source

    def ledger(self):
        with (self.state / "audio-import-ledger.tsv").open() as stream:
            return list(csv.DictReader(stream, delimiter="\t"))

    def test_scan_is_read_only_even_with_missing_state(self):
        source = self.recording()
        result = self.run_audio("scan")
        self.assertIn(str(source), result.stdout)
        self.assertIn("split-continues", result.stdout)
        self.assertFalse(self.state.exists())

    def test_stage_reuses_verified_copy_and_refuses_collision(self):
        source = self.recording()
        self.run_audio("stage")
        staged = self.state / "audio-staging" / source.name
        inode = staged.stat().st_ino
        self.run_audio("stage")
        self.assertEqual(staged.stat().st_ino, inode)
        staged.write_bytes(b"different session")
        result = self.run_audio("stage", ok=False)
        self.assertIn("collision", result.stderr)
        self.assertEqual(staged.read_bytes(), b"different session")
        self.assertTrue(source.exists())

    def test_filing_hash_receipt_and_clear_require_intact_destination(self):
        source = self.recording(data=b"original")
        destination = self.root / "Dropbox/final.wav"
        self.run_audio("file", source, destination)
        self.assertEqual(self.ledger()[0]["sha256"], hashlib.sha256(b"original").hexdigest())
        self.run_audio("clear")
        self.assertTrue(source.exists())
        destination.write_bytes(b"replaced")  # Same length must not pass.
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())
        self.assertIn(str(source), self.run_audio("scan").stdout)
        destination.write_bytes(b"original")
        self.run_audio("clear", "--confirm")
        self.assertFalse(source.exists())

    def test_changed_card_with_same_name_and_size_is_not_deleted(self):
        source = self.recording(data=b"original")
        self.run_audio("file", source, self.root / "dest.wav")
        source.write_bytes(b"replaced")
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())

    def test_destination_replaced_during_final_card_hash_is_not_deleted(self):
        import importlib.util
        from unittest.mock import patch
        source = self.recording(data=b"original")
        destination = self.root / "dest.wav"
        self.run_audio("file", source, destination)
        spec = importlib.util.spec_from_file_location("audio_fixture", SCRIPTS / "audio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_digest = module.Audio.digest
        calls = 0

        def digest_and_replace(path):
            nonlocal calls
            value = original_digest(path)
            if path == source:
                calls += 1
                if calls == 2:
                    destination.write_bytes(b"replaced")
            return value

        with patch.dict(os.environ, self.env, clear=True), patch.object(module.Audio, "digest", side_effect=digest_and_replace):
            with self.assertRaisesRegex(module.AudioError, "Destination changed"):
                module.Audio().clear(True)
        self.assertTrue(source.exists())

    def test_same_size_rewrite_with_restored_mtime_is_not_deleted(self):
        import importlib.util
        from unittest.mock import patch
        source = self.recording(data=b"original")
        destination = self.root / "dest.wav"
        self.run_audio("file", source, destination)
        spec = importlib.util.spec_from_file_location("audio_fixture", SCRIPTS / "audio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_digest = module.Audio.digest
        calls = 0

        def digest_and_rewrite(path):
            nonlocal calls
            value = original_digest(path)
            if path == source:
                calls += 1
                if calls == 2:
                    before = destination.stat()
                    with destination.open("r+b") as stream:
                        stream.write(b"replaced")
                    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
            return value

        with patch.dict(os.environ, self.env, clear=True), patch.object(module.Audio, "digest", side_effect=digest_and_rewrite):
            with self.assertRaisesRegex(module.AudioError, "Destination changed"):
                module.Audio().clear(True)
        self.assertTrue(source.exists())

    def test_ctime_change_while_hashing_is_not_a_content_change(self):
        # Dropbox's File Provider touches ctime (xattrs, attributes) during or after a copy.
        # Content identity is inode + size + mtime; ctime alone must not fail the hash.
        import importlib.util
        from unittest.mock import patch
        source = self.recording(data=b"original")
        spec = importlib.util.spec_from_file_location("audio_fixture", SCRIPTS / "audio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_digest = module.hashlib.file_digest

        def digest_and_touch_ctime(stream, name):
            value = real_digest(stream, name)
            before = os.stat(source).st_ctime_ns
            os.chmod(source, 0o600)
            os.chmod(source, 0o644)
            self.assertNotEqual(os.stat(source).st_ctime_ns, before)
            return value

        with patch.object(module.hashlib, "file_digest", side_effect=digest_and_touch_ctime):
            self.assertEqual(module.Audio.digest(source), hashlib.sha256(b"original").hexdigest())
        self.assertEqual(self.run_audio("file", source, self.root / "dest.wav").returncode, 0)

    def test_content_change_while_hashing_still_fails(self):
        import importlib.util
        from unittest.mock import patch
        source = self.recording(data=b"original")
        spec = importlib.util.spec_from_file_location("audio_fixture", SCRIPTS / "audio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_digest = module.hashlib.file_digest

        def digest_and_append(stream, name):
            value = real_digest(stream, name)
            with source.open("ab") as writer:
                writer.write(b" more")
            return value

        with patch.object(module.hashlib, "file_digest", side_effect=digest_and_append):
            with self.assertRaisesRegex(module.AudioError, "changed while hashing"):
                module.Audio.digest(source)

    def test_destination_directory_sync_failure_prevents_deletion(self):
        import importlib.util
        from unittest.mock import patch
        source = self.recording()
        destination = self.root / "archive/dest.wav"
        self.run_audio("file", source, destination)
        spec = importlib.util.spec_from_file_location("audio_fixture", SCRIPTS / "audio.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, self.env, clear=True), patch.object(module, "sync_directory", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(OSError, "sync failed"):
                module.Audio().clear(True)
        self.assertTrue(source.exists())

    def test_legacy_receipts_do_not_authorize_delete_or_dedupe(self):
        source = self.recording()
        destination = self.root / "old.wav"
        destination.write_bytes(source.read_bytes())
        self.state.mkdir(parents=True)
        ledger = self.state / "audio-import-ledger.tsv"
        legacy = ("original_name\tbytes\timported_at\tdestination\n"
                  f"{source.name}\t{source.stat().st_size}\t2026-08-01\t{destination}\n")
        ledger.write_text(legacy)
        self.run_audio("clear", "--confirm")
        self.assertEqual(ledger.read_text(), legacy)
        self.assertTrue(source.exists())
        self.assertIn(str(source), self.run_audio("scan").stdout)
        self.run_audio("file", "--verify-existing", source, destination)
        self.assertEqual(len(self.ledger()), 2)
        self.assertEqual(self.ledger()[0]["sha256"], "")
        self.assertNotEqual(self.ledger()[1]["sha256"], "")
        self.run_audio("clear", "--confirm")
        self.assertFalse(source.exists())

    def test_existing_destination_must_match_to_upgrade_receipt(self):
        source = self.recording()
        destination = self.root / "old.wav"
        destination.write_bytes(b"different")
        self.run_audio("file", "--verify-existing", source, destination, ok=False)
        self.assertFalse((self.state / "audio-import-ledger.tsv").exists())
        self.run_audio("file", source, destination, ok=False)
        self.assertEqual(destination.read_bytes(), b"different")

    def test_missing_destination_and_symlink_destination_are_not_safe(self):
        source = self.recording()
        dest = self.root / "dest.wav"
        self.run_audio("file", source, dest)
        dest.unlink()
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())
        dest.symlink_to(source)
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())

    def test_filing_cannot_use_a_destination_on_a_card_through_a_directory_alias(self):
        source = self.root / "staged.wav"
        source.write_bytes(b"synthetic")
        alias = self.root / "alias"
        alias.symlink_to(self.card, target_is_directory=True)
        self.run_audio("file", source, alias / "dest.wav", ok=False)
        self.assertFalse((self.card / "dest.wav").exists())

    def test_retargeted_destination_directory_cannot_make_card_its_own_backup(self):
        source = self.recording()
        archive = self.root / "archive"
        archive.mkdir()
        alias = self.root / "destination"
        alias.symlink_to(archive, target_is_directory=True)
        self.run_audio("file", source, alias / source.name)
        self.assertEqual(self.ledger()[0]["destination"], str((archive / source.name).resolve()))
        (archive / source.name).unlink()
        alias.unlink()
        alias.symlink_to(self.card, target_is_directory=True)
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())
        # Also cover already-written hash receipts with lexical destinations.
        ledger = self.state / "audio-import-ledger.tsv"
        ledger.write_text(ledger.read_text().replace(str((archive / source.name).resolve()), str(alias / source.name)))
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())

    def test_hardlink_to_source_is_not_a_separate_verified_copy(self):
        source = self.recording()
        dest = self.root / "dest.wav"
        self.run_audio("file", source, dest)
        dest.unlink()
        os.link(source, dest)
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())
        self.run_audio("file", "--verify-existing", source, dest, ok=False)

    def test_confirmed_discard_is_hashed_and_legacy_discard_is_not_trusted(self):
        source = self.recording()
        self.state.mkdir(parents=True)
        discards = self.state / "audio-discard-ledger.tsv"
        discards.write_text(f"{source.name}\t{source.stat().st_size}\t2026-08-01\n")
        self.run_audio("clear", "--confirm")
        self.assertTrue(source.exists())
        self.run_audio("stage")
        staged = self.state / "audio-staging" / source.name
        self.run_audio("discard", staged, ok=False)
        self.assertTrue(staged.exists())
        self.run_audio("discard", "--confirm", staged)
        self.assertFalse(staged.exists())
        self.run_audio("stage")
        self.assertFalse(staged.exists())
        self.run_audio("clear", "--confirm")
        self.assertFalse(source.exists())

    def test_discard_cannot_delete_a_card_directly(self):
        source = self.recording()
        self.run_audio("discard", "--confirm", source, ok=False)
        self.assertTrue(source.exists())

    def test_malformed_ledger_leaves_audio_untouched(self):
        source = self.recording()
        self.state.mkdir(parents=True)
        (self.state / "audio-import-ledger.tsv").write_text("not a receipt\n")
        dest = self.root / "dest.wav"
        self.run_audio("file", source, dest, ok=False)
        self.assertFalse(dest.exists())
        self.run_audio("clear", "--confirm", ok=False)
        self.assertTrue(source.exists())

    def test_owner_guard_prevents_remote_state_creation(self):
        self.env["AUDIO_EXECUTOR_HOST"] = "a-different-owner"
        self.run_audio("stage", ok=False)
        self.assertFalse(self.state.exists())

    def test_common_lock_rejects_other_harness_operations(self):
        source = self.recording()
        self.state.mkdir(parents=True)
        with (self.state / "audio-import.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_audio("file", source, self.root / "dest.wav", ok=False)
            self.assertIn("busy", result.stderr)
        self.assertFalse((self.root / "dest.wav").exists())

    def private_stub(self, body):
        self.stub("fake-whisper", body)
        self.env["AUDIO_PRIVATE_COMMAND"] = "fake-whisper {input} {output}"

    def test_private_job_is_unique_and_cleanup_cannot_remove_neighbor(self):
        source = self.recording()
        self.private_stub('''import json, os, pathlib, sys
p = pathlib.Path(sys.argv[1])
with (pathlib.Path(os.environ['HOME']) / 'jobs.jsonl').open('a') as stream:
    stream.write(json.dumps({'path': str(p), 'mode': p.parent.stat().st_mode & 0o777}) + '\\n')
pathlib.Path(sys.argv[2]).write_text('synthetic transcript')''')
        neighbor = self.state / ".audio-import-other-job"
        neighbor.mkdir(parents=True)
        (neighbor / "keep").write_bytes(b"neighbor")
        self.run_audio("private", source, self.root / "one.txt")
        self.run_audio("private", source, self.root / "two.txt")
        import json
        jobs = [json.loads(line) for line in (self.home / "jobs.jsonl").read_text().splitlines()]
        self.assertNotEqual(Path(jobs[0]["path"]).parent, Path(jobs[1]["path"]).parent)
        for job in jobs:
            self.assertEqual(job["mode"], 0o700)
            self.assertFalse(Path(job["path"]).parent.exists())
        self.assertEqual((neighbor / "keep").read_bytes(), b"neighbor")
        self.assertEqual((self.root / "one.txt").read_text(), "synthetic transcript")

    def test_default_private_lane_uses_local_whisper(self):
        source = self.recording()
        self.stub("ffmpeg", "import pathlib, sys; pathlib.Path(sys.argv[-1]).write_bytes(b'wav')")
        self.stub("whisper", '''import pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output_dir') + 1])
(out / (pathlib.Path(args[0]).stem + '.txt')).write_text('local transcript')''')
        self.run_audio("private", source, self.root / "local.txt")
        self.assertEqual((self.root / "local.txt").read_text(), "local transcript")

    def test_private_command_needs_placeholders(self):
        source = self.recording()
        self.env["AUDIO_PRIVATE_COMMAND"] = "fake-whisper"
        result = self.run_audio("private", source, self.root / "out.txt", ok=False)
        self.assertIn("{input} and {output}", result.stderr)
        self.assertFalse((self.root / "out.txt").exists())

    def test_private_command_failure_is_not_published(self):
        source = self.recording()
        self.private_stub('''import pathlib, sys
pathlib.Path(sys.argv[2]).write_text('partial')
sys.exit(3)''')
        output = self.root / "failed.txt"
        result = self.run_audio("private", source, output, ok=False)
        self.assertIn("Private transcription failed", result.stderr)
        self.assertFalse(output.exists())

    def test_running_private_job_blocks_stage(self):
        source = self.recording()
        self.private_stub('''import os, pathlib, sys, time
root = pathlib.Path(os.environ['HOME'])
(root / 'started').touch()
while not (root / 'release').exists(): time.sleep(0.02)
pathlib.Path(sys.argv[2]).write_text('done')''')
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "audio.py"), "private", str(source),
                                    str(self.root / "out.txt")], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.home / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.home / "started").exists())
            self.assertIn("busy", self.run_audio("stage", ok=False).stderr)
        finally:
            (self.home / "release").touch()
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)

    def cloud_stubs(self):
        self.env["OPENAI_API_KEY"] = "synthetic-test-token"
        self.stub("ffmpeg", '''import pathlib, sys
pattern = sys.argv[-1]
for i in range(2): pathlib.Path(pattern.replace('%03d', f'{i:03d}')).write_bytes(b'synthetic chunk')''')
        self.stub("bc", '''import sys
value = eval(sys.stdin.read().strip(), {'__builtins__': {}}, {})
print(int(value) if isinstance(value, bool) else value)''')
        self.stub("curl", '''import json, os, pathlib, sys
args = sys.argv[1:]
model = next(x.split('=', 1)[1] for x in args if x.startswith('model='))
prompt = next(x.split('=', 1)[1] for x in args if x.startswith('prompt='))
with (pathlib.Path(os.environ['HOME']) / 'requests.jsonl').open('a') as stream:
    stream.write(json.dumps({'model': model, 'prompt': prompt}) + '\\n')
if model == 'gpt-transcribe' or os.environ.get('FAIL_BOTH'):
    print('{"error": {"message": "synthetic failure"}}')
else: print('synthetic transcript')''')

    def test_cloud_fallback_preserves_prompt_and_does_not_publish_failures(self):
        import json
        self.cloud_stubs()
        source = self.recording()
        self.env["MIN_WPM"] = "0"
        output = self.root / "cloud.txt"
        result = self.run_audio("cloud", source, output, "example vocabulary")
        self.assertIn("fallback model", result.stderr)
        self.assertEqual(output.read_text(), "synthetic transcript synthetic transcript\n")
        requests = [json.loads(line) for line in (self.home / "requests.jsonl").read_text().splitlines()]
        self.assertEqual([r["model"] for r in requests],
                         ["gpt-transcribe", "gpt-4o-transcribe"] * 2)
        self.assertEqual({r["prompt"] for r in requests}, {"example vocabulary"})
        self.env["FAIL_BOTH"] = "1"
        failed = self.root / "failed.txt"
        result = self.run_audio("cloud", source, failed, ok=False)
        self.assertIn("chunk(s) failed", result.stderr)
        self.assertFalse(failed.exists())
        self.assertFalse(list(self.state.glob(".audio-import-*")))

    def test_cloud_truncation_failure_is_not_published(self):
        self.cloud_stubs()
        source = self.recording()
        self.env["MIN_WPM"] = "45"
        output = self.root / "truncated.txt"
        result = self.run_audio("cloud", source, output, ok=False)
        self.assertIn("TRUNCATED", result.stderr)
        self.assertFalse(output.exists())

    def test_cloud_transport_failure_text_is_not_a_transcript(self):
        self.cloud_stubs()
        self.stub("curl", '''import sys
print('upstream gateway unavailable')
sys.exit(22)''')
        source = self.recording()
        self.env["MIN_WPM"] = "0"
        output = self.root / "error.txt"
        result = self.run_audio("cloud", source, output, ok=False)
        self.assertIn("FALLBACK", result.stderr)
        self.assertFalse(output.exists())

    def test_cloud_all_skipped_chunks_do_not_publish_a_blank_transcript(self):
        self.cloud_stubs()
        self.stub("ffprobe", "print('0.1')")
        source = self.recording()
        output = self.root / "blank.txt"
        result = self.run_audio("cloud", source, output, ok=False)
        self.assertIn("No nonblank speech transcript", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse((self.home / "requests.jsonl").exists())

    def test_cloud_key_command_is_used_and_empty_key_fails_before_upload(self):
        self.cloud_stubs()
        self.env["MIN_WPM"] = "0"
        del self.env["OPENAI_API_KEY"]
        self.stub("key-lookup", "print('from-command', end='')")
        self.stub("curl", '''import os, pathlib, sys
header = sys.stdin.read()
(pathlib.Path(os.environ['HOME']) / 'header').write_text(header)
print('synthetic transcript')''')
        self.env["OPENAI_KEY_COMMAND"] = "key-lookup"
        self.run_audio("cloud", self.recording(), self.root / "keyed.txt")
        self.assertIn("Bearer from-command", (self.home / "header").read_text())
        (self.home / "header").unlink()
        self.env["OPENAI_KEY_COMMAND"] = "true"
        source = self.recording()
        output = self.root / "nokey.txt"
        result = self.run_audio("cloud", source, output, ok=False)
        self.assertIn("empty OpenAI key", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse((self.home / "header").exists())

    def test_private_whitespace_output_is_not_published(self):
        source = self.recording()
        self.private_stub('''import pathlib, sys
pathlib.Path(sys.argv[2]).write_text(' \\n\\t\\n')''')
        output = self.root / "private-blank.txt"
        result = self.run_audio("private", source, output, ok=False)
        self.assertIn("No nonblank speech transcript", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.state.glob(".audio-import-*")))

    def test_private_empty_success_does_not_publish_output(self):
        source = self.recording()
        self.private_stub("print('synthetic no-output success')")
        output = self.root / "missing.txt"
        self.run_audio("private", source, output, ok=False)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.state.glob(".audio-import-*")))

    def test_config_file_sets_volumes_and_env_overrides_it(self):
        config = self.home / ".config/import-audio"
        config.mkdir(parents=True)
        (config / "config.toml").write_text('mic_volumes = ["RX"]\n')
        other = self.card.parent / "RX"
        other.mkdir()
        (other / "REC001.wav").write_bytes(b"other recorder")
        tx1 = self.recording()
        result = self.run_audio("scan")
        self.assertIn("REC001.wav", result.stdout)
        self.assertNotIn(str(tx1), result.stdout)
        self.env["AUDIO_MIC_VOLUMES"] = "TX1"
        result = self.run_audio("scan")
        self.assertIn(str(tx1), result.stdout)
        self.assertNotIn("REC001.wav", result.stdout)

    def test_no_executor_host_means_any_machine(self):
        del self.env["AUDIO_EXECUTOR_HOST"]
        self.recording()
        self.run_audio("stage")

    def test_no_mic_is_not_reported_as_empty(self):
        self.card.rmdir()
        result = self.run_audio("clear", "--confirm", ok=False)
        self.assertIn("Nothing was examined", result.stderr)


if __name__ == "__main__":
    unittest.main()
