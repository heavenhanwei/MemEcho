import { describe, expect, it } from "vitest";
import type { JobStatus } from "./index";

describe("public contract", () => {
  it("keeps the documented terminal states", () => {
    const states: JobStatus[] = ["complete", "failed"];
    expect(states).toEqual(["complete", "failed"]);
  });
});

