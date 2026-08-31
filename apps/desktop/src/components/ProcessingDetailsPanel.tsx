import type {
  FileTransPhase,
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

const filetransPhaseLabel: Record<FileTransPhase, string> = {
  not_started: "等待录音上传",
  submitting: "正在提交正式转写",
  queued: "已提交，等待百炼处理",
  polling: "正在等待正式转写",
  downloading: "转写完成，正在读取结果",
  normalizing: "正在整理正式转写",
  succeeded: "正式转写完成",
  failed: "正式转写失败",
  timed_out: "等待超时",
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

function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
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

function filetransDetail(track: TrackProcessingDetails): string | undefined {
  const ft = track.filetrans;
  const phase = ft.phase;

  if (phase === "succeeded") {
    return [
      ft.sentence_count != null ? `${ft.sentence_count} 句` : null,
      ft.audio_duration_ms != null ? formatDuration(ft.audio_duration_ms) : null,
      formatSeconds(ft.elapsed_ms) ? `耗时 ${formatSeconds(ft.elapsed_ms)}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
  }

  if (phase === "polling" || phase === "downloading" || phase === "normalizing") {
    const parts: string[] = [];
    if ((ft.poll_attempts ?? 0) > 0) {
      parts.push(`第 ${ft.poll_attempts} 次查询`);
    }
    if (ft.elapsed_ms != null && ft.elapsed_ms > 0) {
      parts.push(`${(ft.elapsed_ms / 1000).toFixed(1)} 秒`);
    }
    return parts.length > 0 ? parts.join(" · ") : filetransPhaseLabel[phase];
  }

  // Terminal phases: error_code is shown by StageRow, so don't duplicate.
  if (phase === "timed_out" || phase === "failed") {
    return undefined;
  }

  // Non-terminal phases (not_started, submitting, queued): show phase label.
  return phase ? filetransPhaseLabel[phase] : undefined;
}

function trackRows(track: TrackProcessingDetails) {
  const ft = track.filetrans;
  const phase = ft.phase;
  const ftStage: ProcessingStage =
    phase === "timed_out" ? "failed" : ft.status;

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
      stage={ftStage}
      errorCode={phase === "timed_out" ? "upstream_timeout" : ft.error_code}
      detail={filetransDetail(track)}
    />,
  ];

  // Show task_reference hint during polling phases.
  if (
    (phase === "polling" || phase === "downloading" || phase === "normalizing") &&
    ft.task_reference
  ) {
    rows.push(
      <div
        key={`${track.upload_id}-ft-ref`}
        className="processing-detail-hint processing-detail-ref"
      >
        任务标识：{ft.task_reference}
      </div>,
    );
  }

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
  showEmpty = false,
  modelName = "文本分析模型",
}: {
  details: ProcessingDetails | null;
  showEmpty?: boolean;
  modelName?: string;
}) {
  if (!details || details.tracks.length === 0) {
    if (!showEmpty) return null;
    return (
      <div className="processing-details" aria-label="会后处理详情">
        <p className="eyebrow">PIPELINE · 会后处理详情</p>
        <StageRow
          name="Gateway 上传"
          stage="queued"
          detail="等待录音上传"
        />
        <StageRow name="OSS 临时媒体" stage="queued" detail="等待 Gateway 上传" />
        <StageRow
          name="FileTrans 正式转写"
          stage="queued"
          detail={filetransPhaseLabel.not_started}
        />
        <StageRow name="证据对齐" stage="queued" detail="等待正式转写" />
        <StageRow name={modelName} stage="queued" detail="等待对齐结果" />
        <p className="processing-detail-hint" role="status">
          结束录音后，这里会显示上传、FileTrans 轮询和正式转写文本。
        </p>
      </div>
    );
  }

  const filetransFailed = details.tracks.find(
    (track) =>
      track.filetrans.phase === "failed" || track.filetrans.phase === "timed_out",
  );
  const filetransSucceeded = details.tracks.find(
    (track) => track.filetrans.phase === "succeeded",
  );

  const alignmentStage: ProcessingStage =
    filetransSucceeded && details.aligned_segment_count > 0
      ? "succeeded"
      : filetransFailed
        ? "failed"
        : details.qwen_status === "running"
          ? "running"
          : "queued";

  const qwenStage: ProcessingStage = details.qwen_status;

  // Determine Qwen wait reason.
  let qwenDetail: string | undefined;
  if (details.submitted_to_qwen) {
    qwenDetail = `已接收 ${details.aligned_segment_count} 个片段`;
  } else if (filetransFailed) {
    qwenDetail = "等待正式转写";
  } else if (!filetransSucceeded) {
    qwenDetail = "等待正式转写";
  }

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
            : filetransFailed
              ? "等待正式转写"
              : undefined
        }
      />
      <StageRow
        name={modelName}
        stage={qwenStage}
        errorCode={details.qwen_error_code}
        detail={qwenDetail}
      />
      {filetransFailed && (
        <p className="processing-detail-hint" role="note">
          {filetransFailed.filetrans.phase === "timed_out"
            ? "FileTrans 等待超时（upstream_timeout）。录音已安全保存，可重试正式转写，不需要重新录音。"
            : `正式转写模块失败（稳定错误码：${filetransFailed.filetrans.error_code}）。对齐不会伪造片段；检查上游配置或网络后可重试当前步骤。`}
        </p>
      )}
    </div>
  );
}
