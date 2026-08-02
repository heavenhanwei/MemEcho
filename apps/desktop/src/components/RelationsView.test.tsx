// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import type { SessionSummary } from "@memecho/contracts";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  RelationsView,
  type MemoryCandidate,
  type MemorySourceRelation,
} from "./RelationsView";

const sessions: SessionSummary[] = [
  {
    id: "session_1",
    title: "范围讨论",
    context: "工作",
    occurred_at: "2026-07-10T10:00:00+08:00",
    duration_ms: 1_200_000,
    status: "complete",
    participant_count: 2,
    has_result: true,
  },
  {
    id: "session_2",
    title: "交付复盘",
    context: "工作",
    occurred_at: "2026-07-18T14:00:00+08:00",
    duration_ms: 900_000,
    status: "complete",
    participant_count: 3,
    has_result: true,
  },
];

const confirmedMemories: MemoryCandidate[] = [
  {
    id: "memory_1",
    session_id: "session_1",
    segment_id: "segment_12",
    label: "先确认版本边界",
    summary: "在讨论新增需求前先确认当前版本范围。",
    confirmed: true,
  },
  {
    id: "memory_2",
    session_id: "session_2",
    segment_id: "segment_08",
    label: "先统一决策标准",
    summary: "分歧出现后先明确共同采用的判断标准。",
    confirmed: true,
  },
];

const sharedRelations: MemorySourceRelation[] = [
  {
    id: "relation_1",
    memory_id: "memory_1",
    pattern_id: "pattern_scope",
    pattern_label: "先收束，再决策",
  },
  {
    id: "relation_2",
    memory_id: "memory_2",
    pattern_id: "pattern_scope",
    pattern_label: "先收束，再决策",
  },
];

afterEach(cleanup);

describe("RelationsView confirmed memory boundary", () => {
  it("renders only confirmed candidates and never promotes an unconfirmed relation", () => {
    const onOpenSource = vi.fn();
    const unconfirmed: MemoryCandidate = {
      id: "memory_draft",
      session_id: "session_2",
      segment_id: "segment_draft",
      label: "尚未确认的推测",
      summary: "这条候选不应出现在关系视图。",
      confirmed: false,
    };
    render(
      <RelationsView
        sessions={sessions}
        memoryCandidates={[confirmedMemories[0], unconfirmed]}
        sourceRelations={[
          {
            id: "relation_draft",
            memory_id: unconfirmed.id,
            pattern_id: "pattern_draft",
            pattern_label: "不应显示的模式",
          },
        ]}
        onOpenSource={onOpenSource}
      />,
    );

    expect(screen.getByLabelText("1 条已确认记忆")).toBeInTheDocument();
    expect(screen.getAllByText("先确认版本边界").length).toBeGreaterThan(0);
    expect(screen.queryByText("尚未确认的推测")).not.toBeInTheDocument();
    expect(screen.queryByText("不应显示的模式")).not.toBeInTheDocument();
    expect(
      screen.getByText("还没有至少由两条已确认事件共同支持的模式。"),
    ).toBeInTheDocument();
  });

  it("shows an honest empty state when there are no confirmed memories", () => {
    render(
      <RelationsView
        sessions={sessions}
        memoryCandidates={[
          {
            ...confirmedMemories[0],
            confirmed: false,
          },
        ]}
        sourceRelations={sharedRelations}
        onOpenSource={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "关系会从你确认的记忆开始。" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/候选观察不会自动进入关系视图/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /关系记忆图/ })).not.toBeInTheDocument();
  });
});

describe("RelationsView source navigation and accessibility", () => {
  it("forms a shared pattern from confirmed events and opens exact sources", () => {
    const onOpenSource = vi.fn();
    render(
      <RelationsView
        sessions={sessions}
        memoryCandidates={confirmedMemories}
        sourceRelations={sharedRelations}
        onOpenSource={onOpenSource}
      />,
    );

    expect(
      screen.getByRole("group", {
        name: "关系记忆图：事件是点，共享模式形成连线或流动面",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("memory-pattern-flow-pattern_scope")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: "图中打开事件“先确认版本边界”的来源",
      }),
    );
    expect(onOpenSource).toHaveBeenLastCalledWith("session_1", "segment_12");

    fireEvent.click(
      screen.getByRole("button", {
        name: "图中打开共享模式“先收束，再决策”的最近来源",
      }),
    );
    expect(onOpenSource).toHaveBeenLastCalledWith("session_2", "segment_08");

    const eventList = screen.getByRole("region", { name: "事件来源" });
    fireEvent.click(within(eventList).getByRole("button", { name: /先统一决策标准/ }));
    expect(onOpenSource).toHaveBeenLastCalledWith("session_2", "segment_08");
  });

  it("supports keyboard activation and explains the interpretation boundary", () => {
    const onOpenSource = vi.fn();
    render(
      <RelationsView
        sessions={sessions}
        memoryCandidates={confirmedMemories}
        sourceRelations={sharedRelations}
        onOpenSource={onOpenSource}
      />,
    );

    const eventNode = screen.getByRole("button", {
      name: "图中打开事件“先统一决策标准”的来源",
    });
    fireEvent.keyDown(eventNode, { key: "Enter" });
    expect(onOpenSource).toHaveBeenLastCalledWith("session_2", "segment_08");

    const patternNode = screen.getByRole("button", {
      name: "图中打开共享模式“先收束，再决策”的最近来源",
    });
    fireEvent.keyDown(patternNode, { key: " " });
    expect(onOpenSource).toHaveBeenLastCalledWith("session_2", "segment_08");
    expect(screen.getByText(/不代表因果或固定关系/)).toBeInTheDocument();
    expect(screen.getByText(/不会自动确认候选项/)).toBeInTheDocument();
  });
});