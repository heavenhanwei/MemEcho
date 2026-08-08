import {
  AudioLines,
  CircleUserRound,
  FileAudio,
  History,
  Mic,
  Network,
  Pause,
  Play,
  Radio,
  RotateCcw,
  Speaker,
  Square,
  Trash2,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { EchoSphere } from "./components/EchoSphere";
import { ReportView } from "./components/ReportView";
import {
  gateway,
  gatewayBaseUrl,
  type GatewayJob,
  type ParticipantCandidate,
} from "./lib/api";
import { startLivePcmCapture, type LivePcmCapture } from "./lib/livePcm";
import {
  bridge,
  isTauriRuntime,
  type AudioDevice,
  type RecoveryMeta,
  type RecoveryStatus,
} from "./lib/tauri";
import { useAppStore, type Page } from "./store";

const nav: Array<{ page: Page; label: string; icon: typeof Radio }> = [
  { page: "now", label: "此刻", icon: Radio },
  { page: "echoes", label: "回声", icon: History },
  { page: "relations", label: "关系", icon: Network },
  { page: "settings", label: "我的", icon: CircleUserRound },
];

function formatTime(seconds: number) {
  return [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), seconds % 60]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

function describeError(cause: unknown, fallback: string) {
  if (cause instanceof Error) return cause.message;
  if (typeof cause === "string" && cause) return cause;
  return fallback;
}

type WorkflowContext = {
  gatewaySessionId: string;
  requestId: string;
  localSessionId: string | null;
  uploaded: boolean;
  source?: { type: "text" | "transcript"; text: string };
  jobId: string | null;
  identityResolved: boolean;
};


type LiveStatus = "connecting" | "connected" | "reconnecting" | "offline";

const liveStatusLabel: Record<LiveStatus, string> = {
  connecting: "\u4e34\u65f6\u5b57\u5e55\u8fde\u63a5\u4e2d",
  connected: "\u4e34\u65f6\u5b57\u5e55\u5df2\u8fde\u63a5",
  reconnecting: "\u4e34\u65f6\u5b57\u5e55\u6b63\u5728\u91cd\u8fde\uff0c\u672c\u5730\u5f55\u97f3\u7ee7\u7eed",
  offline: "\u4e34\u65f6\u5b57\u5e55\u79bb\u7ebf\uff0c\u672c\u5730\u5f55\u97f3\u7ee7\u7eed",
};

function isAbortError(cause: unknown) {
  return cause instanceof DOMException && cause.name === "AbortError";
}

function NowPage() {
  const state = useAppStore();
  const isTauri = isTauriRuntime();
  const timer = useRef<number | undefined>(undefined);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const liveCapture = useRef<LivePcmCapture | null>(null);
  const recordingActive = useRef(false);
  const liveSessionId = useRef<string | null>(null);
  const liveRetryTimer = useRef<number | undefined>(undefined);
  const liveFinishTimer = useRef<number | undefined>(undefined);
  const liveRetryAttempt = useRef(0);
  const backend = useRef<"tauri" | "web">("web");
  const localSessionId = useRef<string | null>(null);
  const workflow = useRef<WorkflowContext | null>(null);
  const progressAbort = useRef<AbortController | null>(null);
  const [source, setSource] = useState<"microphone" | "mixed">("mixed");
  const [error, setError] = useState("");
  const [meterNote, setMeterNote] = useState("");
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [micDeviceId, setMicDeviceId] = useState("");
  const [renderDeviceId, setRenderDeviceId] = useState("");
  const [identityCandidates, setIdentityCandidates] = useState<ParticipantCandidate[]>([]);
  const [participantNames, setParticipantNames] = useState<Record<string, string>>({});
  const [selfParticipantId, setSelfParticipantId] = useState("");
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [canRetry, setCanRetry] = useState(false);
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("offline");
  const [textImportBusy, setTextImportBusy] = useState(false);

  useEffect(() => {
    if (!isTauri) return;
    let cancelled = false;
    bridge
      .listAudioDevices()
      .then((found) => {
        if (!cancelled) setDevices(found);
      })
      .catch((cause) => {
        if (!cancelled) setMeterNote(describeError(cause, "无法枚举音频设备，将使用系统默认设备"));
      });
    return () => {
      cancelled = true;
    };
  }, [isTauri]);

  useEffect(
    () => () => {
      recordingActive.current = false;
      if (liveRetryTimer.current) window.clearTimeout(liveRetryTimer.current);
      if (liveFinishTimer.current) window.clearTimeout(liveFinishTimer.current);
      if (timer.current) window.clearInterval(timer.current);
      void stopLiveAudio();
      socket.current?.close();
      progressAbort.current?.abort();
    },
    [],
  );

  function startMeter(media: MediaStream) {
    const context = new AudioContext();
    audioContext.current = context;
    const analyser = context.createAnalyser();
    const sourceNode = context.createMediaStreamSource(media);
    sourceNode.connect(analyser);
    analyser.fftSize = 256;
    const data = new Uint8Array(analyser.frequencyBinCount);
    const measure = () => {
      if (!stream.current) return;
      analyser.getByteFrequencyData(data);
      const average = data.reduce((sum, value) => sum + value, 0) / data.length / 255;
      useAppStore.getState().patch({ volume: average });
      requestAnimationFrame(measure);
    };
    measure();
  }

  function stopMeter() {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    if (audioContext.current) {
      void audioContext.current.close();
      audioContext.current = null;
    }
  }


  function scheduleLiveReconnect() {
    if (
      !recordingActive.current ||
      !liveCapture.current ||
      !liveSessionId.current ||
      liveRetryTimer.current
    ) {
      return;
    }
    setLiveStatus("reconnecting");
    const delay = Math.min(1000 * 2 ** Math.min(liveRetryAttempt.current, 4), 15_000);
    liveRetryTimer.current = window.setTimeout(() => {
      liveRetryTimer.current = undefined;
      liveRetryAttempt.current += 1;
      if (liveSessionId.current) connectLive(liveSessionId.current);
    }, delay);
  }

  function connectLive(sessionId: string) {
    if (!recordingActive.current || !liveCapture.current) return;
    setLiveStatus(liveRetryAttempt.current > 0 ? "reconnecting" : "connecting");
    let allowReconnect = true;
    let ws: WebSocket;
    try {
      ws = new WebSocket(gateway.liveUrl(sessionId));
    } catch {
      setLiveStatus("offline");
      scheduleLiveReconnect();
      return;
    }
    socket.current = ws;

    ws.onopen = () => {
      if (socket.current !== ws) return;
      liveRetryAttempt.current = 0;
      setLiveStatus("connected");
    };
    ws.onmessage = (message) => {
      if (socket.current !== ws) return;
      let event: {
        type?: string;
        state?: string;
        text?: string;
        retryable?: boolean;
      };
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      if (
        (event.type === "transcript.partial" || event.type === "transcript.final") &&
        event.text
      ) {
        useAppStore.getState().patch({ caption: event.text });
      } else if (event.type === "connection.state") {
        if (event.state === "connected") setLiveStatus("connected");
        if (event.state === "reconnecting") setLiveStatus("reconnecting");
        if (event.state === "offline") {
          setLiveStatus("offline");
          if (recordingActive.current) scheduleLiveReconnect();
          ws.close();
        }
      } else if (event.type === "error") {
        allowReconnect = event.retryable !== false;
        setLiveStatus(allowReconnect ? "reconnecting" : "offline");
        if (allowReconnect) scheduleLiveReconnect();
        ws.close();
      }
    };
    ws.onerror = () => {
      if (socket.current !== ws) return;
      setLiveStatus("reconnecting");
      ws.close();
    };
    ws.onclose = () => {
      if (socket.current !== ws) return;
      socket.current = null;
      if (recordingActive.current && allowReconnect) scheduleLiveReconnect();
      else setLiveStatus("offline");
    };
  }

  function startLiveAudio(media: MediaStream) {
    liveCapture.current = startLivePcmCapture(media, {
      getSocket: () => socket.current,
      onLevel: (volume) => useAppStore.getState().patch({ volume }),
      onSendError: () => {
        setLiveStatus("reconnecting");
        socket.current?.close();
      },
    });
  }

  async function stopLiveAudio(flush = false) {
    const capture = liveCapture.current;
    liveCapture.current = null;
    try {
      await capture?.stop(flush);
    } catch {
      setLiveStatus("offline");
    } finally {
      stream.current?.getTracks().forEach((track) => track.stop());
      stream.current = null;
      useAppStore.getState().patch({ volume: 0 });
    }
  }

  function finishLiveSocket() {
    if (liveRetryTimer.current) window.clearTimeout(liveRetryTimer.current);
    liveRetryTimer.current = undefined;
    const ws = socket.current;
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send("end");
      } catch {
        socket.current = null;
        setLiveStatus("offline");
        ws.close();
        return;
      }
      liveFinishTimer.current = window.setTimeout(() => {
        if (socket.current === ws) socket.current = null;
        ws.close();
      }, 2000);
    } else {
      socket.current = null;
      ws.close();
    }
  }

  async function start() {
    if (state.soulState !== "idle") return;
    setError("");
    setMeterNote("");
    localSessionId.current = null;
    workflow.current = null;
    setIdentityCandidates([]);
    setCanRetry(false);
    try {
      const session = await gateway.createSession("新的回声", source);
      liveSessionId.current = session.id;
      liveRetryAttempt.current = 0;
      setLiveStatus("connecting");

      if (isTauri) {
        backend.current = "tauri";
        const capture = await bridge.startCapture(
          micDeviceId || null,
          renderDeviceId || null,
        );
        localSessionId.current = capture.session_id;
        try {
          const media = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true },
          });
          stream.current = media;
          startLiveAudio(media);
        } catch {
          setMeterNote("音量计不可用：未授予窗口麦克风访问权限（不影响本地录音）");
        }
      } else {
        backend.current = "web";
        const media = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
        });
        stream.current = media;
        recorder.current = new MediaRecorder(media);
        recorder.current.start(1000);
        startLiveAudio(media);
      }
      recordingActive.current = true;
      if (liveCapture.current) {
        connectLive(session.id);
      } else {
        setLiveStatus("offline");
      }

      state.patch({
        sessionId: session.id,
        requestId: session.request_id,
        soulState: "recording",
        elapsed: 0,
        caption: "临时字幕将在这里出现",
      });
      timer.current = window.setInterval(
        () => useAppStore.setState((current) => ({ elapsed: current.elapsed + 1 })),
        1000,
      );
    } catch (cause) {
      recordingActive.current = false;
      if (liveRetryTimer.current) window.clearTimeout(liveRetryTimer.current);
      liveRetryTimer.current = undefined;
      socket.current?.close();
      socket.current = null;
      await stopLiveAudio();
      if (timer.current) window.clearInterval(timer.current);
      timer.current = undefined;
      setError(describeError(cause, "无法开始录音"));
      state.patch({ soulState: "idle" });
    }
  }

  async function togglePause() {
    const paused = state.soulState === "paused";
    try {
      if (backend.current === "tauri") {
        if (paused) await bridge.resumeCapture();
        else await bridge.pauseCapture();
      } else if (paused) {
        recorder.current?.resume();
      } else {
        recorder.current?.pause();
      }
      state.patch({ soulState: paused ? "recording" : "paused" });
      liveCapture.current?.setPaused(!paused);
    } catch (cause) {
      setError(describeError(cause, "无法切换暂停状态"));
    }
  }

  function applyJobProgress(job: GatewayJob) {
    state.patch({
      jobId: job.id,
      jobStatus: job.status,
      progress: job.progress,
      stageLabel: job.stage_label,
    });
  }

  async function monitorJob(jobId: string, stopOnAwaitingIdentity: boolean) {
    let latest: GatewayJob | null = null;
    let stoppedForIdentity = false;
    const controller = new AbortController();
    progressAbort.current?.abort();
    progressAbort.current = controller;

    try {
      await gateway.jobEvents(
        jobId,
        (event) => {
          latest = event;
          applyJobProgress(event);
          if (stopOnAwaitingIdentity && event.status === "awaiting_identity") {
            stoppedForIdentity = true;
            controller.abort();
          }
        },
        controller.signal,
      );
    } catch (cause) {
      if (!stoppedForIdentity) {
        if (isAbortError(cause)) throw cause;
        state.patch({ stageLabel: "实时进度连接中断，已切换到状态轮询" });
      }
    } finally {
      if (progressAbort.current === controller) progressAbort.current = null;
    }

    const streamed = latest as GatewayJob | null;
    if (
      streamed &&
      (streamed.status === "complete" ||
        streamed.status === "failed" ||
        (stopOnAwaitingIdentity && streamed.status === "awaiting_identity"))
    ) {
      return streamed;
    }

    while (true) {
      const current = await gateway.job(jobId);
      applyJobProgress(current);
      if (
        current.status === "complete" ||
        current.status === "failed" ||
        (stopOnAwaitingIdentity && current.status === "awaiting_identity")
      ) {
        return current;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
  }

  async function finishWorkflow(context: WorkflowContext) {
    state.patch({ stageLabel: "正在读取分析结果与报告文件", progress: 96 });
    const result = await gateway.result(context.gatewaySessionId);
    const artifacts = await gateway.artifacts(context.gatewaySessionId);

    if (context.localSessionId) {
      state.patch({ stageLabel: "正在安全保存本地报告", progress: 98 });
      await bridge.saveReportFiles(
        context.localSessionId,
        artifacts.contents.json || JSON.stringify(result),
        artifacts.contents.markdown,
        artifacts.contents.html,
      );
    }

    setIdentityCandidates([]);
    setCanRetry(false);
    state.patch({
      result,
      soulState: "responding",
      page: "report",
      progress: 100,
      stageLabel: context.localSessionId
        ? "分析完成，报告已保存到本地"
        : "网页演示分析完成（未写入桌面本地文件）",
    });
  }

  function prepareIdentity(candidates: ParticipantCandidate[]) {
    if (candidates.length === 0) {
      throw new Error("分析需要确认参与者身份，但没有可用的说话人候选");
    }
    setIdentityCandidates(candidates);
    setParticipantNames(
      Object.fromEntries(
        candidates.map((candidate) => [candidate.participant_id, candidate.display_name]),
      ),
    );
    setSelfParticipantId(candidates.length === 1 ? candidates[0].participant_id : "");
    state.patch({ stageLabel: "请确认参与者，并指定哪一位是我", progress: 55 });
  }

  async function runWorkflow(context: WorkflowContext) {
    setWorkflowBusy(true);
    setError("");
    setCanRetry(false);
    state.patch({ soulState: "processing" });

    try {
      if (context.localSessionId && !context.uploaded) {
        state.patch({ jobStatus: "uploading", progress: 8, stageLabel: "正在上传本地双轨录音" });
        await bridge.uploadSessionTracks(
          context.localSessionId,
          context.gatewaySessionId,
          gatewayBaseUrl,
        );
        context.uploaded = true;
      } else if (!context.localSessionId && !context.uploaded) {
        state.patch({
          progress: 8,
          stageLabel: "网页演示模式：跳过桌面音频上传与本地持久化",
        });
        context.uploaded = true;
      }

      if (!context.jobId) {
        state.patch({ jobStatus: "queued", progress: 12, stageLabel: "正在提交分析请求" });
        const job = await gateway.analyze(context.gatewaySessionId, context.requestId, context.source);
        context.jobId = job.id;
        applyJobProgress(job);
      }

      const current = await monitorJob(context.jobId, !context.identityResolved);
      if (current.status === "awaiting_identity" && !context.identityResolved) {
        prepareIdentity(
          (await gateway.participantCandidates(context.gatewaySessionId)).candidates,
        );
        return;
      }
      if (current.status === "failed") {
        if (current.retryable) {
          context.jobId = null;
          context.requestId = `${context.requestId}-retry-${Date.now()}`;
        }
        throw new Error(current.error_code ?? "分析失败");
      }
      await finishWorkflow(context);
    } catch (cause) {
      if (!isAbortError(cause)) {
        setError(describeError(cause, "分析失败"));
        setCanRetry(true);
        state.patch({ stageLabel: "当前步骤未完成，可以安全重试" });
      }
    } finally {
      setWorkflowBusy(false);
    }
  }

  async function importTextFile(file: File) {
    if (state.soulState !== "idle" || textImportBusy) return;
    setError("");
    setTextImportBusy(true);
    try {
      const text = await file.text();
      if (!text.trim()) throw new Error("文本内容不能为空");
      const title = file.name.replace(/\.[^.]+$/, "").trim() || "文本回声";
      const imported = isTauri
        ? await bridge.importTextContent(text, title, file.name)
        : null;
      const session = await gateway.createSession(title, "import");
      const context: WorkflowContext = {
        gatewaySessionId: session.id,
        requestId: session.request_id,
        localSessionId: imported?.session.id ?? null,
        uploaded: true,
        source: { type: "text", text },
        jobId: null,
        identityResolved: false,
      };
      workflow.current = context;
      localSessionId.current = context.localSessionId;
      state.patch({
        sessionId: session.id,
        requestId: session.request_id,
        soulState: "processing",
        progress: 4,
        stageLabel: "正在导入文本，准备分析",
        caption: text.slice(0, 120),
      });
      await runWorkflow(context);
    } catch (cause) {
      setError(describeError(cause, "文本导入失败"));
    } finally {
      setTextImportBusy(false);
    }
  }

  async function confirmIdentity() {
    let identityResolved = false;
    const context = workflow.current;
    if (!context?.jobId || !selfParticipantId || workflowBusy) return;
    setWorkflowBusy(true);
    setError("");
    try {
      await gateway.resolveParticipants(context.gatewaySessionId, {
        participants: identityCandidates.map((candidate) => ({
          id: candidate.participant_id,
          name: participantNames[candidate.participant_id]?.trim() || candidate.display_name,
          is_self: candidate.participant_id === selfParticipantId,
        })),
        self_participant_id: selfParticipantId,
        identity_basis: "user_confirmed",
      });
      context.identityResolved = true;
      identityResolved = true;
      setIdentityCandidates([]);
      state.patch({ stageLabel: "身份已确认，正在继续同一分析任务" });
      const current = await monitorJob(context.jobId, false);
      if (current.status === "failed") {
        if (current.retryable) {
          context.jobId = null;
          context.requestId = `${context.requestId}-retry-${Date.now()}`;
        }
        throw new Error(current.error_code ?? "分析失败");
      }
      await finishWorkflow(context);
    } catch (cause) {
      if (!isAbortError(cause)) {
        setError(describeError(cause, "身份确认或继续分析失败"));
        setCanRetry(identityResolved);
        state.patch({ stageLabel: "当前步骤未完成，可以安全重试" });
      }
    } finally {
      setWorkflowBusy(false);
    }
  }

  async function retryWorkflow() {
    if (!workflow.current || workflowBusy) return;
    await runWorkflow(workflow.current);
  }

  async function stop() {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = undefined;
    setError("");
    recordingActive.current = false;

    let stopFailure: unknown = null;
    try {
      if (backend.current === "tauri") {
        const stopped = await bridge.stopCapture();
        localSessionId.current = stopped.session_id;
      } else {
        recorder.current?.stop();
        recorder.current = null;
      }
    } catch (cause) {
      stopFailure = cause;
    }

    await stopLiveAudio(true);
    finishLiveSocket();

    if (stopFailure) {
      setError(describeError(stopFailure, "停止录音时出现问题"));
      state.patch({ soulState: "idle" });
      return;
    }

    const { sessionId, requestId } = useAppStore.getState();
    if (!sessionId || !requestId) {
      setError("会话信息缺失，无法开始分析");
      state.patch({ soulState: "idle" });
      return;
    }

    const context: WorkflowContext = {
      gatewaySessionId: sessionId,
      requestId,
      localSessionId: backend.current === "tauri" ? localSessionId.current : null,
      uploaded: false,
      jobId: null,
      identityResolved: false,
    };
    workflow.current = context;
    state.patch({
      soulState: "processing",
      jobStatus: "queued",
      progress: 0,
      stageLabel: "录音已结束，准备上传和分析",
    });
    await runWorkflow(context);
  }

  if (state.soulState === "processing") {
    return (
      <section className="processing">
        <EchoSphere state="processing" energy={0.3} />
        <p className="eyebrow">ECHO IS FORMING</p>
        <h1>回声正在成形</h1>
        <p>{state.stageLabel}</p>
        {identityCandidates.length > 0 ? (
          <div className="identity-panel" aria-label="参与者身份确认">
            <h2>哪一位是“我”？</h2>
            <p>请确认说话人名称，并选择你的视角。memEcho 不会使用声纹推断身份。</p>
            {identityCandidates.map((candidate) => (
              <label key={candidate.participant_id}>
                <input
                  type="radio"
                  name="self-participant"
                  checked={selfParticipantId === candidate.participant_id}
                  onChange={() => setSelfParticipantId(candidate.participant_id)}
                />
                <input
                  aria-label={`${candidate.display_name} 的名称`}
                  value={participantNames[candidate.participant_id] ?? ""}
                  maxLength={80}
                  onChange={(event) =>
                    setParticipantNames((current) => ({
                      ...current,
                      [candidate.participant_id]: event.target.value,
                    }))
                  }
                />
                <small>
                  {Math.round(candidate.speaking_time_ms / 100) / 10} 秒 · {candidate.segment_count} 段
                </small>
              </label>
            ))}
            <button
              className="primary-action"
              onClick={confirmIdentity}
              disabled={!selfParticipantId || workflowBusy}
            >
              {workflowBusy ? "正在继续分析…" : "确认身份并继续"}
            </button>
          </div>
        ) : (
          <>
            <div className="progress-track">
              <i style={{ width: `${state.progress}%` }} />
            </div>
            <strong>{state.progress}%</strong>
          </>
        )}
        {error && <p className="workflow-error">{error}</p>}
        {canRetry && (
          <button className="retry-action" onClick={retryWorkflow} disabled={workflowBusy}>
            <RotateCcw size={15} /> {workflowBusy ? "正在重试…" : "重试当前步骤"}
          </button>
        )}
      </section>
    );
  }

  const inputDevices = devices.filter((device) => device.is_input);
  const outputDevices = devices.filter((device) => !device.is_input);

  return (
    <section className="now-page">
      <div className="hero-copy">
        <p className="eyebrow">MEMORY · EMOTION · RELATIONSHIP</p>
        <h1>
          听见声音，
          <br />
          也听见自己。
        </h1>
        <p>
          语言与声学证据共同形成一段可回看的回声。
          <br />
          结论始终可以回到来源，也始终允许被修正。
        </p>
        <div className="source-switch">
          <button
            className={source === "mixed" ? "active" : ""}
            onClick={() => setSource("mixed")}
          >
            <AudioLines size={16} /> 麦克风＋系统声音
          </button>
          <button
            className={source === "microphone" ? "active" : ""}
            onClick={() => setSource("microphone")}
          >
            <Radio size={16} /> 仅麦克风
          </button>
        </div>
        {isTauri && (
          <div className="device-selects">
            <label>
              <span>
                <Mic size={14} /> 麦克风
              </span>
              <select
                value={micDeviceId}
                onChange={(event) => setMicDeviceId(event.target.value)}
                disabled={state.soulState !== "idle"}
              >
                <option value="">系统默认麦克风</option>
                {inputDevices.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name}
                    {device.is_default ? "（默认）" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>
                <Speaker size={14} /> 系统输出
              </span>
              <select
                value={renderDeviceId}
                onChange={(event) => setRenderDeviceId(event.target.value)}
                disabled={state.soulState !== "idle"}
              >
                <option value="">系统默认输出</option>
                {outputDevices.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name}
                    {device.is_default ? "（默认）" : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {isTauri && source === "mixed" && (
          <p className="live-scope-note">
            {"\u5b9e\u65f6\u5b57\u5e55\u4ec5\u4f7f\u7528\u9ea6\u514b\u98ce\uff1b\u6b63\u5f0f\u62a5\u544a\u4ecd\u4f7f\u7528\u672c\u5730\u9ea6\u514b\u98ce\u4e0e\u7cfb\u7edf\u58f0\u97f3\u53cc\u8f68\u3002"}
          </p>
        )}
        <p className="backend-note">
          {isTauri
            ? "桌面原生录音 · 麦克风＋系统输出双轨（WASAPI）"
            : "网页演示模式 · 使用 MediaRecorder 仅录制麦克风"}
        </p>
      </div>
      <div className="soul-column">
        <EchoSphere state={state.soulState} energy={state.volume} onActivate={start} />
        {state.soulState === "idle" ? (
          <button className="record-cta" onClick={start}>
            点击球体，开始录音
          </button>
        ) : (
          <div className="recording-panel">
            <p className="live-pill">
              <i /> {state.soulState === "paused" ? "录音已暂停" : "正在录音"}
            </p>
            <strong>{formatTime(state.elapsed)}</strong>
            <p className="live-caption">{state.caption}</p>
            <p className={`live-connection ${liveStatus}`}>{liveStatusLabel[liveStatus]}</p>
            {meterNote && <p className="meter-note">{meterNote}</p>}
            <div>
              <button onClick={togglePause}>
                {state.soulState === "paused" ? <Play size={17} /> : <Pause size={17} />}
                {state.soulState === "paused" ? "继续" : "暂停"}
              </button>
              <button className="stop" onClick={stop}>
                <Square size={15} /> 结束并分析
              </button>
            </div>
          </div>
        )}
      </div>
      <div className="quick-row">
        <label className="quick-card">
          <Upload size={21} />
          <span>
            <b>导入音频</b>
            <small>MP3 · WAV · M4A · MP4</small>
          </span>
          <input type="file" accept="audio/*,video/*" hidden />
        </label>
        <label className={`quick-card ${textImportBusy ? "disabled" : ""}`}>
          <FileAudio size={21} />
          <span>
            <b>分析文本</b>
            <small>会议转写或个人独白</small>
          </span>
          <input
            type="file"
            accept=".txt,.md,text/plain,text/markdown"
            hidden
            disabled={textImportBusy}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.currentTarget.value = "";
              if (file) void importTextFile(file);
            }}
          />
        </label>
      </div>
      {error && <p className="error-banner">{error}</p>}
    </section>
  );
}

const recoveryStatusLabel: Record<RecoveryStatus, string> = {
  recording: "录音中（被中断）",
  paused: "已暂停（被中断）",
  finalized: "已完成",
  failed: "失败",
};

function formatStarted(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString("zh-CN", { hour12: false });
}

function EchoesPage() {
  const { result, setPage } = useAppStore();
  const isTauri = isTauriRuntime();
  const [recoverable, setRecoverable] = useState<RecoveryMeta[]>([]);
  const [recoveryError, setRecoveryError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!isTauri) return;
    try {
      setRecoverable(await bridge.listRecoverableSessions());
      setRecoveryError("");
    } catch (cause) {
      setRecoveryError(describeError(cause, "无法读取可恢复的会话"));
    }
  }, [isTauri]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function recover(sessionId: string) {
    setBusyId(sessionId);
    setRecoveryError("");
    try {
      await bridge.recoverSession(sessionId);
      await refresh();
    } catch (cause) {
      setRecoveryError(describeError(cause, "恢复失败"));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(sessionId: string) {
    setBusyId(sessionId);
    setRecoveryError("");
    try {
      await bridge.deleteLocalSession(sessionId);
      await refresh();
    } catch (cause) {
      setRecoveryError(describeError(cause, "删除失败"));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="list-page">
      <p className="eyebrow">YOUR ECHOES</p>
      <h1>每一次重要的对话，都会留下可以回看的形状。</h1>

      {isTauri && (
        <div className="recovery-section">
          <p className="eyebrow">INTERRUPTED · 被中断的录音</p>
          {recoverable.length === 0 ? (
            <p className="recovery-empty">没有被中断、需要恢复的本地录音。</p>
          ) : (
            <div className="recovery-list">
              {recoverable.map((meta) => (
                <article key={meta.session_id} className="recovery-card">
                  <div>
                    <b>{meta.session_id.slice(0, 8)}…</b>
                    <p>
                      {recoveryStatusLabel[meta.status]} · 开始于 {formatStarted(meta.started_at)}
                    </p>
                    {meta.error_code ? (
                      <p className="recovery-error">错误：{meta.error_code}</p>
                    ) : null}
                  </div>
                  <div className="recovery-actions">
                    <button
                      onClick={() => recover(meta.session_id)}
                      disabled={busyId === meta.session_id}
                    >
                      <RotateCcw size={14} /> 恢复
                    </button>
                    <button
                      className="danger"
                      onClick={() => remove(meta.session_id)}
                      disabled={busyId === meta.session_id}
                    >
                      <Trash2 size={14} /> 删除
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          {recoveryError && <p className="error-banner">{recoveryError}</p>}
        </div>
      )}

      <div className="session-list">
        <article>
          <time>今天</time>
          <div>
            <b>{result ? "新的回声" : "等待第一段真实会谈"}</b>
            <p>{result?.minutes.summary ?? "开始录音或导入音频，形成你的第一份分析报告。"}</p>
          </div>
          <button disabled={!result} onClick={() => setPage("report")}>
            {result ? "打开报告" : "尚无报告"}
          </button>
        </article>
      </div>
    </section>
  );
}

function RelationsPage() {
  return (
    <section className="relations-page">
      <div className="relation-copy">
        <p className="eyebrow">MEMORY ECHO</p>
        <h1>记忆不是标签，而是已经发生、仍在流动的回应。</h1>
        <p>只显示你明确保存的内容。点击每个记忆点，都可以回到它来自的对话。</p>
      </div>
      <EchoSphere state="memory" energy={0.08} />
      <div className="memory-legend">
        <span>
          <i className="violet" /> 已确认的事件
        </span>
        <span>
          <i className="peach" /> 值得继续观察
        </span>
        <span>0 条长期记忆 · 默认关闭</span>
      </div>
    </section>
  );
}

function SettingsPage() {
  return (
    <section className="settings-page">
      <p className="eyebrow">YOUR SPACE</p>
      <h1>你的声音，首先属于你。</h1>
      <div className="settings-grid">
        <article>
          <h2>数据位置</h2>
          <p>原始录音、报告和记忆保存在本机。云端临时副本分析后删除。</p>
          <button>打开本地数据目录</button>
        </article>
        <article>
          <h2>长期记忆</h2>
          <p>默认关闭。只有经你确认的观察才会进入关系视图。</p>
          <label>
            <input type="checkbox" /> 允许保存确认后的记忆
          </label>
        </article>
        <article>
          <h2>分析边界</h2>
          <p>memEcho 不进行心理诊断，不把表达状态解释为真实内心。</p>
          <button>查看分析原则</button>
        </article>
      </div>
    </section>
  );
}

export function App() {
  const { page, result, setPage } = useAppStore();
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => setPage("now")}>
          <span className="brand-symbol">◌</span>
          mem<span>Echo</span>
        </button>
        <p>让每一次回应，都成为理解自己的入口</p>
        <button className="avatar">M</button>
      </header>
      <aside className="side-nav">
        {nav.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.page}
              className={page === item.page ? "active" : ""}
              onClick={() => setPage(item.page)}
            >
              <Icon size={20} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </aside>
      <main>
        {page === "now" && <NowPage />}
        {page === "echoes" && <EchoesPage />}
        {page === "relations" && <RelationsPage />}
        {page === "settings" && <SettingsPage />}
        {page === "report" &&
          (result ? (
            <ReportView
              result={result}
              localSessionId={localSessionId.current}
              onBack={() => setPage("echoes")}
            />
          ) : (
            <EchoesPage />
          ))}
      </main>
    </div>
  );
}
