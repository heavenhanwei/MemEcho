// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ProcessingDetails } from "../lib/api";
import { ProcessingDetailsPanel } from "./ProcessingDetailsPanel";

afterEach(() => {
  cleanup();
});

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

  it("shows upload failure status and OSS status for a failed track", () => {
    const details = baseDetails();
    details.tracks[0].upload_status = "failed";
    details.tracks[0].oss_status = "queued";
    details.tracks[0].filetrans.status = "queued";
    details.tracks[0].modules["transcription"].status = "queued";
    render(<ProcessingDetailsPanel details={details} />);
    // Upload row shows failed
    const uploadRow = screen.getByText("Gateway 上传 · mixed").closest(".processing-detail-row");
    expect(uploadRow).toHaveClass("stage-failed");
    // OSS row shows queued
    const ossRow = screen.getByText("OSS 临时媒体").closest(".processing-detail-row");
    expect(ossRow).toHaveClass("stage-queued");
    // FileTrans shows queued (not failed)
    const ftRow = screen.getByText("FileTrans 正式转写").closest(".processing-detail-row");
    expect(ftRow).toHaveClass("stage-queued");
  });

  it("shows error code for a failed Fun-ASR module", () => {
    const details = baseDetails();
    details.tracks[0].modules["fun_asr"] = {
      status: "failed",
      error_code: "upstream_connection_error",
      elapsed_ms: 5000,
    };
    render(<ProcessingDetailsPanel details={details} />);
    expect(screen.getByText("Fun-ASR 说话人分离")).toBeInTheDocument();
    const funAsrRow = screen.getByText("Fun-ASR 说话人分离").closest(".processing-detail-row");
    expect(funAsrRow).toHaveClass("stage-failed");
    expect(funAsrRow).toHaveTextContent("upstream_connection_error");
  });

  it("shows error code for a failed emotion module", () => {
    const details = baseDetails();
    details.tracks[0].modules["emotion"] = {
      status: "failed",
      error_code: "upstream_timeout",
      elapsed_ms: 900,
    };
    render(<ProcessingDetailsPanel details={details} />);
    expect(screen.getByText("情绪识别")).toBeInTheDocument();
    const emotionRow = screen.getByText("情绪识别").closest(".processing-detail-row");
    expect(emotionRow).toHaveClass("stage-failed");
    expect(emotionRow).toHaveTextContent("upstream_timeout");
  });
});
