import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("@react-three/fiber", () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  useFrame: () => undefined,
}));
vi.mock("@react-three/drei", () => ({
  Float: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Html: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

describe("App", () => {
  it("shows the recording entry without dot particles", () => {
    render(<App />);
    expect(screen.getByText("点击球体，开始录音")).toBeInTheDocument();
    expect(screen.getByText("麦克风＋系统声音")).toBeInTheDocument();
  });
});

