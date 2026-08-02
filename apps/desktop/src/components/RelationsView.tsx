import type { SessionSummary } from "@memecho/contracts";
import { ArrowUpRight, Layers3, MapPin, Network } from "lucide-react";
import { useMemo, type KeyboardEvent } from "react";

export interface MemoryCandidate {
  id: string;
  session_id: string;
  segment_id: string;
  label: string;
  summary: string;
  confirmed: boolean;
  confirmed_at?: string | null;
}

export interface MemorySourceRelation {
  id: string;
  memory_id: string;
  pattern_id: string;
  pattern_label: string;
}

export interface RelationsViewProps {
  sessions: readonly SessionSummary[];
  memoryCandidates: readonly MemoryCandidate[];
  sourceRelations: readonly MemorySourceRelation[];
  onOpenSource: (sessionId: string, segmentId: string) => void;
}

type PositionedMemory = MemoryCandidate & {
  x: number;
  y: number;
};

type SharedPattern = {
  id: string;
  label: string;
  memories: PositionedMemory[];
};

function occurredAt(
  memory: MemoryCandidate,
  sessionsById: ReadonlyMap<string, SessionSummary>,
) {
  const value =
    sessionsById.get(memory.session_id)?.occurred_at ?? memory.confirmed_at ?? "";
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function layoutMemories(memories: MemoryCandidate[]): PositionedMemory[] {
  if (memories.length === 1) {
    return [{ ...memories[0], x: 380, y: 225 }];
  }
  return memories.map((memory, index) => {
    const angle = -Math.PI / 2 + (index / memories.length) * Math.PI * 2;
    return {
      ...memory,
      x: 380 + Math.cos(angle) * 270,
      y: 225 + Math.sin(angle) * 155,
    };
  });
}

function patternPath(memories: PositionedMemory[]) {
  if (memories.length === 2) {
    const [first, second] = memories;
    const controlX = (first.x + second.x) / 2 - (second.y - first.y) * 0.14;
    const controlY = (first.y + second.y) / 2 + (second.x - first.x) * 0.14;
    return `M ${first.x} ${first.y} Q ${controlX} ${controlY} ${second.x} ${second.y}`;
  }
  const center = memories.reduce(
    (value, memory) => ({ x: value.x + memory.x, y: value.y + memory.y }),
    { x: 0, y: 0 },
  );
  center.x /= memories.length;
  center.y /= memories.length;
  const ordered = [...memories].sort(
    (left, right) =>
      Math.atan2(left.y - center.y, left.x - center.x) -
      Math.atan2(right.y - center.y, right.x - center.x),
  );
  return `${ordered
    .map((memory, index) => `${index === 0 ? "M" : "L"} ${memory.x} ${memory.y}`)
    .join(" ")} Z`;
}

function patternCenter(memories: PositionedMemory[]) {
  return memories.reduce(
    (value, memory) => ({
      x: value.x + memory.x / memories.length,
      y: value.y + memory.y / memories.length,
    }),
    { x: 0, y: 0 },
  );
}

function activateOnKeyboard(
  event: KeyboardEvent<SVGGElement>,
  action: () => void,
) {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
}

function displayDate(value: string | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

export function RelationsView({
  sessions,
  memoryCandidates,
  sourceRelations,
  onOpenSource,
}: RelationsViewProps) {
  const model = useMemo(() => {
    const sessionsById = new Map(sessions.map((session) => [session.id, session]));
    const confirmed = memoryCandidates
      .filter((memory) => memory.confirmed)
      .sort(
        (left, right) =>
          occurredAt(left, sessionsById) - occurredAt(right, sessionsById) ||
          left.id.localeCompare(right.id),
      );
    const positioned = layoutMemories(confirmed);
    const memoriesById = new Map(positioned.map((memory) => [memory.id, memory]));
    const grouped = new Map<
      string,
      { label: string; memories: Map<string, PositionedMemory> }
    >();

    for (const relation of sourceRelations) {
      const memory = memoriesById.get(relation.memory_id);
      if (!memory) continue;
      const pattern = grouped.get(relation.pattern_id) ?? {
        label: relation.pattern_label,
        memories: new Map<string, PositionedMemory>(),
      };
      pattern.memories.set(memory.id, memory);
      grouped.set(relation.pattern_id, pattern);
    }

    const patterns: SharedPattern[] = [...grouped.entries()]
      .map(([id, pattern]) => ({
        id,
        label: pattern.label,
        memories: [...pattern.memories.values()],
      }))
      .filter((pattern) => pattern.memories.length >= 2)
      .sort((left, right) => left.id.localeCompare(right.id));

    return { sessionsById, positioned, patterns };
  }, [memoryCandidates, sessions, sourceRelations]);

  if (model.positioned.length === 0) {
    return (
      <section className="relations-view relations-empty" aria-labelledby="relations-title">
        <p className="eyebrow">MEMORY ECHO</p>
        <h1 id="relations-title">关系会从你确认的记忆开始。</h1>
        <p>
          当前没有已确认的记忆。候选观察不会自动进入关系视图，也不会在这里写入长期记忆。
        </p>
      </section>
    );
  }

  const openMemory = (memory: MemoryCandidate) =>
    onOpenSource(memory.session_id, memory.segment_id);
  const latestForPattern = (pattern: SharedPattern) =>
    [...pattern.memories].sort(
      (left, right) =>
        occurredAt(right, model.sessionsById) -
          occurredAt(left, model.sessionsById) ||
        right.id.localeCompare(left.id),
    )[0];

  return (
    <section className="relations-view" aria-labelledby="relations-title">
      <header className="relations-heading">
        <div>
          <p className="eyebrow">MEMORY ECHO</p>
          <h1 id="relations-title">已经发生的点，仍在关系里形成回声。</h1>
          <p>
            这里只呈现你明确确认过的事件。连线与流动面表示多个事件共享的模式，不代表因果或固定关系。
          </p>
        </div>
        <div className="relations-count" aria-label={`${model.positioned.length} 条已确认记忆`}>
          <strong>{model.positioned.length}</strong>
          <span>已确认记忆</span>
        </div>
      </header>

      <div className="relations-layout">
        <div className="memory-map-card">
          <svg
            className="memory-map"
            viewBox="0 0 760 450"
            role="group"
            aria-label="关系记忆图：事件是点，共享模式形成连线或流动面"
          >
            <defs>
              <filter id="memory-soft-glow" x="-40%" y="-40%" width="180%" height="180%">
                <feGaussianBlur stdDeviation="7" />
              </filter>
            </defs>

            {model.patterns.map((pattern, index) => {
              const source = latestForPattern(pattern);
              const center = patternCenter(pattern.memories);
              const open = () => openMemory(source);
              return (
                <g
                  className={`memory-pattern pattern-tone-${index % 4}`}
                  key={pattern.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`图中打开共享模式“${pattern.label}”的最近来源`}
                  onClick={open}
                  onKeyDown={(event) => activateOnKeyboard(event, open)}
                >
                  <title>{`${pattern.label}，连接 ${pattern.memories.length} 个已确认事件`}</title>
                  <path
                    className="memory-pattern-surface"
                    d={patternPath(pattern.memories)}
                    data-testid={`memory-pattern-flow-${pattern.id}`}
                  />
                  <path className="memory-pattern-flow" d={patternPath(pattern.memories)} />
                  <text x={center.x} y={center.y - 12}>
                    {pattern.label}
                  </text>
                </g>
              );
            })}

            {model.positioned.map((memory, index) => {
              const open = () => openMemory(memory);
              return (
                <g
                  className={`memory-event-node event-tone-${index % 4}`}
                  key={memory.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`图中打开事件“${memory.label}”的来源`}
                  onClick={open}
                  onKeyDown={(event) => activateOnKeyboard(event, open)}
                  transform={`translate(${memory.x} ${memory.y})`}
                >
                  <title>{memory.summary}</title>
                  <circle className="memory-event-glow" r="28" filter="url(#memory-soft-glow)" />
                  <circle className="memory-event-point" r="12" />
                  <circle className="memory-event-ring" r="20" />
                  <text y="39">{memory.label}</text>
                </g>
              );
            })}
          </svg>
          <div className="memory-map-legend" aria-hidden="true">
            <span>
              <i className="event-key" /> 已确认事件
            </span>
            <span>
              <i className="pattern-key" /> 跨会话共享模式
            </span>
          </div>
        </div>

        <div className="memory-source-panel">
          <section aria-labelledby="confirmed-events-title">
            <div className="memory-panel-title">
              <MapPin size={16} />
              <h2 id="confirmed-events-title">事件来源</h2>
            </div>
            <ul className="memory-event-list">
              {model.positioned.map((memory) => {
                const session = model.sessionsById.get(memory.session_id);
                return (
                  <li key={memory.id}>
                    <button type="button" onClick={() => openMemory(memory)}>
                      <span>
                        <b>{memory.label}</b>
                        <small>
                          {session?.title ?? memory.session_id}
                          {displayDate(session?.occurred_at)
                            ? ` · ${displayDate(session?.occurred_at)}`
                            : ""}
                        </small>
                        <p>{memory.summary}</p>
                      </span>
                      <ArrowUpRight size={16} aria-hidden="true" />
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>

          <section aria-labelledby="shared-patterns-title">
            <div className="memory-panel-title">
              <Layers3 size={16} />
              <h2 id="shared-patterns-title">共享模式</h2>
            </div>
            {model.patterns.length > 0 ? (
              <ul className="memory-pattern-list">
                {model.patterns.map((pattern) => {
                  const source = latestForPattern(pattern);
                  return (
                    <li key={pattern.id}>
                      <button type="button" onClick={() => openMemory(source)}>
                        <Network size={15} aria-hidden="true" />
                        <span>
                          <b>{pattern.label}</b>
                          <small>
                            {pattern.memories.length} 个已确认事件 · 打开最近来源
                          </small>
                        </span>
                        <ArrowUpRight size={15} aria-hidden="true" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="memory-pattern-empty">
                还没有至少由两条已确认事件共同支持的模式。
              </p>
            )}
          </section>
        </div>
      </div>

      <p className="relations-boundary">
        关系视图只读取本地已确认记忆；它不会自动确认候选项，也不会把共享模式解释为隐藏意图、人格标签或因果关系。
      </p>
    </section>
  );
}