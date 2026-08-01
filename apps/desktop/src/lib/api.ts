import type {
  AnalysisResult,
  JobStatus,
  RealtimeEvent,
} from "@memecho/contracts";

const baseUrl = import.meta.env.VITE_GATEWAY_URL ?? "http://127.0.0.1:8787";
const token = import.meta.env.VITE_GATEWAY_TOKEN ?? "change-me";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...init.headers,
    },
  });
  if (!response.ok) {
    throw new Error(`${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export interface GatewaySession {
  id: string;
  request_id: string;
  status: JobStatus;
}

export const gateway = {
  health: () => request<{ status: string; provider: string }>("/v1/health"),
  createSession: (title: string, sourceMode: string) =>
    request<GatewaySession>("/v1/sessions", {
      method: "POST",
      body: JSON.stringify({
        title,
        context: "工作",
        occurred_at: new Date().toISOString(),
        source_mode: sourceMode,
        marks: [],
      }),
    }),
  analyze: (sessionId: string, requestId: string) =>
    request<{ id: string; status: JobStatus }>(
      `/v1/sessions/${sessionId}/analyze`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "1.1",
          request_id: requestId,
          focus: ["minutes", "content_analysis", "vad", "self_echo"],
          memory_mode: "off",
          language: "zh-CN",
        }),
      },
    ),
  job: (jobId: string) =>
    request<{
      id: string;
      status: JobStatus;
      progress: number;
      stage_label: string;
      error_code?: string;
    }>(`/v1/jobs/${jobId}`),
  result: (sessionId: string) =>
    request<AnalysisResult>(`/v1/sessions/${sessionId}/result`),
  chat: async (
    question: string,
    result: AnalysisResult,
    onDelta: (value: string) => void,
  ) => {
    const response = await fetch(`${baseUrl}/v1/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ question, result, evidence_ids: [] }),
    });
    if (!response.ok || !response.body) throw new Error("追问连接失败");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const packets = buffer.split("\n\n");
      buffer = packets.pop() ?? "";
      for (const packet of packets) {
        const line = packet.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        if (event.delta) onDelta(event.delta);
      }
    }
  },
  liveUrl: (sessionId: string) =>
    `${baseUrl.replace(/^http/, "ws")}/v1/sessions/${sessionId}/live?token=${encodeURIComponent(token)}`,
};

export type { RealtimeEvent };

