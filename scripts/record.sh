#!/usr/bin/env bash
# Record the narrated demo walkthrough (Playwright + Chromium, 1440x900) against
# the running stack (`make up`, which includes the labelled prompt-only overlay).
#
#   1. Synthesise one narration clip per segment with macOS `say` (offline).
#   2. Run the walkthrough; it waits each clip's exact length and logs when it started.
#   3. Lay the clips onto the video at those offsets with ffmpeg.
#
# Outputs: recordings/governed-agent-demo.mp4 (H.264/AAC) and .webm (VP8/Opus).
# Without `say` or `ffmpeg` it still records a captioned, silent video.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=recordings
RAW=$OUT/raw
AUDIO=$OUT/narration  # outside $RAW: Playwright empties its output dir
rm -rf "$RAW" "$AUDIO"
mkdir -p "$AUDIO"

NARRATION=e2e/recording/narration.json
have_audio=0
if command -v say >/dev/null && command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  have_audio=1
  voice=${VOICE:-$(python3 -c "import json;print(json.load(open('$NARRATION'))['voice'])")}
  rate=$(python3 -c "import json;print(json.load(open('$NARRATION'))['rate'])")
  echo "synthesising narration (voice: $voice)"
  python3 - "$NARRATION" "$AUDIO" <<'EOF'
import json, sys
segments = json.load(open(sys.argv[1]))["segments"]
for s in segments:
    open(f"{sys.argv[2]}/{s['id']}.txt", "w").write(s["text"])
print("\n".join(s["id"] for s in segments), file=open(f"{sys.argv[2]}/order.txt", "w"))
EOF
  durations="{"
  sep=""
  while read -r id; do
    say -v "$voice" -r "$rate" -o "$AUDIO/$id.aiff" -f "$AUDIO/$id.txt"
    ffmpeg -loglevel error -y -i "$AUDIO/$id.aiff" -ar 48000 -ac 1 "$AUDIO/$id.wav"
    ms=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$AUDIO/$id.wav" | python3 -c "import sys;print(round(float(sys.stdin.read())*1000))")
    durations+="$sep\"$id\": $ms"
    sep=", "
  done <"$AUDIO/order.txt"
  echo "$durations}" >"$AUDIO/durations.json"
  export NARRATION_DURATIONS="$PWD/$AUDIO/durations.json"
  export NARRATION_TIMELINE="$PWD/$AUDIO/timeline.json"
else
  echo "say/ffmpeg not found: recording a captioned video without voice-over" >&2
fi

(cd e2e && npx playwright test --config playwright.record.config.ts)
video=$(find "$RAW" -name 'video.webm' | head -1)
[[ -n "$video" ]] || { echo "no video produced" >&2; exit 1; }

if [[ $have_audio -eq 0 ]]; then
  cp "$video" "$OUT/governed-agent-demo.webm"
  echo "recorded (silent): $OUT/governed-agent-demo.webm"
  exit 0
fi

# Build an ffmpeg filter that delays each clip to its logged start and mixes them.
read -r inputs filter < <(python3 - "$AUDIO" <<'EOF'
import json, sys
d = sys.argv[1]
timeline = json.load(open(f"{d}/timeline.json"))
inputs, parts, labels = [], [], []
for i, seg in enumerate(timeline, start=1):
    inputs.append(f"{d}/{seg['id']}.wav")
    parts.append(f"[{i}:a]adelay={seg['offset_ms']}|{seg['offset_ms']}[a{i}]")
    labels.append(f"[a{i}]")
filt = ";".join(parts) + ";" + "".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest[aout]"
print(",".join(inputs), filt)
EOF
)
args=(-loglevel error -y -i "$video")
IFS=',' read -ra files <<<"$inputs"
for f in "${files[@]}"; do args+=(-i "$f"); done

echo "mixing narration onto video"
ffmpeg "${args[@]}" -filter_complex "$filter" -map 0:v -map "[aout]" \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -movflags +faststart \
  "$OUT/governed-agent-demo.mp4"
ffmpeg "${args[@]}" -filter_complex "$filter" -map 0:v -map "[aout]" \
  -c:v copy -c:a libopus -b:a 128k "$OUT/governed-agent-demo.webm"
for f in "$OUT/governed-agent-demo.mp4" "$OUT/governed-agent-demo.webm"; do
  echo "recorded: $f ($(du -h "$f" | cut -f1), $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" | cut -d. -f1)s)"
done
