# Where recordings go

Copy to `~/.config/import-audio/routing.md` and make it yours. The agent reads
this file in full before every import. Everything here is an example: the
folder layout below is a PARA-style one (Projects, Areas, Resources, Archive)
in a synced folder, but any layout works as long as this file describes it.

## Who and where

- Owner: Alex Example (used in transcript names and call participants).
- Root: `~/Dropbox/`.
- Inbox folders for hand-copied files: `~/Dropbox/1 Projects/*/_inbox/`.
  Anything found there is treated exactly like a file off the card, and is
  removed from `_inbox/` once filed.

## Categories and lanes

| Category | Lane | Audio | Transcript |
|---|---|---|---|
| A project | cloud | `1 Projects/<Project>/audio/` | `1 Projects/<Project>/transcripts/` |
| Personal journal | **private** | `3 Resources/Audio Journals/` | alongside the audio |
| Daily planning | cloud | `2 Areas/Planning/audio/` | `2 Areas/Planning/transcripts/` |
| A call about a work topic | cloud | `2 Areas/Work/<Topic>/Calls/` | same folder |
| Health or finances | **private** | the matching `2 Areas/` folder, in `Calls/` | same folder |
| Unclear | private | ask | ask |

Build the project registry by listing `1 Projects/` at run time rather than
trusting a list written here; projects come and go. A topic folder that doesn't
exist, or a recording that fits two, is **unclear**: ask.

## Naming

| | pattern |
|---|---|
| project audio | `YYYY-MM-DD HHMM <type> N [ptM].wav` (N counts that type in the folder) |
| project transcript | `YYYY-MM-DD HHMM <type> N.txt` |
| journal | `YYYY-MM-DD HHMM journal [ptM].wav` and `.txt`; the transcript's `# ` title line carries a short description |
| call note | `YYYY-MM-DD HHMM <who or topic> call.md` |

## Journals

Filed in full and unredacted. This is the private copy of record; that split is
what makes narrow redaction safe elsewhere. Never committed to any repo.

## Per-project overrides

A project can declare its own rules in an `## Audio import` section of a
`README.md` or `AGENTS.md` in its folder (audio and transcript folders, naming,
repo, redaction). Read that before filing into the project; it overrides the
defaults above. Or list them here:

### Project One

- Shares to repo `~/Code/project-one`, folder `docs/transcripts/`, committed
  and pushed after filing. Audio stays in the synced folder.
- Redaction in the repo copy: cut personal, family and health matters and
  personal money; replace each cut with a bracketed note such as
  `[Personal conversation redacted.]`. Never invent content.
- Context prompt: "Project One" in `vocabulary.md`.

## Sharing switch

A transcript or call note goes to git only when its project names a repo above.
Audio never goes in git. Health, family and finance material never goes to a
repo, whatever else matches.

## Intake log and tasks (optional)

- Log: add a row per filed item to `~/Notes/intake-log.md` (date, title,
  project, transcript path, status, tasks). It is also the dedupe index for
  call notes (meeting id in the transcript column until filed).
- Tasks: after the owner confirms the table, add the confirmed tasks to the
  task manager (for example via its CLI or MCP connector), each linking back to
  its log row.

## Deciding which project

Match on what's actually said: the project name, the people named, the
vocabulary. The owner usually states the type in the opening seconds. When two
projects fit, or nothing fits, mark the row **unclear** and ask. A misfiled
recording is worse than a question.
