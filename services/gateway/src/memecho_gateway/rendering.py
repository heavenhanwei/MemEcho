from __future__ import annotations

import html
from typing import Any


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# memEcho Analysis Report")
    lines.append("")
    lines.append(f"**Schema Version:** {result.get('schema_version', '1.1')}")
    lines.append(f"**Request ID:** {result.get('request_id', 'N/A')}")
    lines.append(f"**Analysis Mode:** {result.get('analysis_mode', 'N/A')}")
    lines.append("")

    scope = result.get("scope", {})
    if scope:
        lines.append("## Scope")
        lines.append("")
        lines.append(f"- **Quality:** {scope.get('quality', 'N/A')}")
        lines.append(f"- **Signals Used:** {', '.join(scope.get('signals_used', []))}")
        lines.append(f"- **Target Participants:** {', '.join(scope.get('target_participant_ids', []))}")
        lines.append("")

    minutes = result.get("minutes", {})
    if minutes:
        lines.append("## Minutes")
        lines.append("")
        lines.append(f"**Summary:** {minutes.get('summary', '')}")
        lines.append("")
        if minutes.get("focus"):
            lines.append(f"**Focus:** {', '.join(minutes['focus'])}")
        if minutes.get("consensus"):
            lines.append(f"**Consensus:** {', '.join(minutes['consensus'])}")
        if minutes.get("disagreements"):
            lines.append(f"**Disagreements:** {', '.join(minutes['disagreements'])}")
        lines.append("")

        actions = minutes.get("explicit_actions", [])
        if actions:
            lines.append("### Explicit Actions")
            lines.append("")
            for a in actions:
                lines.append(f"- [{a.get('status', '')}] {a.get('text', '')} (owner: {a.get('owner', 'N/A')})")
            lines.append("")

        recs = minutes.get("recommendations", [])
        if recs:
            lines.append("### Recommendations")
            lines.append("")
            for r in recs:
                lines.append(f"- [{r.get('status', '')}] {r.get('text', '')}")
            lines.append("")

    insights = result.get("insights", [])
    if insights:
        lines.append("## Insights")
        lines.append("")
        for ins in insights:
            lines.append(f"- **{ins.get('id', '')}** [{ins.get('claim_level', '')}] (conf: {ins.get('confidence', 0)}): {ins.get('claim', '')}")
            if ins.get("alternatives"):
                lines.append(f"  - Alternatives: {'; '.join(ins['alternatives'])}")
        lines.append("")

    evidence = result.get("evidence", [])
    if evidence:
        lines.append("## Evidence")
        lines.append("")
        for ev in evidence:
            lines.append(f"- **{ev.get('id', '')}** [{ev.get('source_type', '')}] speaker={ev.get('speaker_id', '')} {ev.get('start_ms', 0)}-{ev.get('end_ms', 0)}ms")
            if ev.get("excerpt"):
                lines.append(f"  > {ev['excerpt']}")
        lines.append("")

    provenance = result.get("provenance", {})
    if provenance:
        lines.append("## Provenance")
        lines.append("")
        lines.append(f"- **Skill Version:** {provenance.get('skill_version', '')}")
        lines.append(f"- **Service Version:** {provenance.get('service_version', '')}")
        models = provenance.get("model_manifest", [])
        for m in models:
            lines.append(f"- **Model:** {m.get('provider', '')}/{m.get('model', '')}")
        lines.append("")

    uncertainties = result.get("uncertainties", [])
    if uncertainties:
        lines.append("## Uncertainties")
        lines.append("")
        for u in uncertainties:
            lines.append(f"- {u}")
        lines.append("")

    return "\n".join(lines)


def render_html(result: dict[str, Any]) -> str:
    md = render_markdown(result)
    escaped = html.escape(md)
    return _HTML_TEMPLATE.replace("{{CONTENT}}", escaped)


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memEcho Analysis Report</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.6; }
  h1 { border-bottom: 2px solid #333; padding-bottom: 0.5rem; }
  h2 { color: #2c5282; margin-top: 2rem; }
  h3 { color: #2d3748; }
  blockquote { border-left: 3px solid #cbd5e0; margin: 0.5rem 0; padding: 0.25rem 1rem; color: #4a5568; }
  code { background: #edf2f7; padding: 0.1rem 0.3rem; border-radius: 3px; }
  ul { padding-left: 1.5rem; }
  li { margin: 0.25rem 0; }
</style>
</head>
<body>
<pre>{{CONTENT}}</pre>
</body>
</html>
"""
