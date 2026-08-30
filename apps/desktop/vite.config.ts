import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Web deployments require an explicit HTTPS Gateway. Tauri installer builds
// use the managed sidecar discovered over IPC and therefore must not embed a
// fixed Gateway URL or token.
function productionGatewayCheck() {
  return {
    name: "production-gateway-check",
    configResolved(config: { command: string; mode: string }) {
      const isBuild = config.command === "build";
      const isTauriSidecarBuild = isBuild && config.mode === "tauri";
      const processEnv = (
        globalThis as unknown as {
          process?: { cwd?: () => string; env?: Record<string, string | undefined> };
        }
      ).process;
      const env = loadEnv(config.mode, processEnv?.cwd?.() ?? ".", "");
      const gatewayUrl = env.VITE_GATEWAY_URL || processEnv?.env?.VITE_GATEWAY_URL;
      const embeddedToken = env.VITE_GATEWAY_TOKEN || processEnv?.env?.VITE_GATEWAY_TOKEN;
      if (isBuild && !isTauriSidecarBuild && !gatewayUrl) {
        throw new Error(
          "\n" +
            "╔══════════════════════════════════════════════════════════════╗\n" +
            "║  PRODUCTION BUILD REQUIRES VITE_GATEWAY_URL                 ║\n" +
            "║                                                             ║\n" +
            "║  Set VITE_GATEWAY_URL to your HTTPS gateway origin:         ║\n" +
            "║    VITE_GATEWAY_URL=https://gw.example.com pnpm build       ║\n" +
            "║                                                             ║\n" +
            "║  For local development, use `pnpm dev` instead.             ║\n" +
            "╚══════════════════════════════════════════════════════════════╝",
        );
      }
      if (isBuild && gatewayUrl && !gatewayUrl.startsWith("https://")) {
        throw new Error("Production VITE_GATEWAY_URL must use HTTPS");
      }
      if (isBuild && embeddedToken) {
        throw new Error(
          "Production builds must not embed VITE_GATEWAY_TOKEN; provision it through Windows Credential Manager",
        );
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), productionGatewayCheck()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  envPrefix: ["VITE_", "TAURI_"],
  build: {
    target: "es2022",
    sourcemap: true,
  },
});
