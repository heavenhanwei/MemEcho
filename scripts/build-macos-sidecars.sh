#!/usr/bin/env bash
set -euo pipefail

# Build the two native executables embedded in memEcho.app. This script must
# run on macOS; PyInstaller and Swift binaries cannot be cross-compiled from
# Windows into a distributable Mac application.

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
gateway_source="$repo_root/services/gateway/src"
gateway_entry="$gateway_source/memecho_gateway/__main__.py"
python="$repo_root/.venv/bin/python"
build_root="$repo_root/.runtime/macos-sidecar-build"
out_dir="$repo_root/apps/desktop/src-tauri/binaries"
swift_source="$repo_root/apps/desktop/src-tauri/macos/MemEchoAudioCapture.swift"

case "$(uname -m)" in
  arm64) target_triple="aarch64-apple-darwin" ;;
  x86_64) target_triple="x86_64-apple-darwin" ;;
  *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 2 ;;
esac

if [[ ! -x "$python" ]]; then
  echo "Missing repository virtualenv: $python" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e 'services/gateway[dev,packaging]'" >&2
  exit 2
fi
if [[ ! -f "$gateway_entry" || ! -f "$swift_source" ]]; then
  echo "Gateway entry point or Swift audio helper source is missing" >&2
  exit 2
fi

mkdir -p "$build_root/dist" "$build_root/work" "$build_root/spec" "$out_dir"

"$python" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name memecho-gateway \
  --paths "$gateway_source" \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  --specpath "$build_root/spec" \
  --collect-submodules memecho_gateway \
  --collect-submodules uvicorn \
  --collect-submodules websockets \
  "$gateway_entry"

gateway_target="$out_dir/memecho-gateway-$target_triple"
audio_target="$out_dir/memecho-audio-capture-$target_triple"
cp "$build_root/dist/memecho-gateway" "$gateway_target"

MACOSX_DEPLOYMENT_TARGET=13.0 xcrun swiftc \
  -O \
  -emit-executable \
  "$swift_source" \
  -framework AVFoundation \
  -framework CoreMedia \
  -framework ScreenCaptureKit \
  -o "$audio_target"

chmod 755 "$gateway_target" "$audio_target"
shasum -a 256 "$gateway_target" "$audio_target"
echo "macOS sidecars ready for $target_triple"
