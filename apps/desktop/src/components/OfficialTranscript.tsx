import type { AnalysisResult } from "@memecho/contracts";
import { useEffect, useMemo, useState } from "react";
import { gateway, type TranscriptSnippet } from "../lib/api";

export interface OfficialTranscriptEmbed {
  segments: TranscriptSnippet[];
  truncated: boolean;
}

/** Read the bounded official transcript persisted with the report JSON. */
export function readEmbeddedTranscript(
  result: AnalysisResult | null | undefined,
): OfficialTranscriptEmbed | null {
  const embedded = (result as unknown as Record<string, unknown> | null)?.[
    "_official_transcript"
  ];
  if (!embedded || typeof embedded !== "object") return null;
  const candidate = embedded as { segments?: unknown; truncated?: unknown };
  if (!Array.isArray(candidate.segments)) return null;
  const segments: TranscriptSnippet[] = [];
  for (const item of candidate.segments) {
    if (!item || typeof item !== "object") continue;
    const segment = item as Partial<TranscriptSnippet>;
    if (typeof segment.text !== "string") continue;
    segments.push({
      speaker_id:
        typeof segment.speaker_id === "string" ? segment.speaker_id : "unknown",
      start_ms: Number.isFinite(segment.start_ms) ? Number(segment.start_ms) : 0,
      end_ms: Number.isFinite(segment.end_ms) ? Number(segment.end_ms) : 0,
      text: segment.text.slice(0, 600),
    });
  }
  return { segments, truncated: candidate.truncated === true };
}

function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function OfficialTranscript({
  result,
  sessionId,
  speakerNames,
}: {
  result: AnalysisResult;
  sessionId: string | null;
  speakerNames: Map<string, string>;
}) {
  const embedded = useMemo(() => readEmbeddedTranscript(result), [result]);
  const [segments, setSegments] = useState<TranscriptSnippet[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    // The report-persisted transcript is authoritative: it survives gateway
    // restarts and works when reopening historical reports offline.
    if (embedded) {
      setSegments(embedded.segments);
      setTruncated(embedded.truncated);
      setError("");
      return;
    }
    if (!sessionId) {
      setSegments(null);
      return;
    }
    let cancelled = false;
    gateway
      .processingDetails(sessionId)
      .then((details) => {
        if (cancelled) return;
        setSegments(details.transcript_segments);
        setTruncated(details.transcript_truncated);
        setError("");
      })
      .catch(() => {
        if (cancelled) return;
        setSegments(null);
        setError("正式转写不可用：该报告未保存正式转写，且网关未保留会话处理状态。");
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, embedded]);

  return (
    <details className="glass-card span-two official-transcript">
      <summary>
        正式转写 · FileTrans
        <small>会后正式转写，与实时临时字幕不同</small>
      </summary>
      <p className="boundary">
        以下为会后 FileTrans 正式转写结果；实时字幕仅是会议中的临时提示，不作为报告证据。
      </p>
      {error && <p className="empty-analysis">{error}</p>}
      {!error && segments === null && <p className="empty-analysis">正在读取正式转写…</p>}
      {!error && segments !== null && segments.length === 0 && (
        <p className="empty-analysis">本次没有可用的正式转写片段。</p>
      )}
      {segments !== null && segments.length > 0 && (
        <ol className="official-transcript-list">
          {segments.map((segment, index) => (
            <li key={`${segment.start_ms}-${index}`}>
              <time>
                {formatClock(segment.start_ms)}–{formatClock(segment.end_ms)}
              </time>
              <b>{speakerNames.get(segment.speaker_id) ?? segment.speaker_id}</b>
              <p>{segment.text}</p>
            </li>
          ))}
        </ol>
      )}
      {truncated && (
        <p className="empty-analysis">片段过多，仅展示前一部分。</p>
      )}
    </details>
  );
}
