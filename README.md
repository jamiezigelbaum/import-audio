# import-audio

Talk into a recorder, plug it in, and have your AI agent turn the recordings
into transcripts filed where they belong.

This is an agent skill for Claude Code and Codex (and any agent that reads a
`SKILL.md`). It was built around the **DJI Mic 3**, whose transmitters mount as
USB drives (`TX1`, `TX2`), but works with any recorder that shows up as a drive.

## What happens when you say "import audio"

1. **Stage.** Every new recording is copied off the recorder and checked by
   SHA-256, straight away, because these drives tend to eject on their own.
2. **Sniff.** The first 90 seconds are transcribed locally to find out what the
   recording is. Most people say it up front: "Personal journal...", "Notes on
   Project One...".
3. **Transcribe.** Private material (journals, health, family) is transcribed
   on your own machine with Whisper; the audio never leaves it. Project material
   can use the OpenAI API, which is faster and more accurate.
4. **Classify.** Each recording is matched to a project or category from your
   routing profile. Several files from one sitting become one entry.
5. **Confirm.** You get one table: what each recording is, where it will go,
   and any tasks it mentions. You correct it in one line ("3 is journal, rest
   good"). Nothing is filed before you answer.
6. **File.** Audio and transcript are copied to their folders, verified, and
   recorded in a ledger. Projects you choose can also get a (redacted)
   transcript committed to a git repo.
7. **Clear.** On the next plug-in, recordings that are safely filed are deleted
   from the recorder. A file leaves the recorder only when its filed copy still
   matches by hash, or when you discarded it in the table.

Meeting notes from a connector such as Granola can go through the same table.

## Install

Needs macOS or Linux, Python 3.11+, FFmpeg, `bc` and `curl`. For the private
lane, a local Whisper: `pip install openai-whisper`, or whisper.cpp
(`brew install whisper-cpp`, then point `WHISPER_CPP_MODEL` at a ggml model).
For the cloud lane, an OpenAI API key.

```
git clone <this repo> ~/Code/import-audio
cd ~/Code/import-audio
./install.sh
```

`install.sh` links the skill into `~/.claude/skills/` and `~/.agents/skills/`,
and copies three templates into `~/.config/import-audio/`:

| File | What to put in it |
|---|---|
| `config.toml` | Recorder volume names, where state lives, how to get the API key, an optional private-transcription command |
| `routing.md` | Your folders, categories, naming, which projects share to a repo and how to redact them, and where to log tasks |
| `vocabulary.md` | Names and jargon per project, so they come back spelled right |

`routing.md` is the important one: it is how the agent knows where things go.
Then plug in the recorder and tell your agent "import audio".

To keep the API key out of files, set `cloud.key_command` to a command that
prints it, for example a 1Password lookup (`op read "op://Private/OpenAI/credential"`).

## Layout

```
skill/import-audio/
  SKILL.md          the procedure the agent follows
  scripts/          stage, scan, clear, file, discard, transcribe (cloud and private)
  examples/         profile templates with placeholder data
tests/              synthetic-audio tests: no devices, network or keys
install.sh
```

The scripts are usable on their own too: `skill/import-audio/scripts/scan-mics.sh`
lists what's on the recorder; `python3 skill/import-audio/scripts/audio.py config`
shows the resolved settings.

## Safety rules

- Nothing is ever overwritten.
- A recording is deleted from the recorder only with a hash-verified receipt
  for its filed copy, or a confirmed discard.
- Nothing is filed without your confirmation.
- Audio is never committed to git.
- One machine owns the state; set `executor_host` if more than one could run it.

## Tests

```
python3 -m unittest discover -s tests -v
```
