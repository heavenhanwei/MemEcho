import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Production build gate: if NODE_ENV=production and no VITE_GATEWAY_URL is set,
// fail the build to prevent silent localhost fallback in installer builds.
function productionGatewayCheck() {
  return {
    name: "production-gateway-check",
    configResolved(config: { command: string; mode: string }) {
      const isProductionBuild =
        config.command === "build" && config.mode === "production";
      const processEnv = (
        globalThis as unknown as {
          process?: { cwd?: () => string; env?: Record<string, string | undefined> };
        }
      ).process;
      const env = loadEnv(config.mode, processEnv?.cwd?.() ?? ".", "");
      const gatewayUrl = env.VITE_GATEWAY_URL || processEnv?.env?.VITE_GATEWAY_URL;
      const embeddedToken = env.VITE_GATEWAY_TOKEN || processEnv?.env?.VITE_GATEWAY_TOKEN;
      if (isProductionBuild && !gatewayUrl) {
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
      if (isProductionBuild && !gatewayUrl?.startsWith("https://")) {
        throw new Error("Production VITE_GATEWAY_URL must use HTTPS");
      }
      if (isProductionBuild && embeddedToken) {
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
