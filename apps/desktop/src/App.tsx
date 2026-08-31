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
import { ProcessingDetailsPanel } from "./components/ProcessingDetailsPanel";
import { ReportView } from "./components/ReportView";
import { RelationsView } from "./components/RelationsView";
import {
  clearLlmConfig,
  gateway,
  GatewayApiError,
  getActiveProviderProfileId,
  getGatewayUrl,
  getLlmConfigState,
  hasGatewayToken,
  hasLlmConfig,
  initGatewayConfig,
  initLlmConfig,
  saveProfileApiKey,
  deleteProfileApiKey,
  setActiveProviderProfileId,
  setGatewayUrl,
  setGatewayToken,
  setLlmConfig,
  type GatewayJob,
  type ParticipantCandidate,
  type ProcessingDetails,
  type ProfileVerification,
  type ProviderProfile,
} from "./lib/api";
import { startLivePcmCapture, type LivePcmCapture } from "./lib/livePcm";
import {
  clearRecordingChunks,
  listRecoverableRecordings,
  loadRecordingChunks,
  persistRecordingChunk,
  type RecoverableRecording,
} from "./lib/chunkStore";
import {
  bridge,
  isTauriRuntime,
  type AudioDevice,
  type RecoveryMeta,
  type RecoveryStatus,
} from "./lib/tauri";
import { useAppStore, type Page, type RelationsData } from "./store";

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

/** Create a gateway session bound to the active provider profile, if any. */
async function createGatewaySession(title: string, sourceMode: string) {
  let profileId = getActiveProviderProfileId();
  const profiles = await gateway.listProfiles().catch(() => []);
  if (profileId && !profiles.some((profile) => profile.id === profileId)) {
    profileId = "";
    setActiveProviderProfileId("");
  }
  if (!profileId) {
    const realProfiles = profiles.filter((profile) => profile.provider !== "mock");
    if (realProfiles.length === 1) {
      profileId = realProfiles[0].id;
      setActiveProviderProfileId(profileId);
    }
  }
  if (!profileId) return gateway.createSession(title, sourceMode);
  try {
    return await gateway.createSession(title, sourceMode, profileId);
  } catch (cause) {
    // A deleted active profile must not block recording; drop it and retry unbound.
    if (cause instanceof GatewayApiError && cause.status === 404) {
      setActiveProviderProfileId("");
      return gateway.createSession(title, sourceMode);
    }
    throw cause;
  }
}

type WorkflowContext = {
  gatewaySessionId: string;
  requestId: string;
  localSessionId: string | null;
  uploaded: boolean;
  webRecording?: Blob;
  source?: { type: "text" | "transcript"; text: string };
  jobId: string | null;
  identityResolved: boolean;
};


type LiveStatus = "connecting" | "connected" | "reconnecting" | "offline";

type BrowserAudioCapture = {
  media: MediaStream;
  stop: () => Promise<void>;
};

