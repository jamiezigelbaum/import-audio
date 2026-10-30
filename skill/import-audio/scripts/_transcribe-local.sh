#!/usr/bin/env bash
# Default private lane: transcribe on this machine with a local Whisper CLI.
#
#   _transcribe-local.sh <input-audio> <output-txt>
#
# Uses whisper.cpp's `whisper-cli` when WHISPER_CPP_MODEL points at a ggml
# model file, otherwise the openai-whisper `whisper` CLI (model WHISPER_MODEL,
# default turbo). Audio never leaves the machine. To transcribe on another
# machine you own, set private.command in config.toml instead.
set -euo pipefail

IN=${1:?usage: _transcribe-local.sh <input-audio> <output-txt>}
OUT=${2:?usage: _transcribe-local.sh <input-audio> <output-txt>}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/transcribe-local.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# whisper.cpp wants 16 kHz mono WAV; -vn skips decoding any video stream.
ffmpeg -nostdin -v error -i "$IN" -vn -ac 1 -ar 16000 "$WORK/audio.wav"

if [ -n "${WHISPER_CPP_MODEL:-}" ]; then
  whisper-cli -m "$WHISPER_CPP_MODEL" -f "$WORK/audio.wav" -otxt -of "$WORK/audio" >&2
elif command -v whisper >/dev/null 2>&1; then
  whisper "$WORK/audio.wav" --model "${WHISPER_MODEL:-turbo}" --output_format txt \
    --output_dir "$WORK" ${WHISPER_LANGUAGE:+--language "$WHISPER_LANGUAGE"} >&2
else
  echo "error: no local Whisper found (install openai-whisper, or set WHISPER_CPP_MODEL for whisper.cpp)" >&2
  exit 1
fi
mv "$WORK/audio.txt" "$OUT"
