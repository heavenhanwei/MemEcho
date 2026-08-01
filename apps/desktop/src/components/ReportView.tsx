import type { AnalysisResult } from "@memecho/contracts";
import { ArrowLeft, CheckCircle2, MessageCircle, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { gateway } from "../lib/api";

function VadChart({ result }: { result: AnalysisResult }) {
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
  const colors = ["#6f5bd3", "#de927c", "#66ad9f", "#768fbf"];
  return (
    <div className="vad-chart" role="img" aria-label="参与者效价变化图">
      <div className="chart-grid">
        <span>正向</span>
        <span>中性</span>
        <span>负向</span>
      </div>
      <svg viewBox="0 0 720 260">
        {[45, 130, 215].map((y) => (
          <line key={y} x1="36" y1={y} x2="700" y2={y} />
        ))}
        {lines.map(([participant, values], lineIndex) => {
          const path = values
            .map((point, index) => {
              const x = 70 + (index / Math.max(values.length - 1, 1)) * 590;
              const y = 130 - point.v * 92;
              return `${index ? "L" : "M"}${x},${y}`;
            })
            .join(" ");
          return (
            <g key={participant}>
              <path d={path} style={{ stroke: colors[lineIndex % colors.length] }} />
              {values.map((point, index) => {
                const x = 70 + (index / Math.max(values.length - 1, 1)) * 590;
                const y = 130 - point.v * 92;
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
  );
}

export function ReportView({
  result,
  onBack,
}: {
  result: AnalysisResult;
  onBack: () => void;
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [asking, setAsking] = useState(false);
  const ask = async () => {
    if (!question.trim() || asking) return;
    setAsking(true);
    setAnswer("");
    try {
      await gateway.chat(question, result, (value) =>
        setAnswer((current) => current + value),
      );
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
        <div className="confidence-orb">
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
            <small>V · Valence</small>
          </div>
          <VadChart result={result} />
          <p className="boundary">
            VAD 仅描述本次情境中的表达状态，不代表参与者真实内心或稳定性格。
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
        <article className="glass-card span-two evidence-card">
          <div className="card-title">
            <span>证据与不确定性</span>
            <small>{result.evidence.length} 条证据</small>
          </div>
          {result.evidence.map((evidence) => (
            <div className="evidence-row" key={evidence.id}>
              <time>{Math.floor(evidence.start_ms / 1000)}s</time>
              <div>
                <b>{evidence.segment_id}</b>
                <p>{evidence.excerpt}</p>
              </div>
            </div>
          ))}
          {result.uncertainties.map((item) => (
            <p className="uncertainty" key={item}>
              {item}
            </p>
          ))}
        </article>
        <article className="glass-card span-two ask-card">
          <MessageCircle size={22} />
          <div>
            <p className="eyebrow">CONTINUE THE ECHO</p>
            <h2>继续问 memEcho</h2>
            <p>只使用这次会谈与已选择的证据，不自动读取其他记忆。</p>
          </div>
          <div className="ask-box">
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="例如：为什么这里被识别为分歧？"
              onKeyDown={(event) => event.key === "Enter" && ask()}
            />
            <button onClick={ask} disabled={asking}>
              {asking ? "回声中…" : "发送"}
            </button>
          </div>
          {answer && <div className="echo-answer">{answer}</div>}
        </article>
      </div>
    </section>
  );
}

