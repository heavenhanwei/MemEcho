from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

GATEWAY_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = GATEWAY_DIR.parents[1]
OUTPUT = WORKSPACE_ROOT / "packages" / "contracts" / "src" / "generated.ts"
sys.path.insert(0, str(GATEWAY_DIR / "src"))

from memecho_gateway.main import app  # noqa: E402


def referenced_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            names.add(ref.rsplit("/", 1)[-1])
        for child in value.values():
            names.update(referenced_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(referenced_names(child))
    return names


def reachable_schemas(openapi: dict[str, Any]) -> dict[str, dict[str, Any]]:
    schemas = openapi["components"]["schemas"]
    analyze = openapi["paths"]["/v1/sessions/{session_id}/analyze"]["post"]
    result = openapi["paths"]["/v1/sessions/{session_id}/result"]["get"]
    details = openapi["paths"]["/v1/sessions/{session_id}/processing-details"]["get"]
    roots = (
        referenced_names(analyze["requestBody"])
        | referenced_names(result["responses"]["200"])
        | referenced_names(details["responses"]["200"])
    )
    pending = list(roots)
    reachable: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in schemas:
            raise RuntimeError(f"OpenAPI reference is missing schema {name}")
        reachable.add(name)
        pending.extend(referenced_names(schemas[name]) - reachable)
    return {name: schemas[name] for name in sorted(reachable)}


def literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def parenthesize_array_item(value: str) -> str:
    return f"({value})" if " | " in value or " & " in value else value


def schema_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return literal(schema["const"])
    if "enum" in schema:
        return " | ".join(literal(item) for item in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(schema_type(item) for item in schema["anyOf"])
    if "oneOf" in schema:
        return " | ".join(schema_type(item) for item in schema["oneOf"])
    if "allOf" in schema:
        return " & ".join(schema_type(item) for item in schema["allOf"])

    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(schema_type({"type": item}) for item in kind)
    if kind == "array":
        item = schema_type(schema.get("items", {}))
        return f"{parenthesize_array_item(item)}[]"
    if kind == "object" or "properties" in schema or "additionalProperties" in schema:
        properties = schema.get("properties", {})
        if properties:
            required = set(schema.get("required", []))
            members = [
                f"{property_name(name)}{'' if name in required else '?'}: {schema_type(value)};"
                for name, value in properties.items()
            ]
            return "{ " + " ".join(members) + " }"
        additional = schema.get("additionalProperties", True)
        if isinstance(additional, dict):
            return f"Record<string, {schema_type(additional)}>"
        return "Record<string, unknown>"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, "unknown")


def property_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) else literal(name)


def render_named_schema(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties")
    if schema.get("type") == "object" and properties:
        required = set(schema.get("required", []))
        lines = [f"export interface {name} {{"]
        for field, field_schema in properties.items():
            optional = "" if field in required else "?"
            lines.append(f"  {property_name(field)}{optional}: {schema_type(field_schema)};")
        lines.append("}")
        return "\n".join(lines)
    return f"export type {name} = {schema_type(schema)};"


def generated_content() -> str:
    openapi = app.openapi()
    schemas = reachable_schemas(openapi)
    blocks = [
        "// This file is generated from the memEcho Gateway OpenAPI document.\n"
        "// Do not edit by hand. Run: python services/gateway/scripts/generate_types.py"
    ]
    blocks.extend(render_named_schema(name, schema) for name, schema in schemas.items())
    blocks.extend(
        [
            'export type SourceType = AnalysisSource["type"];',
            'export type FocusModule = NonNullable<AnalysisRequest["focus"]>[number];',
            'export type AnalysisMode = AnalysisResult["analysis_mode"];',
        ]
    )
    return "\n\n".join(blocks).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate memEcho analysis TypeScript contracts")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when generated.ts differs from the current FastAPI OpenAPI contract",
    )
    args = parser.parse_args()
    expected = generated_content()
    if args.check:
        actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if actual != expected:
            print(
                "Generated contracts are stale. Run "
                "'python services/gateway/scripts/generate_types.py'.",
                file=sys.stderr,
            )
            return 1
        print(f"Contract drift check passed: {OUTPUT.relative_to(WORKSPACE_ROOT)}")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Generated {OUTPUT.relative_to(WORKSPACE_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
