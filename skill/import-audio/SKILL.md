---
name: import-audio
description: Import voice recordings from a USB recorder such as the DJI Mic 3 (its TX1/TX2 drives) or from an _inbox folder, plus optional call notes from a meeting-notes connector such as Granola; transcribe them, work out from what was said where each belongs, confirm with the owner, and file audio plus transcript under a standard naming convention. Use when the user says "import audio", "process audio", "import from my mics", "process inbox", "import calls", "file meeting notes", or plugs in the recorder and asks to pull recordings off it.
---

# Import audio

Pulls recordings off the recorder, works out what each one *is* from what was
said in it, and files audio and transcript where that kind of recording belongs.
Call notes from a meeting-notes connector (see "Call notes") join the same
confirmation table and follow the same routing rules.

The recordings carry no label. A file off a DJI Mic 3 transmitter is
`TX02_MIC001_20260820_114252_orig.wav` and nothing more. Content is the only
reliable routing signal, so the order is always **stage → transcribe → classify
→ confirm with the owner → file**. Never file on a guess.

"The owner" below is the person whose recordings these are.

## Setup and the owner's profile

Everything personal lives outside this skill, in the owner's config directory:
`$AUDIO_IMPORT_HOME`, default `~/.config/import-audio/`. Run
`python3 scripts/audio.py config` to print the resolved paths. The directory holds:

- `config.toml` — machine settings: state directory, recorder volume names,
  executor host, transcription commands and keys. Template:
  `examples/config.toml` in this skill.
- `routing.md` — **where things go**: the folder roots, the categories, the
  per-project overrides, the naming patterns, which projects share to a git
  repo and their redaction rules, and where to log and hand off tasks. Read it
  in full before every import. Template: `examples/routing.md`.
- `vocabulary.md` — transcription context prompts (names and jargon) per
  project. Template: `examples/vocabulary.md`.

If `routing.md` is missing, stop and help the owner write one from the example;
do not invent destinations.

## Execution owner

Run every step on **one machine**: the one the recorder plugs into, which holds
the import state. Resolve `scripts/` relative to this installed skill. A remote
agent uses an existing authorized route to that machine; if there is none, say
so. Never create a second ledger on another host. Setting `executor_host` in
`config.toml` makes the scripts refuse to run anywhere else.

All mutating helpers share one nonblocking process lock and fail with "busy" if
another command is running; wait for it to finish, then retry. Only one agent
should own a whole import and its confirmation table at a time: the lock does
not reserve the workflow while you wait for the owner's answer. Never invoke the
internal `_transcribe-*.sh` scripts directly; use the public wrappers.

Requirements: Python 3.11+, Bash, FFmpeg/FFprobe, `bc` and `curl`; a local
Whisper (`openai-whisper` or whisper.cpp) for the private lane; an OpenAI API
key for the cloud lane.

## Workflow

### 1. Get the recordings off the cards — first, before anything else

```
scripts/stage-mics.sh
scripts/clear-mics.sh --confirm   # immediately after, same plug-in window
```

**Stage, then clear, back-to-back.** USB recorders can eject at random, so a
clear deferred to the end of the import often finds them gone. The safe window
is right now, while staging just proved they're mounted. Clearing deletes only
files the ledger confirms are filed and hash-verified at their destination,
plus previously confirmed discards from the discard ledger. The files just
staged are in neither and are never touched.

`stage-mics.sh` copies every not-yet-verified recording from the recorder
volumes (default `/Volumes/TX1` and `/Volumes/TX2`) into the staging directory,
verifying each by SHA-256, and prints TSV: device, staged_path, original_path,
date, time, minutes, MB, status. Add `--all` to include already-imported ones.

**Everything downstream reads the staged copy, never the card.** A volume that
vanishes mid-read fails as `Invalid argument` from both `ffmpeg` and `cp`, which
reads like a corrupt file rather than a missing disk. Staging collapses that
whole failure class into one short copy you can retry.

Staging is idempotent: an already-staged file is reused only if it still hashes
the same. A different file at that name is preserved and reported as a
collision; resolve it without overwriting either recording. If a copy fails,
re-seat the card and run it again; it skips whatever is already verified. Once
it reports no failures, the cards can be unplugged for the rest of the import.

`scripts/scan-mics.sh` is the read-only equivalent and takes the same `--all`.

If no recorder is mounted, check the `_inbox/` folders named in `routing.md`;
the owner may have copied files across by hand. They are already on local disk
and need no staging.

### The unit is the session, not the file

**One session is one entry, however many files it took.** This rule decides
numbering; getting it wrong fragments one train of thought across entries that
then read as unrelated.

