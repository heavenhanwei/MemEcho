import type { AnalysisResult } from "@memecho/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayApiError, gateway, type GatewayJob } from "./api";

function responseJson(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function progress(status: GatewayJob["status"], progressValue: number): GatewayJob {
  return {
    id: "job-1",
    session_id: "session-1",
    request_id: "request-1",
    status,
    progress: progressValue,
    stage_label: status,
    retryable: false,
    error_code: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("gateway session analysis APIs", () => {
  it("uploads a browser recording through the resumable media contract", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({
        upload_id: "upload-1", chunk_size: 2, received_chunks: [],
      }))
      .mockResolvedValueOnce(responseJson({ ok: true, index: 0 }))
      .mockResolvedValueOnce(responseJson({ ok: true, index: 1 }))
      .mockResolvedValueOnce(responseJson({
        upload_id: "upload-1", path: "asset://upload-1", size: 3, sha256: "ignored",
      }));
    vi.stubGlobal("fetch", fetchMock);

    await gateway.uploadBlob(
      "session-1",
      new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" }),
      "microphone",
    );

    const declaration = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(declaration).toMatchObject({
      track: "microphone", file_name: "browser-microphone.webm", size: 3,
    });
    expect(declaration.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(fetchMock.mock.calls[1][0]).toContain("/chunks/0");
    expect(fetchMock.mock.calls[2][0]).toContain("/chunks/1");
    expect(fetchMock.mock.calls[1][1].headers).toMatchObject({
      "Content-Type": "application/octet-stream",
    });
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({
      upload_id: "upload-1", sha256: declaration.sha256,
    });
  });

  it("includes imported text as the text-only source contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseJson({
      id: "job-1", session_id: "session-1", request_id: "request-1",
      status: "queued", progress: 0, stage_label: "queued", retryable: false,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    }));
    vi.stubGlobal("fetch", fetchMock);
    await gateway.analyze("session-1", "request-1", {
      type: "text", text: "事实、观点与态度",
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.source).toEqual({ type: "text", text: "事实、观点与态度" });
    expect(body.focus).toContain("content_analysis");
  });

  it("loads candidates, resolves snake_case identity, artifacts, and result", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({ candidates: [{
        participant_id: "speaker_1",
        display_name: "Speaker 1",
        source: "diarization",
        speaking_time_ms: 1200,
        segment_count: 2,
      }] }))
      .mockResolvedValueOnce(responseJson({ ok: true }))
      .mockResolvedValueOnce(responseJson({
        artifacts: {},
        contents: { json: "{}", markdown: "# Report", html: "<h1>Report</h1>" },
      }))
      .mockResolvedValueOnce(responseJson({ schema_version: "1.1" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(gateway.participantCandidates("session/1")).resolves.toMatchObject({
      candidates: [{ participant_id: "speaker_1", speaking_time_ms: 1200 }],
    });
    const resolution = {
      participants: [{ id: "speaker_1", name: "Me", is_self: true }],
      self_participant_id: "speaker_1",
      identity_basis: "user_confirmed" as const,
    };
    await expect(gateway.resolveParticipants("session/1", resolution)).resolves.toEqual({ ok: true });
    await expect(gateway.artifacts("session/1")).resolves.toMatchObject({
      contents: { markdown: "# Report" },
    });
    await gateway.result("session/1");

    expect(fetchMock.mock.calls[0][0]).toContain("/sessions/session%2F1/participants/candidates");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual(resolution);
    expect(fetchMock.mock.calls[2][0]).toContain("/sessions/session%2F1/artifacts");
    expect(fetchMock.mock.calls[3][0]).toContain("/sessions/session%2F1/result");
  });

  it("reads sanitized processing details for the session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseJson({
      session_id: "session-1",
      updated_at: "2026-01-01T00:00:00Z",
      tracks: [],
      aligned_segment_count: 0,
      submitted_to_qwen: false,
      qwen_status: "queued",
      qwen_error_code: null,
      transcript_segments: [],
      transcript_truncated: false,
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(gateway.processingDetails("session/1")).resolves.toMatchObject({
      session_id: "session-1",
      qwen_status: "queued",
    });
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/sessions/session%2F1/processing-details",
    );
  });

  it("keeps HTTP errors bounded and redacts gateway and Bearer tokens", async () => {
    const detail = `change-me Bearer super-secret ${"x".repeat(2000)}`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseJson({ detail }, 502)));

    const error = await gateway.job("job-1").catch((value: unknown) => value);
    expect(error).toBeInstanceOf(GatewayApiError);
    if (!(error instanceof GatewayApiError)) throw error;
    expect(error.status).toBe(502);
    expect(error.message).not.toContain("change-me");
    expect(error.message).not.toContain("super-secret");
    expect(error.message).toContain("[REDACTED]");
    expect(error.message.length).toBeLessThan(400);
  });
});

describe("gateway jobEvents", () => {
  it("parses progress SSE across chunks", async () => {
    const first = JSON.stringify(progress("transcribing", 30));
    const second = JSON.stringify(progress("complete", 100));
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${first}\n\nda`));
        controller.enqueue(encoder.encode(`ta: ${second}\n\n`));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, { status: 200 })));
    const events: GatewayJob[] = [];

    await gateway.jobEvents("job-1", (event) => events.push(event));

    expect(events.map((event) => [event.status, event.progress])).toEqual([
      ["transcribing", 30],
      ["complete", 100],
    ]);
  });

  it("surfaces malformed SSE as a safe typed failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("data: not-json\n\n")));
    await expect(gateway.jobEvents("job-1", vi.fn())).rejects.toMatchObject({
      name: "GatewayApiError",
      status: 200,
    });
  });

  it("passes an external abort signal and stops on AbortController cancellation", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        if (init?.signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const pending = gateway.jobEvents("job-1", vi.fn(), controller.signal);

    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
  });
});
describe("gateway chat evidence context", () => {
  it("sends only the selected evidence IDs and streams the reply", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('data: {"delta":"回"}\n\ndata: {"delta":"声"}\n\n'),
    );
    vi.stubGlobal("fetch", fetchMock);
    const deltas: string[] = [];

    await gateway.chat(
      "这里为什么是分歧？",
      {} as AnalysisResult,
      (delta) => deltas.push(delta),
      ["ev_2", "ev_4"],
    );

    expect(deltas).toEqual(["回", "声"]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({
      evidence_ids: ["ev_2", "ev_4"],
    });
  });

  it("sends an explicit empty evidence_ids array when nothing is selected", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('data: {"done":true}\n\n'));
    vi.stubGlobal("fetch", fetchMock);

    await gateway.chat("概括一下", {} as AnalysisResult, vi.fn());

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).evidence_ids).toEqual([]);
  });
});
