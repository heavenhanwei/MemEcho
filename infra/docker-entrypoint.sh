#!/bin/sh
set -eu

required_vars="
MEMECHO_PROVIDER
MEMECHO_DEMO_TOKEN
MEMECHO_PUBLIC_BASE_URL
BAILIAN_TEXT_BASE_URL
BAILIAN_TEXT_API_KEY
BAILIAN_TEXT_MODEL
BAILIAN_AUDIO_BASE_URL
BAILIAN_AUDIO_API_KEY
BAILIAN_REALTIME_WS_URL
BAILIAN_REALTIME_MODEL
BAILIAN_DIARIZATION_MODEL
BAILIAN_EMOTION_MODEL
OSS_ENDPOINT
OSS_BUCKET
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
OSS_PREFIX
CHUNK_SIZE_BYTES
MAX_SESSION_SECONDS
"

for name in ${required_vars}; do
  eval "value=\${${name}:-}"
  if [ -z "${value}" ]; then
    echo "memEcho gateway configuration error: ${name} is required" >&2
    exit 78
  fi
done

if [ "${MEMECHO_PROVIDER}" != "bailian" ]; then
  echo "memEcho gateway configuration error: production image requires MEMECHO_PROVIDER=bailian" >&2
  exit 78
fi

if [ "${#MEMECHO_DEMO_TOKEN}" -lt 32 ]; then
  echo "memEcho gateway configuration error: MEMECHO_DEMO_TOKEN must be at least 32 characters" >&2
  exit 78
fi

case "${MEMECHO_PUBLIC_BASE_URL}" in
  https://*) ;;
  *)
    echo "memEcho gateway configuration error: MEMECHO_PUBLIC_BASE_URL must use HTTPS" >&2
    exit 78
    ;;
esac

case "${BAILIAN_TEXT_BASE_URL}" in
  https://*) ;;
  *)
    echo "memEcho gateway configuration error: BAILIAN_TEXT_BASE_URL must use HTTPS" >&2
    exit 78
    ;;
esac

case "${BAILIAN_AUDIO_BASE_URL}" in
  https://*) ;;
  *)
    echo "memEcho gateway configuration error: BAILIAN_AUDIO_BASE_URL must use HTTPS" >&2
    exit 78
    ;;
esac

case "${BAILIAN_REALTIME_WS_URL}" in
  wss://*) ;;
  *)
    echo "memEcho gateway configuration error: BAILIAN_REALTIME_WS_URL must use WSS" >&2
    exit 78
    ;;
esac

case "${OSS_ENDPOINT}" in
  https://*) ;;
  *)
    echo "memEcho gateway configuration error: OSS_ENDPOINT must use HTTPS" >&2
    exit 78
    ;;
esac

exec uvicorn memecho_gateway.main:app "$@"