People start and stop the recorder constantly inside one sitting: to think, to
cross a road, to talk to someone. Those stops say nothing about the content.
The DJI Mic 3 also cuts a recording at exactly 30.0 minutes (1800 s), which scan
flags `split-continues`. Both are mechanical, not meaningful.

What separates two entries is **turning to unrelated work in between**. A
morning walk session, a few hours of work, and an afternoon session are two
entries. The same thread of thought is one entry, even across four files and a
twenty-minute gap. An interruption that belongs to the session does not split
it: if part 1 ends "I'm going in to watch the talk" and part 2 is the owner
reporting back on it, that is one session continuing off-mic. Ask whether the
gap was *the work* or a departure from it, not how long it was.

For the files of one session: transcribe the parts in order, concatenate them
into a single transcript with `## Part N — HH:MM` headers, and name the audio
`... pt1`, `... pt2`, and so on. The entry takes the timestamp of its first part.

Signals that files belong together (use them together, not alone): a part opens
mid-thread ("and we're back"); a part ends mid-sentence at the 30-minute cap;
the topic runs straight on; gaps of minutes rather than hours. When it is
genuinely ambiguous, put both readings in the confirmation table and let the
owner choose.

### 2. Privacy routing, then transcribe

Two transcription lanes:

- **Cloud lane** — `scripts/transcribe.sh` (OpenAI audio API). Best accuracy and
  speed. For material the owner is happy to send to a cloud API.
- **Private lane** — `scripts/transcribe-private.sh`. By default runs Whisper on
  this machine; `private.command` in `config.toml` can point it at another
  machine the owner controls. Audio never goes to a third party. Required for
  personal journals and sensitive material (health, family, crises). Slower,
  and Whisper can loop, so check its output for repeated passages before filing.

`routing.md` says which categories take which lane. The category isn't known
before transcription, so route with a **sniff pass**: cut the first ~90 seconds
(`ffmpeg -t 90`, downmixed) and transcribe the clip through the PRIVATE lane.
People usually say what a recording is in the opening seconds. Private
categories → full recording through the private lane; cloud categories → cloud
lane. **Ambiguous → private lane**: a project recording transcribed privately
costs minutes; a journal sent to a cloud API can't be unsent.

Overrides: if the owner says "private" or "all private" at ingest, every
recording takes the private lane. If they say a recording is project material,
the cloud lane is fine for it.

```
scripts/transcribe.sh <input-audio> <output-txt> ["context prompt"]
scripts/transcribe-private.sh <input-audio> <output-txt>
```

Cloud lane details: default model `gpt-transcribe`, falling back per chunk to
`gpt-4o-transcribe` on error (`cloud.model`, `cloud.fallback_model`). The key
comes from `cloud.key_command` (any command that prints it, such as a password
manager lookup) or `OPENAI_API_KEY`; never print or echo it. Long audio is
downmixed and chunked at 10 minutes. Every chunk gets the same vocabulary
prompt, never the previous transcript's tail. A low-word-count guard flags
likely truncation; any failed chunk or flagged truncation prevents publishing
to the output path. Do not file a failed run. Roughly 8 seconds of wall clock
per minute of audio; for anything over ~20 minutes, run it in the background.

The context prompt is worth setting: pass the project's vocabulary from
`vocabulary.md` so names come back spelled right. A journal gets no prompt; a
project prompt would pull proper nouns into it that were never said.

The private lane keeps the **audio** off third-party servers. It does not by
itself make a cloud-hosted agent private: transcripts and snippets are
sensitive too. Follow the owner's approved content-sharing scope before putting
them in a cloud agent's context. Where reading the content isn't authorized,
keep processing on the private path and ask the owner for the category. Never
route uncertain content to the cloud just to classify it.

### 3. Classify

**A category the owner states out loud wins.** It is not a hint to weigh
against the content; it is the answer. They usually declare it in the first
seconds ("Personal journal...", "This is for <project>..."). File it there and
stop classifying. A recording that declares itself part of a project stays whole
in that project even if it reads like a journal for twenty minutes, unless
`routing.md` says otherwise for that project.

Only when no category is stated, infer one from content against the categories
and project registry in `routing.md`. When two fit, or none does, mark it
**unclear**. Never resolve "unclear" by guessing.

### 4. Confirm before filing

Show one table covering every recording and every call note: date, time,
length, a one-line summary, the proposed destination and filename, and any
proposed tasks (if `routing.md` sets up a task hand-off). Then stop and wait.
The owner corrects in one pass ("3 is journal, rest good").

This confirmation step is the point of the skill. Do not skip it because the
classification looks obvious.

### 5. File

```
scripts/file-recording.sh <source-audio> <destination-audio-path>
```

