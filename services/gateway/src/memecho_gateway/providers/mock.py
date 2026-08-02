from __future__ import annotations

from typing import Any

from ..text_only import build_text_segments


def _mock_text_only_result(
    session: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    observations = session.get("observations") or {}
    segments = observations.get("text_segments") or []
    if not segments:
        source = request.get("source") or {}
        segments = build_text_segments(source.get("text") or "mock text input")

    participants = request.get("participants") or [
        {"id": "speaker_self", "name": "Self", "is_self": True}
    ]
    self_participant = next(
        (participant for participant in participants if participant.get("is_self")),
        None,
    )
    self_participant_id = self_participant.get("id") if self_participant else None
    identity_basis = request.get("self_identity_basis") or "unknown"
    evidence = [
        {
            "id": segment["evidence_id"],
            "source_type": "transcript",
            "speaker_id": None,
            "start_ms": 0,
            "end_ms": 0,
            "segment_id": segment["segment_id"],
            "excerpt": segment["text"],
            "quality_flags": list(segment.get("quality_flags") or []),
        }
        for segment in segments
    ]
    vad_series = [
        {
            "participant_id": self_participant_id or participants[0]["id"],
            "segment_id": segment["segment_id"],
            "v": 0.0,
            "a": 0.0,
            "d": 0.0,
            "scale": "-1..1",
            "confidence": 0.5,
            "linguistic_weight": 1.0,
            "acoustic_weight": 0.0,
            "evidence_refs": [segment["evidence_id"]],
        }
        for segment in segments
    ]
    first_evidence_id = evidence[0]["id"]

    return {
        "schema_version": "1.1",
        "request_id": request["request_id"],
        "analysis_mode": "text_only",
        "scope": {
            "single_session": True,
            "signals_used": ["transcript", "linguistic"],
            "signals_missing": [
                "acoustic",
                "pitch",
                "energy",
                "speech_rate",
                "voice_quality",
            ],
            "quality": 0.75,
            "target_participant_ids": request.get("target_participant_ids")
            or [participant["id"] for participant in participants],
            "self_participant_id": self_participant_id,
            "self_identity_basis": identity_basis,
        },
        "minutes": {
            "summary": f"The submitted text contains {len(segments)} stable evidence segment(s).",
            "focus": [],
            "consensus": [],
            "disagreements": [],
            "explicit_actions": [],
            "recommendations": [],
        },
        "content_analysis": [
            {
                "participant_id": participant["id"],
                "fact_claims": [],
                "opinions": [],
                "attitudes": [],
                "influence_summary": [],
            }
            for participant in participants
        ],
        "participants": participants,
        "vad_series": vad_series,
        "interaction_events": [],
        "self_echo": {
            "participant_id": self_participant_id,
            "identity_basis": identity_basis,
            "effects": [],
            "alternatives": [],
        },
        "coaching": {"enabled": False, "status": "not_requested", "scenes": []},
        "insights": [
            {
                "id": "in_text_segments",
                "claim": f"The input was divided into {len(segments)} text segment(s).",
                "claim_level": "computed",
                "confidence": 1.0,
                "evidence_refs": [first_evidence_id],
                "alternatives": [
                    "Paragraph boundaries depend on the submitted line breaks."
                ],
            }
        ],
        "evidence": evidence,
        "uncertainties": [
            "Text-only mode cannot evaluate pitch, energy, pace, pauses, or voice quality."
        ],
        "provenance": {
            "skill_version": "1.0.2",
            "service_version": "0.1.0",
            "model_manifest": [
                {"provider": "mock", "model": "deterministic-text-demo"}
            ],
        },
        "memory": {"written": False, "consent_basis": None},
    }


class MockProvider:
    async def analyze(
        self, session: dict[str, Any], tracks: list[str], request: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = request["request_id"]
        if not tracks:
            return _mock_text_only_result(session, request)
        return {
            "schema_version": "1.1",
            "request_id": request_id,
            "analysis_mode": "connected_full" if tracks else "text_only",
            "scope": {
                "single_session": True,
                "signals_used": ["transcript", "linguistic", "acoustic"] if tracks else ["transcript", "linguistic"],
                "signals_missing": [],
                "quality": 0.82,
                "target_participant_ids": ["speaker_self", "speaker_2"],
                "self_participant_id": "speaker_self",
                "self_identity_basis": "user_confirmed",
            },
            "minutes": {
                "summary": "双方围绕范围与时间安排进行澄清，分歧在界定优先级后得到缓和。",
                "focus": ["范围边界", "交付时间", "验证方式"],
                "consensus": ["先固定核心路径，再验证新增需求"],
                "disagreements": ["新增能力是否应进入当前版本"],
                "explicit_actions": [
                    {
                        "text": "整理当前版本范围清单",
                        "owner": "speaker_self",
                        "due_at": None,
                        "origin": "discussed",
                        "status": "confirmed",
                        "evidence_refs": ["ev_03"],
                    }
                ],
                "recommendations": [
                    {
                        "text": "下次会谈开头先确认共同决策标准",
                        "owner": None,
                        "due_at": None,
                        "origin": "suggested",
                        "status": "proposed",
                        "evidence_refs": ["ev_02"],
                    }
                ],
            },
            "content_analysis": [
                {
                    "participant_id": "speaker_self",
                    "fact_claims": ["当前版本已经延迟"],
                    "opinions": ["继续扩大范围会增加交付风险"],
                    "attitudes": ["希望尽快明确边界"],
                    "influence_summary": ["界定边界后，对方开始讨论可执行的验证方式"],
                },
                {
                    "participant_id": "speaker_2",
                    "fact_claims": [],
                    "opinions": ["真正的问题是双方尚未统一优先级"],
                    "attitudes": ["对继续回避核心问题表现出保留"],
                    "influence_summary": ["直接指出分歧后，会谈短暂升高唤醒度"],
                },
            ],
            "participants": [
                {"id": "speaker_self", "name": "我", "is_self": True},
                {"id": "speaker_2", "name": "参与者 B", "is_self": False},
            ],
            "vad_series": [
                {
                    "participant_id": "speaker_self",
                    "segment_id": "seg_01",
                    "v": 0.12,
                    "a": 0.28,
                    "d": 0.22,
                    "scale": "-1..1",
                    "confidence": 0.76,
                    "linguistic_weight": 0.65,
                    "acoustic_weight": 0.35,
                    "evidence_refs": ["ev_01"],
                },
                {
                    "participant_id": "speaker_2",
                    "segment_id": "seg_02",
                    "v": -0.31,
                    "a": 0.61,
                    "d": 0.36,
                    "scale": "-1..1",
                    "confidence": 0.79,
                    "linguistic_weight": 0.65,
                    "acoustic_weight": 0.35,
                    "evidence_refs": ["ev_02"],
                },
                {
                    "participant_id": "speaker_self",
                    "segment_id": "seg_03",
                    "v": 0.26,
                    "a": 0.34,
                    "d": 0.48,
                    "scale": "-1..1",
                    "confidence": 0.81,
                    "linguistic_weight": 0.65,
                    "acoustic_weight": 0.35,
                    "evidence_refs": ["ev_03"],
                },
            ],
            "interaction_events": [],
            "self_echo": {
                "participant_id": "speaker_self",
                "identity_basis": "user_confirmed",
                "effects": [
                    {
                        "wording": "我们先把这一版必须完成的部分定下来。",
                        "observed_followup": "对方开始提出验证方式。",
                        "confidence": 0.78,
                        "evidence_refs": ["ev_03"],
                    }
                ],
                "alternatives": [
                    {
                        "source": "现在不能再加需求了。",
                        "rewrite": "我担心继续扩展会影响共同确认的时间，我们能先确认本版标准吗？",
                    }
                ],
            },
            "coaching": {"enabled": False, "status": "not_requested", "scenes": []},
            "insights": [
                {
                    "id": "in_01",
                    "claim": "直接指出未解决问题之后，对话唤醒度短暂升高。",
                    "claim_level": "interpreted",
                    "confidence": 0.77,
                    "evidence_refs": ["ev_02"],
                    "alternatives": ["变化也可能受到话题难度或录音环境影响"],
                }
            ],
            "evidence": [
                {
                    "id": "ev_01",
                    "source_type": "transcript",
                    "speaker_id": "speaker_self",
                    "start_ms": 0,
                    "end_ms": 8000,
                    "segment_id": "seg_01",
                    "excerpt": "我们先确认一下今天要解决的问题。",
                    "quality_flags": [],
                },
                {
                    "id": "ev_02",
                    "source_type": "transcript",
                    "speaker_id": "speaker_2",
                    "start_ms": 8000,
                    "end_ms": 17000,
                    "segment_id": "seg_02",
                    "excerpt": "我们好像一直在绕开真正的问题。",
                    "quality_flags": [],
                },
                {
                    "id": "ev_03",
                    "source_type": "acoustic",
                    "speaker_id": "speaker_self",
                    "start_ms": 17000,
                    "end_ms": 26000,
                    "segment_id": "seg_03",
                    "excerpt": "我们先把这一版必须完成的部分定下来。",
                    "quality_flags": ["mock_acoustic_evidence"],
                },
            ],
            "uncertainties": ["当前为脱敏演示适配器，真实音频链路需配置百炼与 OSS。"],
            "provenance": {
                "skill_version": "1.0.2",
                "service_version": "0.1.0",
                "model_manifest": [{"provider": "mock", "model": "deterministic-demo"}],
            },
            "memory": {"written": False, "consent_basis": None},
        }

    async def chat(self, question: str, context: dict[str, Any]) -> str:
        return (
            f"你问的是“{question}”。从本次会谈证据看，可以先区分可核验事实与偏好，"
            "再用一个可回应的问题确认双方是否共享同一决策标准。"
        )

