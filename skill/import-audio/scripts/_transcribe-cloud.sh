#!/usr/bin/env bash
# Transcribe one audio file to plain text using the OpenAI audio API.
#
#   transcribe.sh <input-audio> <output-txt> [context-prompt]
#
# Long files are chunked so neither the 25MB upload limit nor the model's
# output-token ceiling can truncate a transcript. Each chunk uses the
# same vocabulary prompt so names and jargon stay consistent across joins.

set -euo pipefail

IN=${1:?usage: transcribe.sh <input-audio> <output-txt> [context-prompt]}
OUT=${2:?usage: transcribe.sh <input-audio> <output-txt> [context-prompt]}
SEED_PROMPT=${3:-}

MODEL=${TRANSCRIBE_MODEL:-gpt-transcribe}
# Preserve the installed model defaults; both are operator-overridable.
# Review fallback output for repetitions and proper-noun errors.
FALLBACK_MODEL=${FALLBACK_MODEL:-gpt-4o-transcribe}
CHUNK_SECONDS=${CHUNK_SECONDS:-600}
# A full-length chunk of real speech lands well above this; a chunk that hits
# the model's output-token ceiling truncates silently and reads clean. 45 wpm
# catches that while clearing slow walking-journal pace (~85 wpm observed).
MIN_WPM=${MIN_WPM:-45}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/transcribe.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# The key comes from OPENAI_KEY_COMMAND (any command that prints it, e.g. a
# password-manager lookup) or else OPENAI_API_KEY. It stays out of argv and
# output; curl reads the header from stdin.
if [ -n "${OPENAI_KEY_COMMAND:-}" ]; then
  key=$(bash -c "$OPENAI_KEY_COMMAND")
else
  key=${OPENAI_API_KEY:-}
fi
[ -n "$key" ] || { echo "error: empty OpenAI key (set OPENAI_API_KEY or cloud.key_command)" >&2; exit 1; }
URL=${TRANSCRIBE_URL:-https://api.openai.com/v1/audio/transcriptions}

# 16 kHz mono MP3: everything speech recognition needs, ~15x smaller than the
# recorder's 48 kHz float WAV.
#
# -vn matters more than it looks. Without it, a video input makes ffmpeg decode
# every frame to produce audio it then discards: a 96-minute 1080p lecture took
# 117 minutes to chunk, and a 106-minute one was still going at 86 minutes with
# nothing written. With -vn the same file yields its audio in about 4 seconds.
# Any input with a video stream -- a course recording, a screen capture, a video
# call -- hits this, so it is not an edge case.
ffmpeg -nostdin -v error -i "$IN" -vn -ac 1 -ar 16000 -b:a 32k \
  -f segment -segment_time "$CHUNK_SECONDS" "$WORK/chunk%03d.mp3"

post_chunk() {
  local file=$1 model=$2 prompt=$3
  printf 'header = "Authorization: Bearer %s"\n' "$key" | curl -sS --fail --config - \
    --max-time 900 \
    -X POST "$URL" \
    -F "file=@${file}" \
    -F "model=${model}" \
    -F "response_format=text" \
    -F "prompt=${prompt}"
}

# The API reports failures as a JSON body on a 200-shaped response, so the only
# way to tell a transcript from an error is to look at the payload.
is_error() {
  [ -z "${1//[[:space:]]/}" ] && return 0
  printf '%s' "$1" | tr -d '\n' | grep -q '"error"[[:space:]]*:' && return 0
  return 1
}

: > "$OUT"
failed=0
fell_back=0
truncated=0
for chunk in "$WORK"/chunk*.mp3; do
  name=$(basename "$chunk")

  # Splitting on a fixed interval can leave a sliver of a final chunk that is
  # below the API's 0.1s floor. It holds no speech -- skip it.
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$chunk" 2>/dev/null || echo 0)
  if [ "$(echo "$dur < 0.3" | bc -l)" -eq 1 ]; then
    continue
  fi

  # The seed prompt is passed to every chunk as-is. Never seed with the
  # previous chunk's tail: the model re-emits the seed as transcript and
  # duplicates passages.
  text=$(post_chunk "$chunk" "$MODEL" "$SEED_PROMPT") || text=""
  if is_error "$text"; then
    echo "FALLBACK: ${MODEL} failed on ${name}, retrying with ${FALLBACK_MODEL}" >&2
    fell_back=1
    text=$(post_chunk "$chunk" "$FALLBACK_MODEL" "$SEED_PROMPT") || text=""
  fi
  if is_error "$text"; then
    # Mark the gap in place rather than silently dropping audio or writing an
    # error blob into the transcript body.
    echo "error: both models failed on ${name}" >&2
    printf '\n[transcription failed for %s -- audio not captured]\n' "$name" >> "$OUT"
    failed=$((failed+1))
    continue
  fi

  # Truncation guard: a near-full-length chunk whose transcript is far below
  # speaking pace means the model hit its output ceiling and dropped the rest.
  words=$(printf '%s' "$text" | wc -w | tr -d ' ')
  min_expected=$(echo "$dur / 60 * $MIN_WPM" | bc -l | cut -d. -f1)
  if [ "$(echo "$dur > 120" | bc -l)" -eq 1 ] && [ "${words:-0}" -lt "${min_expected:-0}" ]; then
    echo "error: ${name} looks truncated (${words} words for ${dur%s}s, floor ${min_expected})." >&2
    echo "       Re-run with smaller CHUNK_SECONDS, or MIN_WPM=0 if the audio is mostly silence." >&2
    truncated=1
  fi

  printf '%s ' "$text" >> "$OUT"
done

# Collapse the chunk-join spaces into a single clean body.
python3 - "$OUT" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
t = re.sub(r"[ \t]+", " ", t)
t = re.sub(r"\n{3,}", "\n\n", t).strip()
open(p, "w", encoding="utf-8").write(t + "\n")
PY

echo "wrote $OUT ($(wc -w < "$OUT" | tr -d ' ') words)"
[ "$fell_back" -eq 0 ] || echo "NOTE: fallback model ${FALLBACK_MODEL} was used -- verify names/duplication before filing" >&2
[ "$truncated" -eq 0 ] || { echo "TRUNCATED -- do not file this transcript" >&2; exit 1; }
[ "$failed" -eq 0 ] || { echo "$failed chunk(s) failed -- transcript has gaps" >&2; exit 1; }
