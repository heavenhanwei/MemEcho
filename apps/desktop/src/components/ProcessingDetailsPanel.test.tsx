// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ProcessingDetails } from "../lib/api";
import { ProcessingDetailsPanel } from "./ProcessingDetailsPanel";

function baseDetails(overrides: Partial<ProcessingDetails> = {}): ProcessingDetails {
  return {
    session_id: "ses_1",
    updated_at: "2026-08-08T10:00:00Z",
    tracks: [
      {
        upload_id: "upl_1",
        file_name: "browser-mixed.webm",
        track: "mixed",
        mime_type: "audio/webm",
        size_bytes: 1024,
        upload_status: "succeeded",
        received_chunks: 3,
        expected_chunks: 3,
        oss_status: "succeeded",
        modules: {
          fun_asr: { status: "succeeded", error_code: null, elapsed_ms: 1200 },
          emotion: { status: "failed", error_code: "upstream_timeout", elapsed_ms: 900 },
          transcription: { status: "succeeded", error_code: null, elapsed_ms: 3000 },
        },
        filetrans: {
          status: "succeeded",
          error_code: null,
          elapsed_ms: 3000,
          sentence_count: 438,
          language: "zh",
          audio_duration_ms: 37800,
        },
      },
    ],
    aligned_segment_count: 421,
    submitted_to_qwen: true,
    qwen_status: "succeeded",
    qwen_error_code: null,
    transcript_segments: [],
    transcript_truncated: false,
    ...overrides,
  };
}

describe("ProcessingDetailsPanel", () => {
  it("renders nothing without details or tracks", () => {
    const { container } = render(<ProcessingDetailsPanel details={null} />);
    expect(container.firstChild).toBeNull();
    const empty = render(
      <ProcessingDetailsPanel details={baseDetails({ tracks: [] })} />,
    );
    expect(empty.container.firstChild).toBeNull();
  });

  it("shows pipeline stages with counters", () => {
    render(<ProcessingDetailsPanel details={baseDetails()} />);
    expect(screen.getByText("PIPELINE · 会后处理详情")).toBeInTheDocument();
    expect(screen.getByText("Gateway 上传 · mixed")).toBeInTheDocument();
    expect(screen.getByText("3/3 分块 · 1.0 KB")).toBeInTheDocument();
    expect(screen.getByText("FileTrans 正式转写")).toBeInTheDocument();
    expect(screen.getByText("438 句 · 37.8 秒 · 耗时 3.0 秒")).toBeInTheDocument();
    expect(screen.getByText("421 个片段")).toBeInTheDocument();
    expect(screen.getByText("已接收 421 个片段")).toBeInTheDocument();
  });

  it("shows module name, stable error code, and retry hint on FileTrans failure", () => {
    const details = baseDetails({
      aligned_segment_count: 0,
      submitted_to_qwen: true,
    });
    details.tracks[0].filetrans = {
      status: "failed",
      error_code: "upstream_task_failed",
      elapsed_ms: 800,
      sentence_count: null,
      language: null,
      audio_duration_ms: null,
    };
    render(<ProcessingDetailsPanel details={details} />);
    expect(
      screen.getByText("正式转写模块失败（稳定错误码：upstream_task_failed）。对齐不会伪造片段；检查上游配置或网络后可重试当前步骤。"),
    ).toBeInTheDocument();
  });
});
