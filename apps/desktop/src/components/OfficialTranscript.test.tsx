// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { gateway, type ProcessingDetails } from "../lib/api";
import { OfficialTranscript } from "./OfficialTranscript";

function details(overrides: Partial<ProcessingDetails> = {}): ProcessingDetails {
  return {
    session_id: "ses_1",
    updated_at: "2026-08-08T10:00:00Z",
    tracks: [],
    aligned_segment_count: 2,
    submitted_to_qwen: true,
    qwen_status: "succeeded",
    qwen_error_code: null,
    transcript_segments: [
      { speaker_id: "speaker_self", start_ms: 12000, end_ms: 18000, text: "我想先确认一下今天讨论的范围。" },
      { speaker_id: "speaker_2", start_ms: 18000, end_ms: 24000, text: "我同意先处理时间节点。" },
    ],
    transcript_truncated: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OfficialTranscript", () => {
  it("renders segments with time ranges and speaker names, marked as official", async () => {
    vi.spyOn(gateway, "processingDetails").mockResolvedValue(details());
    render(
      <OfficialTranscript
        sessionId="ses_1"
        speakerNames={new Map([["speaker_self", "我"], ["speaker_2", "参与者 B"]])}
      />,
    );

    expect(
      screen.getByText("正式转写 · FileTrans", { exact: false }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("我想先确认一下今天讨论的范围。")).toBeInTheDocument();
    });
    expect(screen.getByText("00:12–00:18")).toBeInTheDocument();
    expect(screen.getByText("00:18–00:24")).toBeInTheDocument();
    expect(screen.getByText("我")).toBeInTheDocument();
    expect(screen.getByText("参与者 B")).toBeInTheDocument();
    expect(
      screen.getByText(
        "以下为会后 FileTrans 正式转写结果；实时字幕仅是会议中的临时提示，不作为报告证据。",
      ),
    ).toBeInTheDocument();
  });

  it("shows a graceful note when the gateway no longer holds the session", async () => {
    vi.spyOn(gateway, "processingDetails").mockRejectedValue(new Error("404"));
    render(<OfficialTranscript sessionId="ses_gone" speakerNames={new Map()} />);
    await waitFor(() => {
      expect(
        screen.getByText("正式转写不可用：网关未保留该会话的处理状态。"),
      ).toBeInTheDocument();
    });
  });

  it("does not fetch without a gateway session", () => {
    const spy = vi.spyOn(gateway, "processingDetails");
    render(<OfficialTranscript sessionId={null} speakerNames={new Map()} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
