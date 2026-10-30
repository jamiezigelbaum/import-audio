# Transcription context prompts

Copy to `~/.config/import-audio/vocabulary.md` and edit.

`transcribe.sh` passes an optional third argument to the model as a context
prompt. It doesn't change what's transcribed; it biases spelling, so names and
jargon come back right instead of phonetically. Pass the one for the project
you think the recording belongs to; when unsure, use the general prompt and
re-run later if a project needs its own.

Keep these short. A long list makes the model likelier to insert terms that
were never said.

## General

```
Alex Example. Projects: Project One, Project Two.
```

## Project One

```
Project One. Alex Example, Sam Partner. Terms: widget, flux capacitor.
```

## Personal journal

No prompt. A journal wanders across topics, and a project prompt would pull
proper nouns into it that were never spoken.
