"""``/system/logs`` — the Owner's log console (docs/08 §8–9), view-model first.

Reads the text logs through ``api.system.logs_tail`` (sandboxed to the logs folder, redaction already
applied at write time — never undone here), parses the ``KeyValueFormatter`` layout into columns and
adds the **plain-language** explanation of any ``E-XXX-NNN`` code in the owner's language.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Final

from vaishnavi.api import system as api_system
from vaishnavi.api.system import AppState
from vaishnavi.core.errors import CODE_RE, describe
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.logs")

LEVELS: Final = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
_CODE_IN_TEXT: Final = re.compile(r"\bE-[A-Z]+-\d{3}\b")
_CTX: Final = re.compile(r"cid=(?P<cid>\S+)\s+user=(?P<user>[^()\s]*)\((?P<role>[^)]*)\)\s+shift=(?P<shift>\S+)")


@dataclass(frozen=True)
class LogLine:
    """One parsed line."""

    raw: str
    time: str
    level: str
    logger: str
    cid: str
    user: str
    role: str
    message: str
    code: str
    plain: str


@dataclass(frozen=True)
class LogsModel:
    """What the console renders."""

    families: list[str]
    family: str
    level: str
    cid: str
    q: str
    file: str | None
    lines: list[LogLine]
    level_options: tuple[str, ...] = LEVELS
    labels: dict[str, str] = field(default_factory=dict)


def parse_line(raw: str, texts: UiTexts) -> LogLine:
    """``ts | LEVEL | logger | cid=… user=…(ROLE) shift=… | message`` → columns (lenient for odd lines)."""
    parts = [p.strip() for p in raw.split("|", 4)]
    if len(parts) < 5:
        return LogLine(raw=raw, time="", level="", logger="", cid="", user="", role="", message=raw.strip(), code="", plain="")
    time, level, logger_name, ctx, message = parts
    level = {"WARNING": "WARN"}.get(level.upper(), level.upper())
    m = _CTX.search(ctx)
    cid, user, role = (m.group("cid"), m.group("user"), m.group("role")) if m else ("", "", "")
    code_match = _CODE_IN_TEXT.search(message)
    code = code_match.group(0) if code_match and CODE_RE.match(code_match.group(0)) else ""
    plain = ""
    if code:
        info = describe(code, texts.lang)
        plain = f"{info['message']} — {info['what_to_do']}".strip(" —")
    return LogLine(raw=raw, time=time, level=level, logger=logger_name, cid=cid, user=user, role=role, message=message, code=code, plain=plain)


def build_logs_model(state: AppState, texts: UiTexts, *, family: str = "app", level: str = "INFO", cid: str = "", q: str = "", limit: int = 300) -> LogsModel:
    """The pure view-model; filters are AND-ed."""
    families = api_system.log_families(state)
    tail = api_system.logs_tail(state, family=family, level=level, limit=max(limit * 5, limit), contains=q)
    lines: list[LogLine] = []
    for raw in tail["lines"]:
        parsed = parse_line(raw, texts)
        if cid and parsed.cid != cid:
            continue
        lines.append(parsed)
    lines = lines[-limit:]
    log.debug("LOGS_VIEW family=%s level=%s cid=%s q=%r lines=%d", family, level, cid or "-", q, len(lines))
    labels = {k: texts.t(f"logs.{k}") for k in ("title", "family", "level", "cid", "search", "download", "plain", "empty", "pause", "resume")}
    return LogsModel(families=families, family=family, level=level, cid=cid, q=q, file=tail.get("file"), lines=lines, labels=labels)


def download_text(model: LogsModel) -> str:
    """The current view as a ``.log`` file body."""
    return "".join(line.raw.rstrip("\n") + "\n" for line in model.lines)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: AppState) -> None:
    """``/system/logs`` (Owner only through the guard)."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    @ui.page("/system/logs")
    def _logs(family: str = "app", level: str = "INFO", cid: str = "", q: str = "") -> None:
        client = load_client(app)
        decision = guards.decide("/system/logs", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/system/logs"))
            return
        texts = UiTexts.load(client.lang)
        filters = {"family": family, "level": level, "cid": cid, "q": q, "paused": False}
        with frame(state, current_path="/system/logs"):
            model = build_logs_model(state, texts, family=family, level=level, cid=cid, q=q)
            ui.label(model.labels["title"]).classes("text-xl font-semibold")
            with ui.row().classes("items-end gap-2"):
                ui.select(model.families or [family], value=family, label=model.labels["family"]).bind_value(filters, "family")
                ui.select(list(LEVELS), value=level, label=model.labels["level"]).bind_value(filters, "level")
                ui.input(model.labels["cid"]).bind_value(filters, "cid")
                ui.input(model.labels["search"]).bind_value(filters, "q")
                ui.button(texts.t("common.confirm"), on_click=lambda: ui.navigate.to(f"/system/logs?family={filters['family']}&level={filters['level']}&cid={filters['cid']}&q={filters['q']}")).props("unelevated color=primary")
                ui.button(model.labels["download"], icon="download", on_click=lambda: ui.download(download_text(current["model"]).encode("utf-8"), f"{filters['family']}.log")).props("flat")
                ui.switch(model.labels["pause"]).bind_value(filters, "paused")
            table = ui.column().classes("w-full gap-0 vsm-mono text-xs")
            current = {"model": model}

            def render(m: LogsModel) -> None:
                table.clear()
                with table:
                    if not m.lines:
                        ui.label(m.labels["empty"]).classes("text-gray-500")
                    for line in m.lines:
                        color = {"ERROR": "text-red-700", "CRITICAL": "text-red-700", "WARN": "text-amber-700", "DEBUG": "text-gray-400"}.get(line.level, "")
                        with ui.row().classes(f"w-full no-wrap gap-2 {color}"):
                            ui.label(line.time).classes("w-56 shrink-0")
                            ui.label(line.level).classes("w-12 shrink-0")
                            ui.label(line.logger).classes("w-64 shrink-0 truncate")
                            ui.label(line.cid).classes("w-32 shrink-0 truncate")
                            ui.label(line.message).classes("flex-1")
                        if line.plain:
                            ui.label(f"↳ {line.plain}").classes("pl-72 text-gray-600 font-sans")

            render(model)

            def refresh() -> None:
                if filters["paused"]:
                    return
                current["model"] = build_logs_model(state, texts, family=filters["family"], level=filters["level"], cid=filters["cid"], q=filters["q"])
                render(current["model"])

            ui.timer(5.0, refresh)


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
