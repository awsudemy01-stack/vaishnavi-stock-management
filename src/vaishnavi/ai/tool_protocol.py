"""Tool protocol — definitions, strict argument validation and the prompted-JSON fallback (docs/06 §3, §7 layer 5).

A :class:`ToolDef` is data: name, description, a small JSON-schema for the arguments, the roles that may
call it, and the handler. :func:`validate_args` is a deliberately tiny schema checker (types, required,
enum, integer bounds, ``limit ≤ 50``) — no pydantic, so the whole AI tier stays stdlib. For models
without native tool calling, :func:`parse_tool_block` accepts exactly one fenced ``tool`` JSON block and
nothing else.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.ai.providers.base import ToolCall, ToolSpec
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, Role

log = logging.getLogger("vaishnavi.ai.tools")

MAX_LIMIT: Final = 50
MAX_RESULT_CHARS: Final = 8000
"""≈ 2k tokens — tool results are trimmed beyond this."""


class ToolError(VaishnaviError):
    """E-GRD — a tool call the policy does not allow (role, unknown tool, bad arguments)."""

    area = "GRD"
    default_code = "E-GRD-001"


ToolHandler = Callable[[Actor, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDef:
    """One tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    roles: frozenset[Role]
    handler: ToolHandler
    link: str | None = None
    """Report screen the answer should offer to open."""
    staff_strip: tuple[str, ...] = ("cost", "cost_paise", "margin", "margin_pct", "profit", "profit_paise")
    """Keys removed from the result for STAFF (defence in depth — staff tools should never produce them)."""

    def spec(self) -> ToolSpec:
        """What the model sees."""
        return ToolSpec(self.name, self.description, self.parameters)


def validate_args(tool: ToolDef, args: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce + check ``args`` against ``tool.parameters``; raises ``ToolError`` (E-GRD-005) on anything off."""
    props: dict[str, Any] = tool.parameters.get("properties", {})
    required = list(tool.parameters.get("required", []))
    out: dict[str, Any] = {}
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolError("E-GRD-005", tool=tool.name, detail=f"unknown argument(s): {', '.join(sorted(unknown))}")
    for name in required:
        if name not in args or args[name] in (None, ""):
            raise ToolError("E-GRD-005", tool=tool.name, detail=f"missing argument: {name}")
    for name, value in args.items():
        schema = props[name]
        kind = schema.get("type", "string")
        try:
            if kind == "integer":
                v: Any = int(value)
                lo, hi = schema.get("minimum"), schema.get("maximum")
                if lo is not None:
                    v = max(int(lo), v)
                if hi is not None:
                    v = min(int(hi), v)
                if name == "limit":
                    v = max(1, min(MAX_LIMIT, v))
            elif kind == "number":
                v = float(value)
            elif kind == "boolean":
                v = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes")
            else:
                v = str(value).strip()
                if len(v) > 200:
                    v = v[:200]
                if "enum" in schema and v not in schema["enum"]:
                    raise ToolError("E-GRD-005", tool=tool.name, detail=f"{name} must be one of {schema['enum']}")
                if schema.get("format") == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise ToolError("E-GRD-005", tool=tool.name, detail=f"{name} must be YYYY-MM-DD")
        except (TypeError, ValueError) as exc:
            raise ToolError("E-GRD-005", tool=tool.name, detail=f"{name}: {exc}") from exc
        out[name] = v
    for name, schema in props.items():
        if name not in out and "default" in schema:
            out[name] = schema["default"]
    return out


def trim_result(result: dict[str, Any]) -> dict[str, Any]:
    """Cap list fields so one result cannot flood the context."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    trimmed = dict(result)
    for key, value in list(trimmed.items()):
        if isinstance(value, list) and len(value) > 10:
            trimmed[key] = value[:10]
            trimmed["truncated"] = True
    log.debug("AI_TOOL_RESULT_TRIMMED from=%d", len(text))
    return trimmed


def strip_keys(value: Any, keys: tuple[str, ...]) -> Any:
    """Remove the named keys recursively (STAFF field allow-list)."""
    if isinstance(value, dict):
        return {k: strip_keys(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [strip_keys(v, keys) for v in value]
    return value


_BLOCK = re.compile(r"```(?:tool|json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_tool_block(text: str) -> ToolCall | None:
    """Prompted-JSON fallback: one ``{"name": ..., "arguments": {...}}`` block, else None."""
    m = _BLOCK.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        log.debug("AI_TOOL_BLOCK_INVALID_JSON")
        return None
    name = data.get("name") if isinstance(data, dict) else None
    args = data.get("arguments") if isinstance(data, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{3,40}", name) or not isinstance(args, dict):
        log.debug("AI_TOOL_BLOCK_REJECTED name=%r", name)
        return None
    return ToolCall("call_1", name, dict(args))


@dataclass
class ToolCallLog:
    """What the chat layer records per call."""

    name: str
    args: dict[str, Any]
    rows: int
    ms: int
    ok: bool
    error: str | None = None
    link: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
