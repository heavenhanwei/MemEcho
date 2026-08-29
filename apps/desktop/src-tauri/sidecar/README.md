# Gateway Sidecar packaging skeleton

This directory is the placeholder for the bundled `memecho-gateway` sidecar
executable described in `docs/open-source-edition/gateway-sidecar.md`.

## Expected layout at build time

```text
apps/desktop/src-tauri/
└── binaries/
    └── memecho-gateway-<target-triple>[.exe]   # produced by scripts/build-gateway-sidecar.ps1
```

The desktop supervisor resolves the binary at runtime
(`gateway_supervisor::resolve_sidecar_binary`):

1. `MEMECHO_GATEWAY_SIDECAR` environment override (dev hook);
2. next to the desktop executable;
3. in a `binaries/` subdirectory next to the desktop executable.

## Tauri configuration (enable once packaging lands)

Until the Python gateway can be compiled into a standalone executable, the
`externalBin` entry stays disabled so `tauri build` does not fail on a
missing binary. When packaging is unblocked, add:

```jsonc
// tauri.conf.json
{
  "bundle": {
    // Tauri appends the target triple when resolving external binaries.
    "externalBin": ["binaries/memecho-gateway"]
  }
}
```

## Security rules for this skeleton

- Do NOT commit any `.env`, embedded token, or real API key here or in the
  bundled binary. The sidecar receives its one-time access token at spawn
  time through `MEMECHO_GATEWAY_TOKEN` (memory only).
- Do NOT bake a fixed port; the supervisor picks a random loopback port and
  passes it through `MEMECHO_GATEWAY_PORT`.

## Current blocker

The Python gateway (`services/gateway`) has no standalone-executable build
yet (e.g. PyInstaller/Nuitka with all provider adapters). Until that exists,
`scripts/build-gateway-sidecar.ps1` exits with a documented blocker message,
and the desktop falls back to dev/external gateway mode.
