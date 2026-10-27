#!/usr/bin/env bash
# Record the silent, caption-driven "Agentic AI Security" presentation against
# the running stack (make up, which includes the unprotected overlay).
# Output: recordings/agentic-ai-security.mp4 (+ .webm).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=recordings
RAW=$OUT/present-raw
rm -rf "$RAW"
(cd e2e && npx playwright test --config playwright.present.config.ts)
video=$(find "$RAW" -name 'video.webm' | head -1)
[[ -n "$video" ]] || { echo "no video produced" >&2; exit 1; }
if command -v ffmpeg >/dev/null; then
  ffmpeg -loglevel error -y -i "$video" -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -movflags +faststart \
    "$OUT/agentic-ai-security.mp4"
  cp "$video" "$OUT/agentic-ai-security.webm"
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/agentic-ai-security.mp4" | cut -d. -f1)
  echo "recorded: $OUT/agentic-ai-security.mp4 ($(du -h "$OUT/agentic-ai-security.mp4" | cut -f1), ${dur}s)"
else
  cp "$video" "$OUT/agentic-ai-security.webm"
  echo "recorded: $OUT/agentic-ai-security.webm (install ffmpeg for mp4)"
fi
