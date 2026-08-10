import type {
  ProcessingDetails,
  ProcessingStage,
  TrackProcessingDetails,
} from "../lib/api";

const stageLabel: Record<ProcessingStage, string> = {
  queued: "待处理",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

function formatSeconds(ms: number | null | undefined): string | null {
  if (typeof ms !== "number" || ms <= 0) return null;
  return `${(ms / 1000).toFixed(1)} 秒`;
}

function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function StageRow({
  name,
  stage,
  detail,
  errorCode,
}: {
  name: string;
  stage: ProcessingStage;
  detail?: string;
  errorCode?: string | null;
}) {
  return (
    <div className={`processing-detail-row stage-${stage}`}>
      <span className="processing-detail-name">{name}</span>
      <span className="processing-detail-status">{stageLabel[stage]}</span>
      <span className="processing-detail-meta">
        {detail ?? ""}
        {stage === "failed" && errorCode ? ` · ${errorCode}` : ""}
      </span>
    </div>
  );
}

function trackRows(track: TrackProcessingDetails) {
  const rows = [
    <StageRow
      key={`${track.upload_id}-upload`}
      name={`Gateway 上传 · ${track.track}`}
      stage={track.upload_status}
      detail={`${track.received_chunks}/${track.expected_chunks} 分块 · ${formatBytes(track.size_bytes)}`}
    />,
    <StageRow
      key={`${track.upload_id}-oss`}
      name="OSS 临时媒体"
      stage={track.oss_status}
    />,
    <StageRow
      key={`${track.upload_id}-filetrans`}
      name="FileTrans 正式转写"
      stage={track.filetrans.status}
      errorCode={track.filetrans.error_code}
      detail={
        track.filetrans.status === "succeeded"
          ? [
              track.filetrans.sentence_count != null
                ? `${track.filetrans.sentence_count} 句`
                : null,
              formatSeconds(track.filetrans.audio_duration_ms),
              formatSeconds(track.filetrans.elapsed_ms)
                ? `耗时 ${formatSeconds(track.filetrans.elapsed_ms)}`
                : null,
            ]
              .filter(Boolean)
              .join(" · ")
          : undefined
      }
    />,
  ];
  const funAsr = track.modules["fun_asr"];
  if (funAsr) {
    rows.push(
      <StageRow
        key={`${track.upload_id}-funasr`}
        name="Fun-ASR 说话人分离"
        stage={funAsr.status}
        errorCode={funAsr.error_code}
      />,
    );
  }
  const emotion = track.modules["emotion"];
  if (emotion) {
    rows.push(
      <StageRow
        key={`${track.upload_id}-emotion`}
        name="情绪识别"
        stage={emotion.status}
        errorCode={emotion.error_code}
      />,
    );
  }
  return rows;
}

export function ProcessingDetailsPanel({
  details,
}: {
  details: ProcessingDetails | null;
}) {
  if (!details || details.tracks.length === 0) return null;

  const filetransFailed = details.tracks.find(
    (track) => track.filetrans.status === "failed",
  );
  const alignmentStage: ProcessingStage =
    details.aligned_segment_count > 0
      ? "succeeded"
      : filetransFailed
        ? "failed"
        : details.qwen_status === "running"
          ? "running"
          : "queued";
  const qwenStage: ProcessingStage = details.qwen_status;

  return (
    <div className="processing-details" aria-label="会后处理详情">
      <p className="eyebrow">PIPELINE · 会后处理详情</p>
      {details.tracks.flatMap(trackRows)}
      <StageRow
        name="证据对齐"
        stage={alignmentStage}
        errorCode={filetransFailed?.filetrans.error_code}
        detail={
          alignmentStage === "succeeded"
            ? `${details.aligned_segment_count} 个片段`
            : undefined
        }
      />
      <StageRow
        name="Qwen3.7"
        stage={qwenStage}
        errorCode={details.qwen_error_code}
        detail={
          details.submitted_to_qwen
            ? `已接收 ${details.aligned_segment_count} 个片段`
            : undefined
        }
      />
      {filetransFailed && (
        <p className="processing-detail-hint" role="note">
          正式转写模块失败（稳定错误码：{filetransFailed.filetrans.error_code}
          ）。对齐不会伪造片段；检查上游配置或网络后可重试当前步骤。
        </p>
      )}
    </div>
  );
}