Copies, verifies by SHA-256, and atomically records a hash-qualified receipt in
the import ledger. It refuses to overwrite an existing file or to publish a
destination on a recorder card. **The source on the card is left alone** (see
"Clearing the cards").

Then write the transcript to its destination with the standard header (below),
and, only if `routing.md` names a repo for that project, copy it there under
that project's redaction rules and commit and push.

### 6. Record discards

When the owner marks a recording **discard** in the table, run
`scripts/discard-recording.sh --confirm <staged-audio>`. It records filename,
bytes, date and SHA-256 in the discard ledger, then deletes only that staged
copy. Never append discard rows by hand. The card copy is deleted by
`clear-mics.sh` in the next plug-in window, or right now if the cards are still
mounted (worth one try; never worth asking the owner to re-plug).

### 7. Log and hand off tasks (optional)

If `routing.md` names an intake log, add a row for every filed item. If it names
a task manager, write the confirmed tasks there after the owner confirms, each
linking back to its log row. Journals get a date-and-location row only, with no
summary and no tasks unless the owner asks.

## Call notes (optional)

Online calls can arrive as meeting notes rather than audio, from a connector
such as Granola (`list_meetings`, `get_meetings`, `get_meeting_transcript`). They
ride the same workflow: classify, one confirmation table, file, log, tasks. If
the agent has no such connector, say so and skip them.

1. **Find candidates.** Use the dedupe index `routing.md` names (for example the
   intake log, with the meeting id in a column). A meeting the connector has that
   the index lacks gets a new row, never a silent filing.
2. **Fetch.** Get the owner's own notes, the AI summary, the attendees and the
   note URL, then the verbatim transcript. Transcripts are paginated: keep
   fetching until complete; never file the first page alone. A meeting with no
   transcript and no notes goes in the table as exactly that; the owner chooses
   between a header-only stub and a skip. Never invent content.
3. **Classify, confirm.** As steps 3 and 4.
4. **File.** Write the note (format below) to the staging directory as
   `YYYY-MM-DD HHMM <who or topic> call.md`, then `scripts/file-recording.sh` it
   to its destination like any other file.
5. **Log, then tasks**, as step 7.

Health, family and finance calls never go to a repo. Calls with other people
follow the destination project's redaction rules, and the meeting tool's summary
and notes are content too: redact them the same way as the transcript.

### Call note file format

```
# <Title> — YYYY-MM-DD

**Date**: YYYY-MM-DD HH:MM
**Source**: <tool> meeting <meeting-id> (<note URL>)
**Participants**: <names, the owner included>
**Captured via**: <tool>

---

## Notes

<the owner's own notes, if any; omit the section when there are none>

## Summary

<the tool's AI summary, as returned>

## Transcript

<verbatim transcript, all pages>
```

## Transcript header

Every transcript opens with this block, so a renamed file can always be traced
back to its recording:

```
# <Title> — YYYY-MM-DD

**Date**: YYYY-MM-DD HH:MM
**Source**: TX02_MIC001_20260820_114252_orig.wav
**Transcribed via**: <actual lane and model(s), including any fallback>

---

<transcript body>
```

Multi-part recordings list every source file and separate parts in the body
with `## Part N — HH:MM` headers.

## Clearing the cards

The cards are emptied on each import, at the START, right after staging, because
the plug-in window that proves they're mounted is the only reliable one. Each
import therefore clears the previous import's files plus accumulated discards.
The invariant: a file leaves the card only when it is ledger-confirmed AND
hash-verified at its destination, or in the discard ledger with a matching
content hash from the owner's sign-off. Clear rehashes the card file and the
destination; a missing, symlinked or changed destination fails closed.

```
scripts/clear-mics.sh            # dry run
scripts/clear-mics.sh --confirm  # delete imported-and-verified files
```

## Ledger receipts without a hash

Rows without a SHA-256 hash (from an older version or hand edits) stay readable
but **cannot authorize dedupe or deletion**. After checking an existing file
against the confirmed routing, add a verified receipt without overwriting
either copy:

```
scripts/file-recording.sh --verify-existing <source-audio> <existing-destination>
```

A mismatch stops.

## Rules

- Audio never goes into a git repo. Transcripts and call notes go to a repo only
  when `routing.md` names one for that project.
- Re-running an import reuses only matching content: the ledger dedupes by
  filename, byte size and SHA-256 with an intact destination.
- Personal journals are filed **unredacted and in full**, in their private
  folder. Repo copies get redaction only where `routing.md` asks for it.
- If a recording spans two categories (a journal that wanders into a project),
  file the complete copy in the journal and pull only the relevant excerpts into
  the project, noting that in the header.
