import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { SoulState } from "../store";

export interface EchoSphereVisualProps {
  state: SoulState;
  energy: number;
}

const LazyEchoSphereWebGL = lazy(async () => {
  const module = await import("./EchoSphereWebGL");
  return { default: module.EchoSphereWebGL };
});

function prefersReducedMotion() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(prefersReducedMotion);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    update();
    if (typeof query.addEventListener === "function") {
      query.addEventListener("change", update);
      return () => query.removeEventListener("change", update);
    }
    query.addListener(update);
    return () => query.removeListener(update);
  }, []);

  return reduced;
}

function supportsWebGL() {
  if (
    typeof window === "undefined" ||
    typeof document === "undefined" ||
    (typeof window.WebGLRenderingContext === "undefined" &&
      typeof window.WebGL2RenderingContext === "undefined")
  ) {
    return false;
  }

  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

function StaticSphere() {
  return (
    <div className="sphere-static" data-testid="sphere-static">
      <div className="sphere-fallback" aria-label="memEcho 活动球体" />
    </div>
  );
}

class SphereLoadBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export function EchoSphere({
  state,
  energy,
  onActivate,
}: EchoSphereVisualProps & {
  onActivate?: () => void;
}) {
  const reducedMotion = usePrefersReducedMotion();
  const [webGLAvailable] = useState(supportsWebGL);
  const fallback = <StaticSphere />;
  const useWebGL = webGLAvailable && !reducedMotion;

  return (
    <div
      className={`sphere-stage is-${state}`}
      data-renderer={useWebGL ? "webgl-lazy" : "static"}
    >
      <div className="echo-shell shell-one" />
      <div className="echo-shell shell-two" />
      {useWebGL ? (
        <SphereLoadBoundary fallback={fallback}>
          <Suspense fallback={fallback}>
            <LazyEchoSphereWebGL state={state} energy={energy} />
          </Suspense>
        </SphereLoadBoundary>
      ) : (
        fallback
      )}
      {onActivate && (
        <button
          className="sphere-hit"
          aria-label={state === "idle" ? "开始录音" : "memEcho 当前状态"}
          onClick={onActivate}
        />
      )}
      <div className="sphere-glint" />
    </div>
  );
}
