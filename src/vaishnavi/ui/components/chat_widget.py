"""Chat widget — the bottom-right Sahayak bubble that opens a compact chat window (docs/06, docs/09 §1).

Pure view-model (``build_widget_model``, ``send``, ``history``) + a thin NiceGUI renderer. The language
selector (English / हिंदी / Hinglish) defaults to ``ai.default_language``; an *Open report* chip appears when
the answer came from a report-backed tool; an offline line shows when the provider is down while the fast
path keeps answering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.ai.chat_service import ChatReply
from vaishnavi.core.permissions import Role
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.state import ClientState

log = logging.getLogger("vaishnavi.ui.components.chat_widget")

LANGUAGES: Final = (("english", "English"), ("hindi", "हिंदी"), ("hinglish", "Hinglish"))


@dataclass(frozen=True)
class HistoryRow:
    """One bubble."""

    role: str
    text: str
    when: str


@dataclass(frozen=True)
class WidgetModel:
    """Window state."""

    title: str
    hint: str
    languages: tuple[tuple[str, str], ...]
    language: str
    online: bool
    offline_note: str
    role: Role
    suggestions: list[str]
    labels: dict[str, str] = field(default_factory=dict)


def build_widget_model(state: Any, client: ClientState, texts: UiTexts) -> WidgetModel:
    """Header, hint, language default and online state for this user."""
    c = state.extra["services"]
    ai = c.settings_service.effective().ai
    role = client.role
    health = c.ai_provider.health()
    online = bool(health.ok)
    hint = texts.t("chat.hint_owner" if role is Role.OWNER else "chat.hint_staff")
    suggestions = [texts.t("chat.sugg_owner_1"), texts.t("chat.sugg_owner_2"), texts.t("chat.sugg_owner_3")] if role is Role.OWNER else [texts.t("chat.sugg_staff_1"), texts.t("chat.sugg_staff_2"), texts.t("chat.sugg_staff_3")]
    labels = {k: texts.t(f"chat.{k}") for k in ("send", "placeholder", "open_report", "offline", "language", "new_chat", "restricted", "thinking", "failed")}
    log.debug("CHAT_WIDGET_MODEL role=%s online=%s provider=%s", role.value, online, health.provider)
    return WidgetModel(texts.t("chat.title"), hint, LANGUAGES, ai.default_language, online, "" if online else labels["offline"], role, suggestions, labels)


def send(state: Any, client: ClientState, text: str, *, language: str, session_id: int | None) -> ChatReply:
    """Ask the assistant as the signed-in user."""
    c = state.extra["services"]
    return c.chat.ask(client.actor(), session_id, text, language=language)


def history(state: Any, client: ClientState, session_id: int) -> list[HistoryRow]:
    """Bubbles of one session."""
    c = state.extra["services"]
    return [HistoryRow(m.role, m.content, m.created_at[11:16]) for m in c.chat.history(client.actor(), session_id)]


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def open_window(state: Any, client: ClientState, texts: UiTexts, *, host: Any = None, on_close: Any = None) -> None:
    """Open the chat.

    With ``host`` (the frame's right-side panel) the chat is rendered **inside** that column, so it sits beside
    the page content instead of floating over the billing keys — the owner's complaint. Without a host it falls
    back to the old bottom-right dialog.
    """
    from nicegui import run, ui  # noqa: PLC0415

    m = build_widget_model(state, client, texts)
    log.debug("CHAT_WINDOW_OPEN user=%s lang=%s offline=%s docked=%s", client.user.user_id if client.user else "-", m.language, bool(m.offline_note), host is not None)
    form: dict[str, Any] = {"text": "", "language": m.language, "session": None}

    if host is not None:
        host.clear()
        host.set_visibility(True)

        def close() -> None:
            host.clear()
            host.set_visibility(False)
            if on_close is not None:
                on_close()

        with host:
            _render_chat(ui, run, state, client, m, form, close)
        return

    # Quasar's q-dialog accepts only standard/top/right/bottom/left; an unknown position ("bottom-right") throws in the
    # browser and the dialog never shows. "right" + an auto top margin on the card anchors it bottom-right.
    with ui.dialog().props("position=right seamless") as dialog, ui.card().classes("vsm-chat w-96 p-0"):
        _render_chat(ui, run, state, client, m, form, dialog.close)
    dialog.open()


def _render_chat(ui: Any, run: Any, state: Any, client: ClientState, m: WidgetModel, form: dict[str, Any], close: Any) -> None:
    """Header · hint · bubbles · suggestion chips · input row, inside whatever container is current."""
    with ui.row().classes("w-full items-center justify-between px-3 py-2").style("background:#B4532A;color:white"):
        ui.label(m.title).classes("font-semibold")
        with ui.row().classes("items-center gap-1"):
            ui.select({code: label for code, label in m.languages}, value=m.language).bind_value(form, "language").props("dense dark borderless options-dense").classes("text-white")
            ui.button(icon="close", on_click=close).props("flat dense color=white")
    if m.offline_note:
        ui.label(m.offline_note).classes("text-xs px-3 py-1").style("background:#FFF3E0;color:#7A4A1E")
    ui.label(m.hint).classes("text-xs text-gray-600 px-3 pt-2")
    log_box = ui.column().classes("w-full gap-1 px-3 py-2 vsm-chatlog").style("max-height: 60vh; overflow-y: auto")
    chips = ui.row().classes("px-3 gap-1 flex-wrap")

    def bubble(role: str, text: str) -> None:
        with log_box, ui.row().classes("w-full " + ("justify-end" if role == "user" else "justify-start")):
            ui.label(text).classes("px-3 py-2 rounded-lg text-sm").style(("background:#B4532A;color:white" if role == "user" else "background:#F4EDE2") + "; max-width: 85%; white-space: pre-wrap")

    async def ask(text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        bubble("user", text)
        form["text"] = ""
        # The model can take 10–30 s on a small PC. Running it on a worker thread keeps NiceGUI's event loop
        # (and the browser's heartbeat) alive — a blocking call here showed "connection lost, trying to reconnect".
        with log_box, ui.row().classes("w-full justify-start") as typing:
            ui.spinner("dots", size="sm").style("color:#B4532A")
            ui.label(m.labels["thinking"]).classes("text-xs text-gray-500")
        try:
            reply = await run.io_bound(send, state, client, text, language=str(form["language"]), session_id=form["session"])
        except Exception as exc:  # noqa: BLE001 - the widget must never die on a provider error
            log.error("CHAT_WIDGET_FAILED error=%s", exc)
            typing.delete()
            bubble("assistant", m.labels["failed"])
            return
        typing.delete()
        form["session"] = reply.session_id
        bubble("assistant", reply.text)
        chips.clear()
        if reply.link:
            with chips:
                ui.button(m.labels["open_report"], icon="open_in_new", on_click=lambda _e, p=reply.link: ui.navigate.to(p)).props("outline dense size=sm")
        if reply.locked:
            with chips:
                ui.label(m.labels["restricted"]).classes("text-xs text-orange-800")

    with chips:
        for s in m.suggestions:
            ui.button(s, on_click=lambda _e, q=s: ask(q)).props("outline dense size=sm")
    with ui.row().classes("w-full items-center gap-1 px-3 pb-3"):
        box = ui.input(placeholder=m.labels["placeholder"]).bind_value(form, "text").classes("flex-1").props("dense outlined autofocus")
        box.on("keydown.enter", lambda: ask(str(form["text"])))
        ui.button(icon="send", on_click=lambda: ask(str(form["text"]))).props("unelevated color=primary dense")
