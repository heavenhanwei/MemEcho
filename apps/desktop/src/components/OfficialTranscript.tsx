import { useEffect, useState } from "react";
import { gateway, type TranscriptSnippet } from "../lib/api";

function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function OfficialTranscript({
  sessionId,
  speakerNames,
}: {
  sessionId: string | null;
  speakerNames: Map<string, string>;
}) {
  const [segments, setSegments] = useState<TranscriptSnippet[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
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
        setError("正式转写不可用：网关未保留该会话的处理状态。");
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

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
