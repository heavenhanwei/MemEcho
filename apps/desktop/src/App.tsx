import {
  AudioLines,
  CircleUserRound,
  FileAudio,
  History,
  Network,
  Pause,
  Play,
  Radio,
  Settings,
  Square,
  Upload,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { EchoSphere } from "./components/EchoSphere";
import { ReportView } from "./components/ReportView";
import { gateway } from "./lib/api";
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

function NowPage() {
  const state = useAppStore();
  const timer = useRef<number>();
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const [source, setSource] = useState<"microphone" | "mixed">("mixed");
  const [error, setError] = useState("");

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
      stream.current?.getTracks().forEach((track) => track.stop());
      socket.current?.close();
    },
    [],
  );

  async function start() {
    if (state.soulState !== "idle") return;
    setError("");
    try {
      const session = await gateway.createSession("新的回声", source);
      const media = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      stream.current = media;
      recorder.current = new MediaRecorder(media);
      recorder.current.start(1000);
      const ws = new WebSocket(gateway.liveUrl(session.id));
      socket.current = ws;
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data);
        if (event.type === "transcript.partial" || event.type === "transcript.final") {
          state.patch({ caption: event.text });
        }
      };
      const context = new AudioContext();
      const analyser = context.createAnalyser();
      const sourceNode = context.createMediaStreamSource(media);
      sourceNode.connect(analyser);
      analyser.fftSize = 256;
      const data = new Uint8Array(analyser.frequencyBinCount);
      const measure = () => {
        if (!stream.current) return;
        analyser.getByteFrequencyData(data);
        const average = data.reduce((sum, value) => sum + value, 0) / data.length / 255;
        state.patch({ volume: average });
        requestAnimationFrame(measure);
      };
      measure();
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
      setError(cause instanceof Error ? cause.message : "无法开始录音");
    }
  }

  function togglePause() {
    const paused = state.soulState === "paused";
    if (paused) recorder.current?.resume();
    else recorder.current?.pause();
    state.patch({ soulState: paused ? "recording" : "paused" });
  }

  async function stop() {
    if (timer.current) window.clearInterval(timer.current);
    recorder.current?.stop();
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    socket.current?.send("end");
    state.patch({
      soulState: "processing",
      jobStatus: "queued",
      progress: 4,
      stageLabel: "正在安全保存本地录音",
    });
    if (!state.sessionId || !state.requestId) return;
    try {
      const job = await gateway.analyze(state.sessionId, state.requestId);
      state.patch({ jobId: job.id });
      while (true) {
        const current = await gateway.job(job.id);
        state.patch({
          jobStatus: current.status,
          progress: current.progress,
          stageLabel: current.stage_label,
        });
        if (current.status === "complete") {
          const result = await gateway.result(state.sessionId);
          state.patch({ result, soulState: "responding", page: "report" });
          break;
        }
        if (current.status === "failed") throw new Error(current.error_code ?? "分析失败");
        await new Promise((resolve) => window.setTimeout(resolve, 350));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "分析失败");
      state.patch({ soulState: "idle" });
    }
  }

  if (state.soulState === "processing") {
    return (
      <section className="processing">
        <EchoSphere state="processing" energy={0.3} />
        <p className="eyebrow">ECHO IS FORMING</p>
        <h1>回声正在成形</h1>
        <p>{state.stageLabel}</p>
        <div className="progress-track">
          <i style={{ width: `${state.progress}%` }} />
        </div>
        <strong>{state.progress}%</strong>
      </section>
    );
  }

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
        <button className="quick-card">
          <FileAudio size={21} />
          <span>
            <b>分析文本</b>
            <small>会议转写或个人独白</small>
          </span>
        </button>
      </div>
      {error && <p className="error-banner">{error}</p>}
    </section>
  );
}

function EchoesPage() {
  const { result, setPage } = useAppStore();
  return (
    <section className="list-page">
      <p className="eyebrow">YOUR ECHOES</p>
      <h1>每一次重要的对话，都会留下可以回看的形状。</h1>
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
            <ReportView result={result} onBack={() => setPage("echoes")} />
          ) : (
            <EchoesPage />
          ))}
      </main>
    </div>
  );
}

