import type { AnalysisResult } from "@memecho/contracts";
import { ArrowLeft, CheckCircle2, MessageCircle, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { gateway } from "../lib/api";
import { bridge, type EvidenceTrack } from "../lib/tauri";
import { useAppStore } from "../store";
import { OfficialTranscript } from "./OfficialTranscript";

type VadMetric = "v" | "a" | "d";

const vadMetricConfig: Record<
  VadMetric,
  { short: string; name: string; chartLabel: string; axis: [string, string, string] }
> = {
  v: { short: "V", name: "效价", chartLabel: "参与者效价变化图", axis: ["正向", "中性", "负向"] },
  a: { short: "A", name: "唤醒度", chartLabel: "参与者唤醒度变化图", axis: ["高唤醒", "中等", "低唤醒"] },
  d: { short: "D", name: "支配感", chartLabel: "参与者支配感变化图", axis: ["主导", "中性", "顺应"] },
};

function VadChart({ result }: { result: AnalysisResult }) {
  const [metric, setMetric] = useState<VadMetric>("v");
  const points = result.vad_series;
  const lines = useMemo(() => {
    const byPerson = new Map<string, typeof points>();
    points.forEach((point) =>
      byPerson.set(point.participant_id, [
        ...(byPerson.get(point.participant_id) ?? []),
        point,
      ]),
    );
    return [...byPerson.entries()];
  }, [points]);
  const participantNames = new Map(
    result.participants.map((participant) => [participant.id, participant.name]),
  );
  const colors = ["#6f5bd3", "#de927c", "#66ad9f", "#768fbf"];
  const config = vadMetricConfig[metric];

  return (
    <>
      <div className="vad-metric-switch" role="group" aria-label="选择 VAD 指标">
        {(Object.keys(vadMetricConfig) as VadMetric[]).map((key) => (
          <button
            type="button"
            key={key}
            aria-pressed={metric === key}
            onClick={() => setMetric(key)}
          >
            <b>{vadMetricConfig[key].short}</b>
            <span>{vadMetricConfig[key].name}</span>
          </button>
        ))}
      </div>
      <div className="vad-chart" role="img" aria-label={config.chartLabel}>
        <div className="chart-grid" aria-hidden="true">
          {config.axis.map((label) => (
            <span key={label}>{label}</span>
          ))}
        </div>
        <svg viewBox="0 0 720 260" aria-hidden="true" focusable="false">
          {[45, 130, 215].map((y) => (
            <line key={y} x1="36" y1={y} x2="700" y2={y} />
          ))}
          {lines.map(([participant, values], lineIndex) => {
            const path = values
              .map((point, index) => {
                const x = 70 + (index / Math.max(values.length - 1, 1)) * 590;
                const y = 130 - point[metric] * 92;
                return `${index ? "L" : "M"}${x},${y}`;
              })
              .join(" ");
            return (
              <g key={participant}>
                <path d={path} style={{ stroke: colors[lineIndex % colors.length] }} />
                {values.map((point, index) => {
                  const x = 70 + (index / Math.max(values.length - 1, 1)) * 590;
                  const y = 130 - point[metric] * 92;
                  return (
                    <circle
                      key={point.segment_id}
                      cx={x}
                      cy={y}
                      r="5"
                      style={{ fill: colors[lineIndex % colors.length] }}
                    />
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
      <div className="vad-legend" aria-label="图表参与者图例">
        {lines.map(([participant], index) => (
          <span key={participant}>
            <i style={{ background: colors[index % colors.length] }} />
            {participantNames.get(participant) ?? participant}
          </span>
        ))}
      </div>
    </>
  );
}

function AnalysisList({ title, items }: { title: string; items: string[] }) {
  return (
    <section>
      <h4>{title}</h4>
      {items.length > 0 ? (
        <ul>
          {items.map((item, index) => (
            <li key={`${title}-${index}`}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="empty-analysis">本次未提取</p>
      )}
    </section>
  );
}

export function ReportView({
  result,
  onBack,
  localSessionId = null,
  sourceMode = "recording",
}: {
  result: AnalysisResult;
  onBack: () => void;
  localSessionId?: string | null;
  sourceMode?: "recording" | "import";
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [asking, setAsking] = useState(false);
  const [selectedEvidenceIds, setSelectedEvidenceIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [playingEvidenceId, setPlayingEvidenceId] = useState<string | null>(null);
  const [clipUrl, setClipUrl] = useState<string | null>(null);
  const [clipStatus, setClipStatus] = useState<string>("");
  const gatewaySessionId = useAppStore((state) => state.sessionId);
  useEffect(() => {
    return () => {
      if (clipUrl) URL.revokeObjectURL(clipUrl);
    };
  }, [clipUrl]);
  const playEvidence = async (evidence: (typeof result.evidence)[number]) => {
    if (sourceMode === "import") {
      setClipStatus("导入会话暂不支持证据回听。");
      return;
    }
    const track = (evidence as typeof evidence & { track?: EvidenceTrack }).track;
    if (!track) {
      setClipStatus("此证据缺少音轨信息，无法安全回听。 ");
      return;
    }
    if (!localSessionId) {
      setClipStatus("当前报告没有本地录音会话，无法回听。 ");
      return;
    }
    setPlayingEvidenceId(evidence.id);
    setClipStatus("正在读取证据片段…");
    try {
      const clip = await bridge.readEvidenceClip(
        localSessionId,
        track,
        evidence.start_ms,
        evidence.end_ms,
      );
      const binary = Uint8Array.from(atob(clip.data_base64), (char) => char.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([binary], { type: clip.mime_type }));
      setClipUrl(url);
      setClipStatus("");
    } catch {
      setClipStatus("证据片段读取失败，请稍后重试。 ");
    } finally {
      setPlayingEvidenceId(null);
    }
  };
  const participantNames = new Map(
    result.participants.map((participant) => [participant.id, participant.name]),
  );
  const toggleEvidence = (evidenceId: string) => {
    setSelectedEvidenceIds((current) => {
      const next = new Set(current);
      if (next.has(evidenceId)) next.delete(evidenceId);
      else next.add(evidenceId);
      return next;
    });
  };
  const ask = async () => {
    if (!question.trim() || asking) return;
    setAsking(true);
    setAnswer("");
    try {
      const evidenceIds = result.evidence
        .filter((evidence) => selectedEvidenceIds.has(evidence.id))
        .map((evidence) => evidence.id);
      await gateway.chat(
        question,
        result,
        (value) => setAnswer((current) => current + value),
        evidenceIds,
      );
    } catch {
      setAnswer("追问暂时失败，请稍后重试。已选择的证据不会丢失。");
    } finally {
      setAsking(false);
    }
  };
  return (
    <section className="report-page">
      <button className="back-link" onClick={onBack}>
        <ArrowLeft size={16} /> 返回回声
      </button>
      <header className="report-hero">
        <div>
          <p className="eyebrow">ECHO REPORT · 单次会谈</p>
          <h1>这次对话，发生了什么？</h1>
          <p>{result.minutes.summary}</p>
        </div>
        <div
          className="confidence-orb"
          role="img"
          aria-label={`分析质量 ${Math.round(result.scope.quality * 100)}%`}
        >
          <strong>{Math.round(result.scope.quality * 100)}%</strong>
          <span>分析质量</span>
        </div>
      </header>
      <div className="report-grid">
        <article className="glass-card span-two">
          <div className="card-title">
            <span>
              <Sparkles size={17} /> 情绪沿对话变化
            </span>
            <small>VAD · 表达层面的区间推断</small>
          </div>
          <VadChart result={result} />
          <p className="boundary">
            VAD 仅描述本次情境中的表达状态，不代表参与者真实内心或稳定性格。
            V、A、D 分别表示效价、唤醒度和支配感；所有点均应结合证据与置信度理解。
          </p>
        </article>
        <article className="glass-card">
          <p className="eyebrow">FOCUS · 关注重点</p>
          <ul className="focus-list">
            {result.minutes.focus.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </article>
        <article className="glass-card">
          <p className="eyebrow">CONSENSUS · 共识</p>
          {result.minutes.consensus.map((item) => (
            <div className="consensus-row" key={item}>
              <CheckCircle2 size={16} /> {item}
            </div>
          ))}
          <p className="eyebrow disagreement-title">OPEN · 未解决分歧</p>
          {result.minutes.disagreements.map((item) => (
            <p className="disagreement" key={item}>
              {item}
            </p>
          ))}
        </article>
        <article className="glass-card span-two">
          <div className="card-title">
            <span>参与者内容解析</span>
            <small>事实主张、观点与态度分开呈现</small>
          </div>
          <div className="participant-analysis-grid">
            {result.content_analysis.map((analysis) => {
              const name = participantNames.get(analysis.participant_id) ?? analysis.participant_id;
              const headingId = `participant-analysis-${analysis.participant_id}`;
              return (
                <section
                  className="participant-analysis"
                  key={analysis.participant_id}
                  aria-labelledby={headingId}
                >
                  <h3 id={headingId}>{name}</h3>
                  <AnalysisList title="事实主张" items={analysis.fact_claims} />
                  <AnalysisList title="观点" items={analysis.opinions} />
                  <AnalysisList title="态度" items={analysis.attitudes} />
                  <AnalysisList title="对彼此表达的影响" items={analysis.influence_summary} />
                </section>
              );
            })}
          </div>
        </article>
        <article className="glass-card span-two">
          <div className="card-title">
            <span>下一步动作</span>
            <small>确认动作与建议分开</small>
          </div>
          <div className="action-columns">
            <div>
              <h3>已确认</h3>
              {result.minutes.explicit_actions.map((action) => (
                <div className="action-row confirmed" key={action.text}>
                  <b>CONFIRMED</b>
                  <span>{action.text}</span>
                </div>
              ))}
            </div>
            <div>
              <h3>建议</h3>
              {result.minutes.recommendations.map((action) => (
                <div className="action-row proposed" key={action.text}>
                  <b>PROPOSED</b>
                  <span>{action.text}</span>
                </div>
              ))}
            </div>
          </div>
        </article>
        <article className="glass-card span-two">
          <div className="card-title">
            <span>自我回声</span>
            <small>从“我”的视角观察话术作用</small>
          </div>
          <div className="self-echo">
            {result.self_echo.effects.map((effect, index) => (
              <blockquote key={index}>
                “{String(effect.wording ?? "")}”
                <footer>{String(effect.observed_followup ?? "")}</footer>
              </blockquote>
            ))}
            {result.self_echo.alternatives.map((alternative, index) => (
              <div className="rewrite" key={index}>
                <span>低压力替代表达</span>
                <p>{String(alternative.rewrite ?? "")}</p>
              </div>
            ))}
          </div>
        </article>
        <OfficialTranscript
          result={result}
          sessionId={gatewaySessionId}
          speakerNames={participantNames}
        />
        <article className="glass-card span-two evidence-card">
          <div className="card-title">
            <span>证据与不确定性</span>
            <small>{result.evidence.length} 条证据 · 可选择后继续追问</small>
          </div>
          <div className="evidence-selection" role="group" aria-label="选择追问所使用的证据">
            {result.evidence.map((evidence) => {
              const selected = selectedEvidenceIds.has(evidence.id);
              return (
                <label
                  className={`evidence-row${selected ? " selected" : ""}`}
                  key={evidence.id}
                >
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => toggleEvidence(evidence.id)}
                    aria-label={`选择证据 ${evidence.id}`}
                  />
                  <time>{Math.floor(evidence.start_ms / 1000)}s</time>
                  <div>
                    <b>{evidence.segment_id}</b>
                    <p>{evidence.excerpt}</p>
                    <button
                      type="button"
                      className="text-btn evidence-play"
                      onClick={() => void playEvidence(evidence)}
                      disabled={playingEvidenceId === evidence.id}
                    >
                      {playingEvidenceId === evidence.id ? "读取中…" : "▶ 回听证据"}
                    </button>
                  </div>
                </label>
              );
            })}
          </div>
          {clipUrl && (
            <audio controls autoPlay src={clipUrl} aria-label="证据片段播放器" />
          )}
          {clipStatus && (
            <p className="evidence-context-note" role="status">
              {clipStatus}
            </p>
          )}
          {result.uncertainties.map((item) => (
            <p className="uncertainty" key={item}>
              {item}
            </p>
          ))}
        </article>
        <article className="glass-card span-two ask-card">
          <MessageCircle size={22} aria-hidden="true" />
          <div>
            <p className="eyebrow">CONTINUE THE ECHO</p>
            <h2>继续问 memEcho</h2>
            <p>只使用这次会谈与已选择的证据，不自动读取其他记忆。</p>
            <p className="evidence-context-note" aria-live="polite">
              {selectedEvidenceIds.size > 0
                ? `已选择 ${selectedEvidenceIds.size} 条证据作为追问上下文。`
                : "未选择证据：本次追问将明确发送空 evidence_ids。"}
            </p>
          </div>
          <div className="ask-box">
            <input
              aria-label="向 memEcho 提问"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="例如：为什么这里被识别为分歧？"
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void ask();
                }
              }}
            />
            <button onClick={ask} disabled={asking || !question.trim()}>
              {asking ? "回声中…" : "发送"}
            </button>
          </div>
          {answer && (
            <div className="echo-answer" aria-live="polite">
              {answer}
            </div>
          )}
        </article>
      </div>
    </section>
  );
}

