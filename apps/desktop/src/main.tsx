import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initGatewayConfig } from "./lib/api";
import "./styles.css";

const gatewayReady = initGatewayConfig();

function Startup() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      gatewayReady.catch(() => undefined),
      new Promise((resolve) => window.setTimeout(resolve, 450)),
    ]).finally(() => {
      if (!cancelled) setReady(true);
    });
    return () => { cancelled = true; };
  }, []);

  if (ready) return <App />;
  return (
    <div className="startup-screen" role="status" aria-live="polite">
      <div className="startup-orb" aria-hidden="true" />
      <img src="/brand/memecho-wordmark.svg" alt="memEcho" />
      <p>正在唤醒本地回声…</p>
      <div className="startup-progress"><span /></div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Startup />
  </StrictMode>,
);
