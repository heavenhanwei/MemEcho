// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EchoSphere } from "./EchoSphere";

const { webGLModuleLoaded } = vi.hoisted(() => ({
  webGLModuleLoaded: vi.fn(),
}));

vi.mock("./EchoSphereWebGL", () => {
  webGLModuleLoaded();
  return {
    EchoSphereWebGL: () => (
      <div data-testid="sphere-webgl" aria-label="memEcho WebGL 活动球体" />
    ),
  };
});

function setRenderingEnvironment({
  reducedMotion,
  webGL,
}: {
  reducedMotion: boolean;
  webGL: boolean;
}) {
  const listeners = new Set<() => void>();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: reducedMotion,
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      addEventListener: (_type: string, listener: () => void) =>
        listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) =>
        listeners.delete(listener),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
  vi.stubGlobal("WebGLRenderingContext", webGL ? class {} : undefined);
  vi.stubGlobal("WebGL2RenderingContext", webGL ? class {} : undefined);
  Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
    configurable: true,
    value: vi.fn((kind: string) =>
      webGL && (kind === "webgl" || kind === "webgl2") ? {} : null,
    ),
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("EchoSphere deferred WebGL loading", () => {
  it("does not load the Three module when reduced motion is preferred", () => {
    setRenderingEnvironment({ reducedMotion: true, webGL: true });

    const { container } = render(<EchoSphere state="idle" energy={0} />);

    expect(screen.getByTestId("sphere-static")).toBeInTheDocument();
    expect(container.firstElementChild).toHaveAttribute("data-renderer", "static");
    expect(webGLModuleLoaded).not.toHaveBeenCalled();
  });

  it("does not load the Three module when WebGL is unavailable", () => {
    setRenderingEnvironment({ reducedMotion: false, webGL: false });

    render(<EchoSphere state="recording" energy={0.4} />);

    expect(screen.getByTestId("sphere-static")).toBeInTheDocument();
    expect(webGLModuleLoaded).not.toHaveBeenCalled();
  });

  it("shows the immediate fallback, then loads WebGL only on a capable client", async () => {
    setRenderingEnvironment({ reducedMotion: false, webGL: true });
    const onActivate = vi.fn();

    const { container } = render(
      <EchoSphere state="idle" energy={0.2} onActivate={onActivate} />,
    );

    expect(screen.getByTestId("sphere-static")).toBeInTheDocument();
    expect(container.firstElementChild).toHaveAttribute(
      "data-renderer",
      "webgl-lazy",
    );
    fireEvent.click(screen.getByRole("button", { name: "开始录音" }));
    expect(onActivate).toHaveBeenCalledTimes(1);

    expect(await screen.findByTestId("sphere-webgl")).toBeInTheDocument();
    expect(webGLModuleLoaded).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("sphere-static")).not.toBeInTheDocument();
  });
});