async function openBrowserAudioCapture(
  source: "microphone" | "mixed",
): Promise<BrowserAudioCapture> {
  const microphone = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  if (source === "microphone") {
    return {
      media: microphone,
      stop: async () => microphone.getTracks().forEach((track) => track.stop()),
    };
  }

  let shared: MediaStream | null = null;
  let context: AudioContext | null = null;
  try {
    shared = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    if (shared.getAudioTracks().length === 0) {
      throw new Error("共享内容没有音频，请选择 Chrome 标签页并勾选“分享标签页音频”");
    }

    context = new AudioContext();
    const destination = context.createMediaStreamDestination();
    context.createMediaStreamSource(microphone).connect(destination);
    context.createMediaStreamSource(shared).connect(destination);
    await context.resume();
    const mixed = destination.stream;
    let stopped = false;
    return {
      media: mixed,
      stop: async () => {
        if (stopped) return;
        stopped = true;
        mixed.getTracks().forEach((track) => track.stop());
        microphone.getTracks().forEach((track) => track.stop());
        shared?.getTracks().forEach((track) => track.stop());
        await context?.close();
      },
    };
  } catch (cause) {
    microphone.getTracks().forEach((track) => track.stop());
    shared?.getTracks().forEach((track) => track.stop());
    await context?.close();
    throw cause;
  }
}

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
  const browserRecordingChunks = useRef<Blob[]>([]);
  const stream = useRef<MediaStream | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const liveCapture = useRef<LivePcmCapture | null>(null);
  const browserCaptureStop = useRef<(() => Promise<void>) | null>(null);
  const nativeLiveTimer = useRef<number | undefined>(undefined);
  const nativeLiveActive = useRef(false);
  const webChunkIndex = useRef(0);
  const webRecordingId = useRef<string | null>(null);
  const recordingActive = useRef(false);
  const liveSessionId = useRef<string | null>(null);
  const liveRetryTimer = useRef<number | undefined>(undefined);
  const liveFinishTimer = useRef<number | undefined>(undefined);
  const liveRetryAttempt = useRef(0);
  const backend = useRef<"tauri" | "web">("web");
  const localSessionId = useRef<string | null>(null);
  const workflow = useRef<WorkflowContext | null>(null);
  const progressAbort = useRef<AbortController | null>(null);
  const [source, setSource] = useState<"microphone" | "mixed">("microphone");
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
  const [gatewayOk, setGatewayOk] = useState<boolean | null>(null);
  const [gatewayError, setGatewayError] = useState("");
  const [gatewayProvider, setGatewayProvider] = useState("");
  const [processingDetails, setProcessingDetails] =
    useState<ProcessingDetails | null>(null);
  const [recoveredRecordings, setRecoveredRecordings] = useState<
    RecoverableRecording[]
  >([]);
  const canCaptureBrowserAudio =
    !isTauri && typeof navigator.mediaDevices?.getDisplayMedia === "function";

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

  // Initialize gateway config from Tauri bridge, then check health
  useEffect(() => {
    let cancelled = false;
    initGatewayConfig()
      .then(() => {
        if (cancelled) return;
        if (isTauri) {
          bridge
            .checkGateway(getGatewayUrl())
            .then((status) => {
              if (cancelled) return;
              setGatewayOk(status.ok);
              setGatewayProvider(status.provider ?? "");
              if (!status.ok) {
                setGatewayError(
                  status.error ?? `Cannot reach analysis gateway at ${status.url}`,
                );
              }
            })
            .catch(() => {
              // bridge error — skip
            });
        } else {
          if (typeof gateway.health === "function") {
            gateway
              .health()
              .then((health) => {
                if (!cancelled) {
                  setGatewayOk(true);
                  setGatewayProvider(health.provider ?? "");
                }
              })
              .catch(() => {
                if (cancelled) return;
                setGatewayOk(false);
                setGatewayError(
                  `Cannot reach analysis gateway at ${getGatewayUrl()} — ensure it is running`,
                );
              });
          }
        }
      })
      .catch((cause) => {
        if (cancelled) return;
        setGatewayOk(false);
        setGatewayError(describeError(cause, "Gateway 初始化失败"));
      });
    return () => {
      cancelled = true;
    };
  }, [isTauri]);

  useEffect(() => {
    if (state.soulState !== "processing") return;
    const sessionId = useAppStore.getState().sessionId;
    if (!sessionId) return;
    let cancelled = false;
    let stopped = false;
    const refresh = () => {
      if (stopped) return;
      gateway
        .processingDetails(sessionId)
        .then((details) => {
          if (!cancelled && !stopped) setProcessingDetails(details);
        })
        .catch((cause) => {
          if (cancelled || stopped) return;
          if (cause instanceof GatewayApiError && cause.status === 404) {
            stopped = true;
            setProcessingDetails(null);
            setCanRetry(false);
            setError(
              "Gateway 已找不到当前会话，可能服务刚刚重启；本次处理详情已停止刷新，请重新录音或重新导入。",
            );
            state.patch({ stageLabel: "当前会话已失效，请重新开始" });
            return;
          }
          // Details are supplementary; transient errors keep the main job progress authoritative.
        });
    };
    refresh();
    const interval = window.setInterval(refresh, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [state.soulState]);

  useEffect(() => {
    if (isTauri) return;
    let cancelled = false;
    listRecoverableRecordings()
      .then((recordings) => {
        if (!cancelled) setRecoveredRecordings(recordings);
      })
      .catch(() => {
        // Persistence is best-effort; never block the page on it.
      });
    return () => {
      cancelled = true;
    };
  }, [isTauri]);

  async function downloadRecoveredRecording(recording: RecoverableRecording) {
    try {
      const chunks = await loadRecordingChunks(recording.recordingId);
      if (chunks.length === 0) return;
      const blob = new Blob(chunks, { type: "audio/webm" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `memecho-recovered-${recording.recordingId}.webm`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      // Best-effort download; the recording stays listed for another attempt.
    }
  }

  async function discardRecoveredRecording(recording: RecoverableRecording) {
    await clearRecordingChunks(recording.recordingId).catch(() => undefined);
    setRecoveredRecordings((current) =>
      current.filter((item) => item.recordingId !== recording.recordingId),
    );
  }

  const retryGatewayCheck = useCallback(async () => {
    setGatewayOk(null);
    setGatewayError("");
    try {
      await initGatewayConfig(true);
      if (isTauri) {
        const status = await bridge.checkGateway(getGatewayUrl());
        setGatewayOk(status.ok);
        setGatewayProvider(status.provider ?? "");
        if (!status.ok) {
          setGatewayError(
            status.error ?? `Cannot reach analysis gateway at ${status.url}`,
          );
        }
      } else {
        if (typeof gateway.health === "function") {
          const health = await gateway.health();
          setGatewayProvider(health.provider ?? "");
        }
        setGatewayOk(true);
      }
    } catch (cause) {
      setGatewayOk(false);
      setGatewayError(describeError(cause, "Gateway check failed"));
    }
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
      (!liveCapture.current && !nativeLiveActive.current) ||
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
    if (!recordingActive.current || (!liveCapture.current && !nativeLiveActive.current)) return;
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
        message?: string;
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
        setMeterNote("");
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
        setMeterNote(event.message || "实时字幕服务返回错误");
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

  function startNativeLiveAudio() {
    nativeLiveActive.current = true;
    // Poll native WASAPI PCM from the Tauri bridge and feed to the live WebSocket.
    const poll = () => {
      if (!recordingActive.current) return;
      bridge
        .pollLivePcm()
        .then((base64) => {
          if (!base64 || !recordingActive.current) return;
          const ws = socket.current;
          if (ws && ws.readyState === WebSocket.OPEN) {
            try {
              const binary = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
              if (binary.byteLength > 0) {
                ws.send(binary.buffer);
                // Simple RMS level estimate from PCM16 bytes
                const view = new DataView(binary.buffer);
                let sum = 0;
                const count = binary.byteLength / 2;
                for (let i = 0; i < count; i++) {
                  const sample = view.getInt16(i * 2, true) / 32768;
                  sum += sample * sample;
                }
                const level = count > 0 ? Math.min(1, Math.sqrt(sum / count)) : 0;
                useAppStore.getState().patch({ volume: level });
              }
            } catch {
              setLiveStatus("reconnecting");
              socket.current?.close();
            }
          }
        })
        .catch((cause) => {
          nativeLiveActive.current = false;
          if (nativeLiveTimer.current) {
            window.clearInterval(nativeLiveTimer.current);
            nativeLiveTimer.current = undefined;
          }
          setMeterNote(
            `实时字幕音频采集已停止：${describeError(cause, "无法读取原生音频流")}（本地录音继续）`,
          );
          setLiveStatus("offline");
          socket.current?.close();
        });
    };
    // Poll at ~50ms intervals (matches WASAPI 50ms buffer + 10ms poll sleep)
    nativeLiveTimer.current = window.setInterval(poll, 50);
  }

  async function stopLiveAudio(flush = false) {
    const capture = liveCapture.current;
    liveCapture.current = null;
    const stopBrowserCapture = browserCaptureStop.current;
    browserCaptureStop.current = null;
    if (nativeLiveTimer.current) {
      window.clearInterval(nativeLiveTimer.current);
      nativeLiveTimer.current = undefined;
    }
    nativeLiveActive.current = false;
    try {
      await capture?.stop(flush);
    } catch {
      setLiveStatus("offline");
    } finally {
      stopMeter();
      await stopBrowserCapture?.();
      // Stop native WASAPI live stream if active
      if (isTauri) {
        await bridge.stopLiveStream().catch(() => undefined);
      }
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
    setProcessingDetails(null);
    try {
      const session = await createGatewaySession("新的回声", source);
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
        // Start native WASAPI live stream for real-time captioning, reusing
        // the native capture audio source: "mic" for microphone-only,
        // "mixed" for microphone + system loopback averaged.
        const liveSource = source === "mixed" ? "mixed" : "mic";
        try {
          await bridge.startLiveStream(
            liveSource,
            micDeviceId || null,
            renderDeviceId || null,
          );
          startNativeLiveAudio();
        } catch (liveErr) {
          setMeterNote(
            `实时字幕流不可用：${describeError(liveErr, "原生音频流启动失败")}（不影响本地录音）`,
          );
        }
        // Volume meter uses a browser microphone stream; native recording
        // and captions keep working if the browser refuses microphone access.
        if (typeof navigator.mediaDevices?.getUserMedia === "function") {
          try {
            const meterMedia = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.current = meterMedia;
            startMeter(meterMedia);
          } catch {
            setMeterNote("音量计不可用（不影响录音与实时字幕）");
          }
        } else {
          setMeterNote("音量计不可用（不影响录音与实时字幕）");
        }
      } else {
        backend.current = "web";
        const browserCapture = await openBrowserAudioCapture(source);
        stream.current = browserCapture.media;
        browserCaptureStop.current = browserCapture.stop;
        browserRecordingChunks.current = [];
        webChunkIndex.current = 0;
        webRecordingId.current = `rec-${session.id}`;
        recorder.current = new MediaRecorder(browserCapture.media);
        recorder.current.ondataavailable = (event) => {
          if (event.data.size > 0) {
            browserRecordingChunks.current.push(event.data);
            const chunkIndex = webChunkIndex.current;
            webChunkIndex.current += 1;
            const recordingId = webRecordingId.current;
            if (recordingId) {
              void persistRecordingChunk(recordingId, chunkIndex, event.data).catch(
                () => undefined,
              );
            }
          }
        };
        recorder.current.start(1000);
        startLiveAudio(browserCapture.media);
      }
      recordingActive.current = true;
      if (liveCapture.current || nativeLiveActive.current) {
        connectLive(session.id);
      } else {
        setLiveStatus("offline");
      }

      state.patch({
        sessionId: session.id,
        localSessionId: null,
        sourceMode: "recording",
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
      if (backend.current === "tauri" && nativeLiveActive.current) {
        if (paused) await bridge.resumeLiveStream().catch(() => undefined);
        else await bridge.pauseLiveStream().catch(() => undefined);
      }
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
    const mockModels = result.provenance.model_manifest.filter(
      (entry) => entry.provider.toLowerCase() === "mock",
    );
    if (mockModels.length > 0) {
      throw new Error(
        "本次会话使用了 Mock 演示模型，结果已被阻止展示。请在“我的 → 提供商配置”中验证真实模型后重新录制。",
      );
    }
    const artifacts = await gateway.artifacts(context.gatewaySessionId);
    try {
      const details = await gateway.processingDetails(context.gatewaySessionId);
      (result as unknown as Record<string, unknown>)["_official_transcript"] = {
        segments: details.transcript_segments,
        truncated: details.transcript_truncated,
      };
    } catch {
      // Observability data is best-effort; the report itself stays intact.
    }

    if (context.localSessionId) {
      state.patch({ stageLabel: "正在安全保存本地报告", progress: 98 });
      await bridge.saveReportFiles(
        context.localSessionId,
        JSON.stringify(result),
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
          getGatewayUrl(),
        );
        context.uploaded = true;
      } else if (context.webRecording && !context.uploaded) {
        state.patch({ jobStatus: "uploading", progress: 8, stageLabel: "正在上传浏览器录音" });
        try {
          await gateway.uploadBlob(
            context.gatewaySessionId,
            context.webRecording,
            source === "mixed" ? "mixed" : "microphone",
          );
        } catch (uploadCause) {
          throw new Error(
            `浏览器录音上传失败：${describeError(uploadCause, "网络或服务端错误")}（录音数据仍保留在本地，可重试）`,
          );
        }
        context.uploaded = true;
        context.webRecording = undefined;
        const recordingId = webRecordingId.current;
        webRecordingId.current = null;
        if (recordingId) {
          void clearRecordingChunks(recordingId)
            .then(() => listRecoverableRecordings())
            .then((recordings) => setRecoveredRecordings(recordings))
            .catch(() => undefined);
        }
        // Immediately reflect upload success in the processing details panel.
        state.patch({ jobStatus: "uploading", progress: 12, stageLabel: "浏览器录音上传完成，正在提交分析" });
        try {
          const details = await gateway.processingDetails(context.gatewaySessionId);
          setProcessingDetails(details);
        } catch {
          // Details are supplementary; the upload succeeded regardless.
        }
      } else if (!context.localSessionId && !context.uploaded) {
        throw new Error("浏览器录音不可用：录音数据为空或未成功录制，请重新录制");
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
        throw new Error(current.error_detail ?? current.error_code ?? "分析失败");
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
      const session = await createGatewaySession(title, "import");
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
        localSessionId: context.localSessionId,
        sourceMode: "import",
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
        throw new Error(current.error_detail ?? current.error_code ?? "分析失败");
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
    let webRecording: Blob | undefined;
    try {
      if (backend.current === "tauri") {
        const stopped = await bridge.stopCapture();
        localSessionId.current = stopped.session_id;
        state.patch({ localSessionId: stopped.session_id });
      } else {
        const currentRecorder = recorder.current;
        if (!currentRecorder) throw new Error("浏览器录音器不可用");
        webRecording = await new Promise<Blob>((resolve, reject) => {
          currentRecorder.onstop = () => {
            resolve(
              new Blob(browserRecordingChunks.current, {
                type: currentRecorder.mimeType || "audio/webm",
              }),
            );
          };
          currentRecorder.onerror = () => reject(new Error("浏览器录音结束失败：MediaRecorder 异常"));
          currentRecorder.stop();
        });
        recorder.current = null;
        if (webRecording.size === 0) {
          throw new Error("浏览器录音数据为空：录音时长过短或未成功采集音频，请重新录制");
        }
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
      webRecording,
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
        <ProcessingDetailsPanel details={processingDetails} showEmpty />
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
            disabled={!isTauri && !canCaptureBrowserAudio}
            title={
              !isTauri && !canCaptureBrowserAudio
                ? "当前浏览器不支持共享标签页音频"
                : undefined
            }
          >
            <AudioLines size={16} />
            {isTauri ? "麦克风＋系统声音" : "麦克风＋标签页声音"}
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
        {isTauri && source === "microphone" && (
          <p className="live-scope-note">
            实时字幕仅使用麦克风；正式报告仍使用本地麦克风＋系统输出双轨录音。
          </p>
        )}
        {isTauri && source === "mixed" && (
          <p className="live-scope-note">
            实时字幕使用麦克风＋系统声音混合音源（WASAPI）；正式报告仍使用本地双轨录音。
          </p>
        )}
        {!isTauri && source === "mixed" && (
          <p className="live-scope-note">
            开始录音后请选择 Chrome 标签页，并勾选“分享标签页音频”；浏览器会同时混合麦克风。
          </p>
        )}
        <p className="backend-note">
          {isTauri
            ? "桌面原生录音 · 麦克风＋系统输出双轨（WASAPI）"
            : canCaptureBrowserAudio
              ? "网页调试模式 · 麦克风或用户授权的 Chrome 标签页音频"
              : "网页调试模式 · 当前浏览器仅支持麦克风"}
        </p>
        {gatewayProvider === "mock" && (
          <p className="live-scope-note">
            Mock 模式仅返回固定演示字幕，不会识别实际说话内容；真实字幕需要使用百炼实时 ASR。
          </p>
        )}
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
            {!isTauri && (
              <p className="recording-memory-note">
                停止前录音仅保存在本页内存，并分块缓存到浏览器本地；刷新或关闭页面会中断录音。
              </p>
            )}
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
      {!isTauri &&
        recoveredRecordings.length > 0 &&
        state.soulState === "idle" && (
          <div className="chunk-recovery-banner" role="status">
            <p>检测到未上传完成的浏览器录音缓存，可分别下载保存或清除：</p>
            {recoveredRecordings.map((recording) => (
              <div key={recording.recordingId} className="recovery-row">
                <span>
                  {recording.recordingId} · {recording.chunkCount} 个分块 ·{" "}
                  {(recording.totalBytes / 1024 / 1024).toFixed(1)} MB
                </span>
                <div className="recovery-actions">
                  <button
                    type="button"
                    onClick={() => void downloadRecoveredRecording(recording)}
                  >
                    下载
                  </button>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void discardRecoveredRecording(recording)}
                  >
                    清除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      {gatewayOk === false && (
        <div className="error-banner" role="alert">
          <p>{gatewayError}</p>
          <button
            type="button"
            className="retry-btn"
            onClick={() => void retryGatewayCheck()}
          >
            重试连接
          </button>
        </div>
      )}
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

type SavedEchoReport = { minutes?: { summary?: unknown } };

function HistoricalEchoesPage() {
  const { result, localSessionId, setPage, patch: patchStore } = useAppStore();
  const isTauri = isTauriRuntime();
  const [recoverable, setRecoverable] = useState<RecoveryMeta[]>([]);
  const [sessions, setSessions] = useState<import("./lib/tauri").LocalSession[]>([]);
  const [reports, setReports] = useState<Record<string, SavedEchoReport | null>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!isTauri) return;
    setLoading(true);
    try {
      const [nextRecoverable, listedResult] = await Promise.all([
        bridge.listRecoverableSessions(),
        bridge.listLocalSessions(),
      ]);
      const listed = listedResult ?? [];
      const entries = await Promise.all(listed.map(async (session) => {
        try {
          const bundle = await bridge.getAnalysisResults(session.id);
          const saved = bundle.results.slice().reverse().find((item) => item.analysis_type === "report");
          return [session.id, saved ? JSON.parse(saved.content_json) as SavedEchoReport : null] as const;
        } catch {
          return [session.id, null] as const;
        }
      }));
      setSessions(listed);
      setRecoverable(nextRecoverable ?? []);
      setReports(Object.fromEntries(entries));
      setError("");
    } catch (cause) {
      setError(describeError(cause, "Unable to read saved echo sessions"));
    } finally {
      setLoading(false);
    }
  }, [isTauri]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function recover(sessionId: string) {
    setBusyId(sessionId);
    try {
      await bridge.recoverSession(sessionId);
      await refresh();
    } catch (cause) {
      setError(describeError(cause, "Unable to recover this local session"));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(sessionId: string) {
    setBusyId(sessionId);
    try {
      await bridge.deleteLocalSession(sessionId);
      if (localSessionId === sessionId) patchStore({ result: null, localSessionId: null, page: "echoes" });
      await refresh();
    } catch (cause) {
      setError(describeError(cause, "Unable to delete this local session"));
    } finally {
      setBusyId(null);
    }
  }

  function openReport(sessionId: string) {
    const saved = reports[sessionId];
    if (!saved) {
      setError("This session has no saved report yet.");
      return;
    }
    const session = sessions.find((item) => item.id === sessionId);
    patchStore({
      result: saved as never,
      localSessionId: sessionId,
      sourceMode: session?.source_mode === "import" ? "import" : "recording",
      page: "report",
    });
  }

  if (!isTauri) {
    return <EchoesPage />;
  }
  return (
    <section className="list-page">
      <p className="eyebrow">YOUR ECHOES</p>
      <h1>本地保存的会话，随时可以回看。</h1>
      {recoverable.length > 0 ? (
        <div className="recovery-section">
          <p className="eyebrow">INTERRUPTED · 被中断的录音</p>
          <div className="recovery-list">
            {recoverable.map((meta) => (
              <article key={meta.session_id} className="recovery-card">
                <div>
                  <b>{meta.session_id.slice(0, 8)}…</b>
                  <p>{recoveryStatusLabel[meta.status]} · 开始于 {formatStarted(meta.started_at)}</p>
                </div>
                <div className="recovery-actions">
                  <button type="button" onClick={() => void recover(meta.session_id)} disabled={busyId === meta.session_id}><RotateCcw size={14} /> 恢复</button>
                  <button type="button" className="danger" onClick={() => void remove(meta.session_id)} disabled={busyId === meta.session_id}><Trash2 size={14} /> 删除</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}
      {loading ? <p className="recovery-empty">正在读取本地会话…</p> : null}
      {!loading && sessions.length === 0 ? <p className="recovery-empty">还没有保存的本地会话。</p> : null}
      <div className="session-list" aria-label="Saved echo sessions">
        {sessions.map((session) => {
          const saved = reports[session.id];
          const summary = saved?.minutes?.summary ? String(saved.minutes.summary) : `状态：${session.status}`;
          return (
            <article key={session.id}>
              <time>{formatStarted(session.started_at)}</time>
              <div><b>{session.title || session.source_name || session.id}</b><p>{summary}</p></div>
              <div className="recovery-actions">
                <button type="button" disabled={!saved || busyId === session.id} onClick={() => openReport(session.id)}>{saved ? "打开报告" : "暂无报告"}</button>
                <button type="button" className="danger" disabled={busyId === session.id} onClick={() => void remove(session.id)}><Trash2 size={14} /> 删除</button>
              </div>
            </article>
          );
        })}
      </div>
      {error ? <p className="error-banner">{error}</p> : null}
    </section>
  );
}

function LegacyRelationsPage() {
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

function localSessionSummary(session: import("./lib/tauri").LocalSession) {
  const status = session.status === "completed"
    ? "complete"
    : session.status === "failed"
      ? "failed"
      : "draft";
  return {
    id: session.id,
    title: session.title ?? session.source_name ?? session.id,
    context: session.source_mode === "import" ? "import" : "recording",
    occurred_at: session.started_at,
    duration_ms: Math.max(0, Math.round((session.duration_secs ?? 0) * 1000)),
    status: status as import("@memecho/contracts").SessionSummary["status"],
    participant_count: 0,
    has_result: false,
  };
}

function parseRelationMetadata(value: string | null): {
  memoryIds: string[];
  patternId?: string;
  patternLabel?: string;
} {
  if (!value) return { memoryIds: [] };
  try {
    const metadata = JSON.parse(value) as Record<string, unknown>;
    const rawIds = metadata.memory_ids ?? metadata.memory_id;
    const memoryIds = Array.isArray(rawIds)
      ? rawIds.filter((item): item is string => typeof item === "string")
      : typeof rawIds === "string"
        ? [rawIds]
        : [];
    return {
      memoryIds,
      patternId: typeof metadata.pattern_id === "string" ? metadata.pattern_id : undefined,
      patternLabel:
        typeof metadata.pattern_label === "string" ? metadata.pattern_label : undefined,
    };
  } catch {
    return { memoryIds: [] };
  }
}

async function loadRelationsData(): Promise<RelationsData> {
  const localSessions = await bridge.listLocalSessions();
  const bundles = await Promise.all(
    localSessions.map(async (session) => ({
      session,
      bundle: await bridge.getAnalysisResults(session.id),
      relations: await bridge.listSourceRelations(session.id),
    })),
  );
  const sessions = bundles.map(({ session, bundle }) => ({
    ...localSessionSummary(session),
    has_result: bundle.results.length > 0,
  }));
  const memoryCandidates = bundles.flatMap(({ bundle }) =>
    bundle.memory_candidates
      .filter((candidate) => candidate.confirmed && candidate.segment_id)
      .map((candidate) => ({
        id: candidate.id,
        session_id: candidate.session_id,
        segment_id: candidate.segment_id as string,
        label:
          candidate.content.length > 32
            ? `${candidate.content.slice(0, 32)}…`
            : candidate.content,
        summary: candidate.content,
        confirmed: true,
        confirmed_at: candidate.created_at,
      })),
  );
  const confirmedIds = new Set(memoryCandidates.map((candidate) => candidate.id));
  const sourceRelations = bundles.flatMap(({ relations }) =>
    relations.flatMap((relation) => {
      const metadata = parseRelationMetadata(relation.metadata_json);
      if (metadata.memoryIds.length === 0) return [];
      const patternId = metadata.patternId ?? relation.relation_type;
      const patternLabel = metadata.patternLabel ?? relation.relation_type;
      return metadata.memoryIds
        .filter((memoryId) => confirmedIds.has(memoryId))
        .map((memoryId) => ({
          id: `${relation.id}:${memoryId}`,
          memory_id: memoryId,
          pattern_id: patternId,
          pattern_label: patternLabel,
        }));
    }),
  );
  return { sessions, memoryCandidates, sourceRelations };
}

function RelationsPage() {
  const { relations, setRelations, patch, setPage } = useAppStore();
  const isTauri = isTauriRuntime();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!isTauri) return;
    setLoading(true);
    try {
      setRelations(await loadRelationsData());
      setError("");
    } catch (cause) {
      setError(describeError(cause, "无法读取本地关系数据"));
    } finally {
      setLoading(false);
    }
  }, [isTauri, setRelations]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openSource = useCallback(
    async (sessionId: string, _segmentId: string) => {
      try {
        const bundle = await bridge.getAnalysisResults(sessionId);
        const report = [...bundle.results]
          .reverse()
          .find((entry) => entry.analysis_type === "report");
        if (!report) {
          setError("该来源尚未生成可打开的分析报告");
          return;
        }
        const parsed = JSON.parse(report.content_json);
        const summary = useAppStore
          .getState()
          .relations.sessions.find((item) => item.id === sessionId);
        patch({
          result: parsed,
          localSessionId: sessionId,
          sourceMode: summary?.context === "import" ? "import" : "recording",
          page: "report",
        });
        setPage("report");
      } catch (cause) {
        setError(describeError(cause, "无法打开来源报告"));
      }
    },
    [patch, setPage],
  );

  if (loading && relations.sessions.length === 0) {
    return <section className="relations-page"><p className="eyebrow">MEMORY ECHO</p><p>正在读取已确认的本地记忆…</p></section>;
  }
  return (
    <section className="relations-page">
      {error && <p className="error-banner">{error}</p>}
      <RelationsView
        sessions={relations.sessions}
        memoryCandidates={relations.memoryCandidates}
        sourceRelations={relations.sourceRelations}
        onOpenSource={(sessionId, segmentId) => void openSource(sessionId, segmentId)}
      />
    </section>
  );
}

const CAPABILITY_LABELS: Record<string, string> = {
  realtime_asr: "实时转写",
  file_transcription: "文件转写",
  diarization: "说话人分离",
  audio_emotion: "语音情绪",
  text_analysis: "文本分析",
};

const PROFILE_ERROR_LABELS: Record<string, string> = {
  provider_auth_failed: "认证失败（密钥无效或已过期）",
  credential_unresolved: "无法从系统凭据库读取密钥",
  endpoint_unreachable: "无法连接到服务端点",
  profile_not_configured: "配置不完整",
  upstream_error: "上游服务返回错误",
  profile_not_found: "配置不存在",
  profile_in_use: "配置仍被会话使用，无法删除",
};

function ProviderProfilesSection() {
  const [profiles, setProfiles] = useState<ProviderProfile[]>([]);
  const [activeId, setActiveId] = useState(getActiveProviderProfileId());
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<ProviderProfile["provider"]>("bailian");
  const [textBaseUrl, setTextBaseUrl] = useState("");
  const [textModel, setTextModel] = useState("");
  const [audioBaseUrl, setAudioBaseUrl] = useState("");
  const [realtimeWsUrl, setRealtimeWsUrl] = useState("");
  const [realtimeModel, setRealtimeModel] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [verifyingId, setVerifyingId] = useState("");
  const [verifications, setVerifications] = useState<Record<string, ProfileVerification>>({});
  const [configPath, setConfigPath] = useState("");

  const refresh = useCallback(async () => {
    try {
      setProfiles(await gateway.listProfiles());
    } catch {
      setProfiles([]);
    }
    try {
      const status = await gateway.profileConfigStatus();
      setConfigPath(status.path);
    } catch {
      if (isTauriRuntime()) {
        setConfigPath(await bridge.getProviderProfilesConfigPath().catch(() => ""));
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = useCallback(async () => {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      if (!name.trim()) throw new Error("请输入配置名称");
      const created = await gateway.createProfile({
        name: name.trim(),
        provider,
        text_base_url: textBaseUrl.trim(),
        text_model: textModel.trim(),
        audio_base_url: audioBaseUrl.trim(),
        realtime_ws_url: realtimeWsUrl.trim(),
        realtime_model: realtimeModel.trim(),
        workspace_id: workspaceId.trim(),
      });
      if (provider !== "mock" && apiKey.trim()) {
        const credentialRef = await saveProfileApiKey(created.id, apiKey.trim());
        await gateway.updateProfile(created.id, { credential_ref: credentialRef });
      }
      setActiveId(created.id);
      setActiveProviderProfileId(created.id);
      setName("");
      setTextBaseUrl("");
      setTextModel("");
      setAudioBaseUrl("");
      setRealtimeWsUrl("");
      setRealtimeModel("");
      setWorkspaceId("");
      setApiKey("");
      await refresh();
      setNotice("配置已创建并设为默认；密钥仅保存在系统凭据库中。");
    } catch (cause) {
      setError(describeError(cause, "保存失败"));
    } finally {
      setSaving(false);
    }
  }, [name, provider, textBaseUrl, textModel, audioBaseUrl, realtimeWsUrl, realtimeModel, workspaceId, apiKey, refresh]);

  const verify = useCallback(async (profileId: string) => {
    setVerifyingId(profileId);
    setError("");
    try {
      const result = await gateway.verifyProfile(profileId);
      setVerifications((prev) => ({ ...prev, [profileId]: result }));
    } catch (cause) {
      setError(describeError(cause, "验证失败"));
    } finally {
      setVerifyingId("");
    }
  }, []);

  const remove = useCallback(async (profile: ProviderProfile) => {
    setError("");
    try {
      await gateway.deleteProfile(profile.id);
      await deleteProfileApiKey(profile.id);
      if (activeId === profile.id) {
        setActiveId("");
        setActiveProviderProfileId("");
      }
      setVerifications((prev) => {
        const next = { ...prev };
        delete next[profile.id];
        return next;
      });
      await refresh();
    } catch (cause) {
      setError(describeError(cause, "删除失败"));
    }
  }, [activeId, refresh]);

  const reloadConfigFile = useCallback(async () => {
    setError("");
    setNotice("");
    try {
      const status = await gateway.reloadProfileConfig();
      setConfigPath(status.path);
      await refresh();
      setNotice(`已从配置文件重新载入 ${status.profiles} 项配置。`);
    } catch (cause) {
      setError(describeError(cause, "重新载入配置文件失败"));
    }
  }, [refresh]);

  const openConfigFile = useCallback(async () => {
    setError("");
    try {
      await bridge.openProviderProfilesConfig();
      setNotice("已打开配置文件；修改并保存后，请点击“重新载入文件”。");
    } catch (cause) {
      setError(describeError(cause, "打开配置文件失败"));
    }
  }, []);

  return (
    <article>
      <h2>提供商配置（BYOK）</h2>
      <p>
        一个配置统一整段会话的分析链路（实时字幕到正式报告）。API Key
        只保存在 Windows Credential Manager，不会写入数据库、日志或网络响应。
      </p>
      <div style={{ marginBottom: 14 }}>
        <p className="gateway-hint" style={{ overflowWrap: "anywhere", marginBottom: 6 }}>
          配置文件：{configPath || "正在准备 provider_profiles.json…"}
        </p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {isTauriRuntime() && (
            <button type="button" onClick={() => void openConfigFile()}>
              打开配置文件
            </button>
          )}
          <button type="button" onClick={() => void reloadConfigFile()}>
            重新载入文件
          </button>
        </div>
        <p className="gateway-hint" style={{ marginTop: 6 }}>
          Endpoint、模型及 Workspace 可直接编辑；API Key 不写入文件，仍由 Windows Credential Manager 保存。
        </p>
      </div>
      {profiles.length === 0 && (
        <p className="gateway-hint">尚无配置；网关当前使用环境变量默认值。</p>
      )}
      {profiles.map((profile) => {
        const result = verifications[profile.id];
        return (
          <div key={profile.id} style={{ marginBottom: 12 }}>
            <p style={{ marginBottom: 4 }}>
              <strong>{profile.name}</strong> · {profile.provider}
              {profile.credential_ref ? " · 密钥已配置" : " · 未配置密钥"}
            </p>
            <p className="gateway-hint" style={{ marginBottom: 4 }}>
              能力: {profile.capabilities.map((cap) => CAPABILITY_LABELS[cap] ?? cap).join("、")}
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                disabled={activeId === profile.id}
                onClick={() => {
                  setActiveId(profile.id);
                  setActiveProviderProfileId(profile.id);
                }}
              >
                {activeId === profile.id ? "使用中" : "设为默认"}
              </button>
              <button
                type="button"
                disabled={verifyingId === profile.id}
                onClick={() => void verify(profile.id)}
              >
                {verifyingId === profile.id ? "验证中…" : "验证连接"}
              </button>
              <button type="button" onClick={() => void remove(profile)}>
                删除
              </button>
            </div>
            {result && (
              <div className={result.ok ? "gateway-ok" : "error-banner"} style={{ marginTop: 6 }}>
                {result.ok
                  ? "✓ 连接正常"
                  : `✗ ${PROFILE_ERROR_LABELS[result.error_code ?? ""] ?? result.error_code ?? "验证失败"}`}
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {result.capabilities.map((probe) => (
                    <li key={probe.capability}>
                      {CAPABILITY_LABELS[probe.capability] ?? probe.capability}
                      {probe.status === "ok" && "：✓"}
                      {probe.status === "failed" &&
                        `：✗ ${PROFILE_ERROR_LABELS[probe.error_code ?? ""] ?? probe.error_code ?? "失败"}`}
                      {probe.status === "unavailable" && "：不适用"}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        );
      })}
      <h3 style={{ marginTop: 12 }}>新建配置</h3>
      <div className="gateway-config-row">
        <input
          type="text"
          value={name}
          onChange={(e) => { setName(e.target.value); setNotice(""); }}
          placeholder="名称，如 百炼-生产"
          className="gateway-url-input"
        />
      </div>
      <div className="gateway-config-row" style={{ marginTop: 4 }}>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value as ProviderProfile["provider"])}
          className="gateway-url-input"
        >
          <option value="bailian">阿里云百炼（语音 + 文本）</option>
          <option value="openai_compatible">OpenAI 兼容接口（仅文本）</option>
          <option value="mock">本地 Mock（无需密钥）</option>
        </select>
      </div>
      {provider !== "mock" && (
        <>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input
              type="text"
              value={textBaseUrl}
              onChange={(e) => setTextBaseUrl(e.target.value)}
              placeholder="文本 Endpoint，如 https://dashscope.aliyuncs.com/compatible-mode/v1"
              className="gateway-url-input"
            />
          </div>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input
              type="text"
              value={textModel}
              onChange={(e) => setTextModel(e.target.value)}
              placeholder="文本模型，如 qwen-plus"
              className="gateway-url-input"
            />
          </div>
          {provider === "bailian" && (
            <>
              <div className="gateway-config-row" style={{ marginTop: 4 }}>
                <input
                  type="text"
                  value={audioBaseUrl}
                  onChange={(e) => setAudioBaseUrl(e.target.value)}
                  placeholder="语音 Endpoint（可选，默认网关配置）"
                  className="gateway-url-input"
                />
              </div>
              <div className="gateway-config-row" style={{ marginTop: 4 }}>
                <input
                  type="text"
                  value={realtimeWsUrl}
                  onChange={(e) => setRealtimeWsUrl(e.target.value)}
                  placeholder="实时 WebSocket 地址（可选）"
                  className="gateway-url-input"
                />
              </div>
              <div className="gateway-config-row" style={{ marginTop: 4 }}>
                <input
                  type="text"
                  value={realtimeModel}
                  onChange={(e) => setRealtimeModel(e.target.value)}
                  placeholder="实时模型（可选）"
                  className="gateway-url-input"
                />
              </div>
              <div className="gateway-config-row" style={{ marginTop: 4 }}>
                <input
                  type="text"
                  value={workspaceId}
                  onChange={(e) => setWorkspaceId(e.target.value)}
                  placeholder="Workspace ID（可选）"
                  className="gateway-url-input"
                />
              </div>
            </>
          )}
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API Key（仅保存在系统凭据库）"
              autoComplete="off"
              className="gateway-url-input"
            />
          </div>
        </>
      )}
      <button
        type="button"
        style={{ marginTop: 8 }}
        disabled={saving || !name.trim()}
        onClick={() => void save()}
      >
        {saving ? "保存中…" : "创建配置"}
      </button>
      {notice && <p className="gateway-ok">✓ {notice}</p>}
      {error && <p className="error-banner">✗ {error}</p>}
    </article>
  );
}

function SettingsPage() {
  const [gwUrl, setGwUrl] = useState("");
  const [gwDraft, setGwDraft] = useState("");
  const [gwStatus, setGwStatus] = useState<"idle" | "testing" | "ok" | "fail">("idle");
  const [gwError, setGwError] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenSaved, setTokenSaved] = useState(false);
  const [tokenError, setTokenError] = useState("");
  const isTauri = isTauriRuntime();

  // LLM config state
  const [textEndpoint, setTextEndpoint] = useState("");
  const [textModel, setTextModel] = useState("");
  const [textApiKeySaved, setTextApiKeySaved] = useState(false);
  const [textDraftEndpoint, setTextDraftEndpoint] = useState("");
  const [textDraftModel, setTextDraftModel] = useState("");
  const [textDraftApiKey, setTextDraftApiKey] = useState("");
  const [textLlmStatus, setTextLlmStatus] = useState<"idle" | "testing" | "ok" | "fail">("idle");
  const [textLlmError, setTextLlmError] = useState("");

  const [audioEndpoint, setAudioEndpoint] = useState("");
  const [audioApiKeySaved, setAudioApiKeySaved] = useState(false);
  const [audioDraftEndpoint, setAudioDraftEndpoint] = useState("");
  const [audioDraftApiKey, setAudioDraftApiKey] = useState("");
  const [audioWorkspaceId, setAudioWorkspaceId] = useState("");
  const [audioDraftWorkspaceId, setAudioDraftWorkspaceId] = useState("");
  const [audioAsrStatus, setAudioAsrStatus] = useState<"idle" | "testing" | "ok" | "fail">("idle");
  const [audioAsrError, setAudioAsrError] = useState("");

  useEffect(() => {
    initGatewayConfig().then((url) => {
      setGwUrl(url);
      setGwDraft(url);
      setTokenSaved(hasGatewayToken());
    });
    if (isTauri) {
      initLlmConfig().then(() => {
        const s = getLlmConfigState();
        setTextEndpoint(s.textEndpoint);
        setTextDraftEndpoint(s.textEndpoint);
        setTextModel(s.textModel);
        setTextDraftModel(s.textModel);
        setTextApiKeySaved(s.hasTextApiKey);
        setAudioEndpoint(s.audioEndpoint);
        setAudioDraftEndpoint(s.audioEndpoint);
        setAudioApiKeySaved(s.hasAudioApiKey);
        setAudioWorkspaceId(s.workspaceId);
        setAudioDraftWorkspaceId(s.workspaceId);
      });
    }
  }, [isTauri]);

  const saveToken = useCallback(async () => {
    setTokenError("");
    try {
      await setGatewayToken(tokenDraft);
      setTokenDraft("");
      setTokenSaved(true);
    } catch (cause) {
      setTokenError(cause instanceof Error ? cause.message : "无法保存访问令牌");
    }
  }, [tokenDraft]);

  const testAndSave = useCallback(async () => {
    setGwStatus("testing");
    setGwError("");
    try {
      // Validate: production must be HTTPS; dev allows localhost HTTP
      const parsed = new URL(gwDraft);
      const isLocalhost =
        parsed.hostname === "localhost" ||
        parsed.hostname === "127.0.0.1" ||
        parsed.hostname === "[::1]";
      if (parsed.protocol === "http:" && !isLocalhost) {
        setGwStatus("fail");
        setGwError("HTTP is only allowed for localhost — production requires HTTPS");
        return;
      }
      // Test connectivity
      if (isTauri) {
        const result = await bridge.checkGateway(gwDraft);
        if (result.ok) {
          await setGatewayUrl(gwDraft);
          setGwUrl(gwDraft);
          setGwStatus("ok");
        } else {
          setGwStatus("fail");
          setGwError(result.error ?? "Gateway is not reachable");
        }
      } else {
        // Web mode: try health endpoint directly
        const resp = await fetch(`${gwDraft}/v1/health`, { signal: AbortSignal.timeout(5000) });
        if (resp.ok) {
          await setGatewayUrl(gwDraft);
          setGwUrl(gwDraft);
          setGwStatus("ok");
        } else {
          setGwStatus("fail");
          setGwError(`Gateway returned HTTP ${resp.status}`);
        }
      }
    } catch (cause) {
      setGwStatus("fail");
      if (cause instanceof TypeError) {
        setGwError("Invalid URL format");
      } else {
        setGwError(cause instanceof Error ? cause.message : "Connection failed");
      }
    }
  }, [gwDraft, isTauri]);

  return (
    <section className="settings-page">
      <p className="eyebrow">YOUR SPACE</p>
      <h1>你的声音，首先属于你。</h1>
      <div className="settings-grid">
        <ProviderProfilesSection />
        <article>
          <h2>分析网关</h2>
          <p>
            当前地址: <code>{gwUrl || "未配置"}</code>
          </p>
          <div className="gateway-config-row">
            <input
              type="text"
              value={gwDraft}
              onChange={(e) => {
                setGwDraft(e.target.value);
                setGwStatus("idle");
              }}
              placeholder="https://your-gateway.example.com"
              className="gateway-url-input"
            />
            <button
              type="button"
              onClick={() => void testAndSave()}
              disabled={gwStatus === "testing" || gwDraft === gwUrl}
            >
              {gwStatus === "testing" ? "测试中…" : "测试并保存"}
            </button>
          </div>
          {gwStatus === "ok" && <p className="gateway-ok">✓ 连接成功，已保存</p>}
          {gwStatus === "fail" && (
            <p className="error-banner">
              ✗ {gwError}
              <br />
              <small>网关地址未更改，仍为: {gwUrl}</small>
            </p>
          )}
          <p className="gateway-hint">
            本地开发: http://127.0.0.1:8787 · 生产: 必须为 HTTPS 地址
          </p>
          {isTauri && (
            <div className="gateway-token-config">
              <label htmlFor="gateway-token">访问令牌</label>
              <div className="gateway-config-row">
                <input
                  id="gateway-token"
                  type="password"
                  value={tokenDraft}
                  autoComplete="off"
                  onChange={(event) => {
                    setTokenDraft(event.target.value);
                    setTokenError("");
                  }}
                  placeholder={tokenSaved ? "已安全保存；输入新值可替换" : "输入 Gateway 访问令牌"}
                  className="gateway-url-input"
                />
                <button
                  type="button"
                  onClick={() => void saveToken()}
                  disabled={!tokenDraft.trim()}
                >
                  安全保存
                </button>
              </div>
              <p className="gateway-hint">
                {tokenSaved
                  ? "令牌已保存在 Windows Credential Manager，不会写入项目或配置文件。"
                  : "尚未配置令牌；分析请求将不可用。"}
              </p>
              {tokenError && <p className="error-banner">{tokenError}</p>}
            </div>
          )}
        </article>
        <article>
          <h2>文本分析模型</h2>
          <p>
            当前 Endpoint: <code>{textEndpoint || "使用网关默认"}</code>
            {textModel && <> · 模型: <code>{textModel}</code></>}
          </p>
          <div className="gateway-config-row">
            <input type="text" value={textDraftEndpoint} onChange={(e) => { setTextDraftEndpoint(e.target.value); setTextLlmStatus("idle"); }} placeholder="https://api.openai.com/v1" className="gateway-url-input" />
          </div>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input type="text" value={textDraftModel} onChange={(e) => { setTextDraftModel(e.target.value); setTextLlmStatus("idle"); }} placeholder="模型名称，如 gpt-4o" className="gateway-url-input" />
          </div>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input type="password" value={textDraftApiKey} onChange={(e) => { setTextDraftApiKey(e.target.value); setTextLlmStatus("idle"); }} placeholder={textApiKeySaved ? "已安全保存；输入新值可替换" : "API Key"} autoComplete="off" className="gateway-url-input" />
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button type="button" disabled={textLlmStatus === "testing"} onClick={async () => {
              setTextLlmStatus("testing"); setTextLlmError("");
              try {
                if (textDraftEndpoint || textDraftModel || textDraftApiKey) {
                  await setLlmConfig({ textEndpoint: textDraftEndpoint, textModel: textDraftModel, textApiKey: textDraftApiKey || undefined });
                }
                const r = await gateway.testLlmConnection("text");
                setTextLlmStatus(r.ok ? "ok" : "fail");
                if (!r.ok) setTextLlmError(r.error ?? "连接失败");
                if (r.ok) { setTextEndpoint(textDraftEndpoint); setTextModel(textDraftModel); setTextApiKeySaved(!!textDraftApiKey || textApiKeySaved); }
              } catch (e) { setTextLlmStatus("fail"); setTextLlmError(e instanceof Error ? e.message : "操作失败"); }
            }}>{textLlmStatus === "testing" ? "测试中…" : "测试并保存"}</button>
            <button type="button" onClick={async () => {
              await setLlmConfig({ textEndpoint: textDraftEndpoint, textModel: textDraftModel, ...(textDraftApiKey ? { textApiKey: textDraftApiKey } : {}) });
              setTextEndpoint(textDraftEndpoint); setTextModel(textDraftModel); if (textDraftApiKey) setTextApiKeySaved(true); setTextDraftApiKey(""); setTextLlmStatus("ok");
            }}>仅保存</button>
            <button type="button" onClick={async () => {
              await clearLlmConfig(); setTextEndpoint(""); setTextModel(""); setTextApiKeySaved(false); setTextDraftEndpoint(""); setTextDraftModel(""); setTextDraftApiKey(""); setAudioEndpoint(""); setAudioApiKeySaved(false); setAudioDraftEndpoint(""); setAudioDraftApiKey(""); setAudioWorkspaceId(""); setAudioDraftWorkspaceId(""); setTextLlmStatus("idle");
            }}>清除全部</button>
          </div>
          {textLlmStatus === "ok" && <p className="gateway-ok">✓ 配置已保存</p>}
          {textLlmStatus === "fail" && <p className="error-banner">✗ {textLlmError}</p>}
          <p className="gateway-hint">支持 OpenAI 兼容接口（/chat/completions）。API Key 安全存储在 Windows Credential Manager。</p>
        </article>
        <article>
          <h2>语音转写模型</h2>
          <p>
            当前 Endpoint: <code>{audioEndpoint || "使用网关默认"}</code>
          </p>
          <div className="gateway-config-row">
            <input type="text" value={audioDraftEndpoint} onChange={(e) => { setAudioDraftEndpoint(e.target.value); setAudioAsrStatus("idle"); }} placeholder="https://dashscope.aliyuncs.com" className="gateway-url-input" />
          </div>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input type="password" value={audioDraftApiKey} onChange={(e) => { setAudioDraftApiKey(e.target.value); setAudioAsrStatus("idle"); }} placeholder={audioApiKeySaved ? "已安全保存；输入新值可替换" : "API Key"} autoComplete="off" className="gateway-url-input" />
          </div>
          <div className="gateway-config-row" style={{ marginTop: 4 }}>
            <input type="text" value={audioDraftWorkspaceId} onChange={(e) => { setAudioDraftWorkspaceId(e.target.value); setAudioAsrStatus("idle"); }} placeholder="Workspace ID（可选）" className="gateway-url-input" />
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button type="button" disabled={audioAsrStatus === "testing"} onClick={async () => {
              setAudioAsrStatus("testing"); setAudioAsrError("");
              try {
                if (audioDraftEndpoint || audioDraftApiKey) {
                  await setLlmConfig({ audioEndpoint: audioDraftEndpoint, audioApiKey: audioDraftApiKey || undefined, workspaceId: audioDraftWorkspaceId });
                }
                const r = await gateway.testLlmConnection("audio");
                setAudioAsrStatus(r.ok ? "ok" : "fail");
                if (!r.ok) setAudioAsrError(r.error ?? "连接失败");
                if (r.ok) { setAudioEndpoint(audioDraftEndpoint); setAudioApiKeySaved(!!audioDraftApiKey || audioApiKeySaved); }
              } catch (e) { setAudioAsrStatus("fail"); setAudioAsrError(e instanceof Error ? e.message : "操作失败"); }
            }}>{audioAsrStatus === "testing" ? "测试中…" : "测试并保存"}</button>
            <button type="button" onClick={async () => {
              await setLlmConfig({ audioEndpoint: audioDraftEndpoint, ...(audioDraftApiKey ? { audioApiKey: audioDraftApiKey } : {}), workspaceId: audioDraftWorkspaceId });
              setAudioEndpoint(audioDraftEndpoint); if (audioDraftApiKey) setAudioApiKeySaved(true); setAudioDraftApiKey(""); setAudioWorkspaceId(audioDraftWorkspaceId); setAudioAsrStatus("ok");
            }}>仅保存</button>
          </div>
          {audioAsrStatus === "ok" && <p className="gateway-ok">✓ 配置已保存</p>}
          {audioAsrStatus === "fail" && <p className="error-banner">✗ {audioAsrError}</p>}
          <p className="gateway-hint">支持 DashScope 兼容接口。API Key 安全存储在 Windows Credential Manager。</p>
        </article>
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
  const { page, result, sourceMode, setPage } = useAppStore();
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" aria-label="返回此刻" onClick={() => setPage("now")}>
          <img src="/brand/memecho-wordmark.svg" alt="memEcho" />
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
        {page === "echoes" && <HistoricalEchoesPage />}
        {page === "relations" && <RelationsPage />}
        {page === "settings" && <SettingsPage />}
        {page === "report" &&
          (result ? (
            <ReportView
              result={result}
              localSessionId={useAppStore.getState().localSessionId}
              sourceMode={sourceMode}
              onBack={() => setPage("echoes")}
            />
          ) : (
            <EchoesPage />
          ))}
      </main>
    </div>
  );
}
