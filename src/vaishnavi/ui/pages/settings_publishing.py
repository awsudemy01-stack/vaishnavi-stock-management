"""Settings → Publishing (docs/05 §11): channels, destinations, credentials (encrypted), schedule, test send, queue.

View-models are pure and tested; NiceGUI rendering is thin. Tokens/passwords go to ``secure_setting``
(``publish.*``) and are never shown again — the tab only says whether one is stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.api.system import AppState
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_publishing")

CHANNELS: Final = ("folder", "email", "telegram", "whatsapp")
SECRETS: Final = {"email": "publish.smtp_password", "telegram": "publish.telegram_token", "whatsapp": "publish.whatsapp_token"}


@dataclass(frozen=True)
class ChannelOption:
    """One selectable channel."""

    key: str
    label: str
    hint: str


@dataclass(frozen=True)
class QueueRow:
    """Outbox row for the tab."""

    id: int
    channel: str
    document: str
    file: str
    status: str
    attempts: int
    next_retry_at: str
    error: str


@dataclass(frozen=True)
class PublishingModel:
    """The tab."""

    enabled: bool
    channels: list[str]
    channel_options: list[ChannelOption]
    folder: str
    email_to: str
    email_from: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    telegram_chat_id: str
    whatsapp_phone_id: str
    whatsapp_to: str
    digest_time: str
    weekly_weekday: int
    secrets_set: dict[str, bool]
    chip: tuple[str, str]
    counts: dict[str, int]
    queue: list[QueueRow]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """Action result."""

    ok: bool
    message: str = ""
    error_code: str | None = None


def build_publishing_model(state: AppState, texts: UiTexts) -> PublishingModel:
    """Current configuration (no secrets) + outbox state."""
    c = state.extra["services"]
    pub = c.settings_service.effective().publishing
    options = [ChannelOption(k, texts.t(f"publishing.ch_{k}"), texts.t(f"publishing.ch_{k}_hint")) for k in CHANNELS]
    secrets_set = {key: c.secure_settings.is_set(key) for key in SECRETS.values()}
    queue = [QueueRow(**c.publisher.outbox.as_dict(i)) for i in c.publisher.outbox.recent(limit=30)]
    labels = {k: texts.t(f"publishing.{k}") for k in ("title", "intro", "enabled", "channels", "folder", "email_to", "email_from", "smtp_host", "smtp_port", "smtp_user", "smtp_password", "telegram_chat_id", "telegram_token", "whatsapp_phone_id", "whatsapp_to", "whatsapp_token", "digest_time", "weekly_weekday", "save", "test", "send_now", "queue", "stored", "not_stored")}
    return PublishingModel(pub.enabled, list(pub.channels), options, pub.folder, pub.email_to, pub.email_from, pub.smtp_host, pub.smtp_port, pub.smtp_user, pub.telegram_chat_id, pub.whatsapp_phone_id, pub.whatsapp_to, pub.digest_time, pub.weekly_weekday, secrets_set, c.publisher.chip(), c.publisher.outbox.counts(), queue, labels)


def save_publishing(state: AppState, actor: Actor, *, enabled: bool, channels: list[str], folder: str = "", email_to: str = "", email_from: str = "", smtp_host: str = "", smtp_port: int = 587, smtp_user: str = "", smtp_password: str = "", telegram_chat_id: str = "", telegram_token: str = "", whatsapp_phone_id: str = "", whatsapp_to: str = "", whatsapp_token: str = "", digest_time: str = "21:35", weekly_weekday: int = 6) -> Outcome:
    """Validate per enabled channel, store secrets encrypted, write the settings overrides."""
    c = state.extra["services"]
    channels = [ch for ch in channels if ch in CHANNELS]
    problems: list[str] = []
    if enabled and not channels:
        problems.append("choose at least one channel")
    if "folder" in channels and not folder.strip():
        problems.append("folder path is required for the folder channel")
    if "email" in channels and (not smtp_host.strip() or not email_to.strip()):
        problems.append("SMTP host and recipient are required for e-mail")
    if "telegram" in channels and not telegram_chat_id.strip():
        problems.append("Telegram chat id is required")
    if "whatsapp" in channels and (not whatsapp_phone_id.strip() or not whatsapp_to.strip()):
        problems.append("WhatsApp phone number id and recipient are required")
    if problems:
        return Outcome(False, "; ".join(problems))
    try:
        for key, value in ((SECRETS["email"], smtp_password), (SECRETS["telegram"], telegram_token), (SECRETS["whatsapp"], whatsapp_token)):
            if value.strip():
                c.secure_settings.set(actor, key, value.strip())
        c.settings_service.set_many(actor, {"publishing.enabled": enabled, "publishing.channels": channels, "publishing.folder": folder.strip(), "publishing.email_to": email_to.strip(), "publishing.email_from": email_from.strip(), "publishing.smtp_host": smtp_host.strip(), "publishing.smtp_port": int(smtp_port), "publishing.smtp_user": smtp_user.strip(), "publishing.telegram_chat_id": telegram_chat_id.strip(), "publishing.whatsapp_phone_id": whatsapp_phone_id.strip(), "publishing.whatsapp_to": whatsapp_to.strip(), "publishing.digest_time": digest_time.strip(), "publishing.weekly_weekday": int(weekly_weekday)})
    except PermissionDenied as exc:
        return Outcome(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    log.info("PUBLISHING_SAVED enabled=%s channels=%s by=%s", enabled, ",".join(channels), actor.user_id)
    return Outcome(True)


def test_channel(state: AppState, actor: Actor, channel: str) -> Outcome:
    """Send a tiny test document through one channel."""
    c = state.extra["services"]
    if actor.role.value != "OWNER":
        return Outcome(False, "owner only", "E-AUTH-010")
    result = c.publisher.test_channel(channel)
    return Outcome(bool(result.sent), result.summary[0] if result.summary else "")


def send_digest_now(state: AppState, actor: Actor) -> Outcome:
    """Owner button: today's digest right now."""
    c = state.extra["services"]
    if actor.role.value != "OWNER":
        return Outcome(False, "owner only", "E-AUTH-010")
    try:
        result = c.publisher.daily_digest()
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    return Outcome(not result.failed, f"{result.path.name}: sent {', '.join(result.sent) or '—'}" + (f" · failed {', '.join(result.failed)}" if result.failed else ""))


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """Settings tab body."""
    from nicegui import ui  # noqa: PLC0415

    actor = client.actor()
    box = ui.column().classes("w-full gap-3")

    def toast(o: Outcome) -> None:
        ui.notify(o.message or texts.t("common.saved"), type="positive" if o.ok else "negative")

    def draw() -> None:
        m = build_publishing_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["title"]).classes("text-lg font-semibold")
            ui.label(m.labels["intro"]).classes("text-sm text-gray-600").style("max-width: 640px")
            ui.badge(m.chip[1], color={"green": "positive", "amber": "warning", "red": "negative"}.get(m.chip[0], "grey")).props("outline")
            form: dict[str, Any] = {"enabled": m.enabled, "channels": list(m.channels), "folder": m.folder, "email_to": m.email_to, "email_from": m.email_from, "smtp_host": m.smtp_host, "smtp_port": m.smtp_port, "smtp_user": m.smtp_user, "smtp_password": "", "telegram_chat_id": m.telegram_chat_id, "telegram_token": "", "whatsapp_phone_id": m.whatsapp_phone_id, "whatsapp_to": m.whatsapp_to, "whatsapp_token": "", "digest_time": m.digest_time, "weekly_weekday": m.weekly_weekday}
            with ui.column().classes("vsm-card p-3 gap-2"):
                ui.switch(m.labels["enabled"]).bind_value(form, "enabled")
                ui.select({o.key: o.label for o in m.channel_options}, multiple=True, value=list(m.channels), label=m.labels["channels"]).bind_value(form, "channels").classes("w-96").props("use-chips")
                for o in m.channel_options:
                    ui.label(f"{o.label}: {o.hint}").classes("text-xs text-gray-600")
                ui.input(m.labels["folder"]).bind_value(form, "folder").classes("w-96")
                with ui.row().classes("gap-2"):
                    ui.input(m.labels["email_to"]).bind_value(form, "email_to").classes("w-60")
                    ui.input(m.labels["email_from"]).bind_value(form, "email_from").classes("w-60")
                with ui.row().classes("gap-2"):
                    ui.input(m.labels["smtp_host"]).bind_value(form, "smtp_host").classes("w-60")
                    ui.number(m.labels["smtp_port"]).bind_value(form, "smtp_port").classes("w-28")
                    ui.input(m.labels["smtp_user"]).bind_value(form, "smtp_user").classes("w-60")
                    ui.input(m.labels["smtp_password"] + (" ✓" if m.secrets_set["publish.smtp_password"] else ""), password=True, password_toggle_button=True).bind_value(form, "smtp_password").classes("w-60")
                with ui.row().classes("gap-2"):
                    ui.input(m.labels["telegram_chat_id"]).bind_value(form, "telegram_chat_id").classes("w-60")
                    ui.input(m.labels["telegram_token"] + (" ✓" if m.secrets_set["publish.telegram_token"] else ""), password=True, password_toggle_button=True).bind_value(form, "telegram_token").classes("w-80")
                with ui.row().classes("gap-2"):
                    ui.input(m.labels["whatsapp_phone_id"]).bind_value(form, "whatsapp_phone_id").classes("w-60")
                    ui.input(m.labels["whatsapp_to"]).bind_value(form, "whatsapp_to").classes("w-60")
                    ui.input(m.labels["whatsapp_token"] + (" ✓" if m.secrets_set["publish.whatsapp_token"] else ""), password=True, password_toggle_button=True).bind_value(form, "whatsapp_token").classes("w-80")
                with ui.row().classes("gap-2"):
                    ui.input(m.labels["digest_time"]).bind_value(form, "digest_time").classes("w-28")
                    ui.select({i: d for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))}, value=m.weekly_weekday, label=m.labels["weekly_weekday"]).bind_value(form, "weekly_weekday").classes("w-40")
                with ui.row().classes("gap-2"):
                    ui.button(m.labels["save"], icon="save", on_click=lambda: (toast(save_publishing(state, actor, **{k: (int(v) if k in ("smtp_port", "weekly_weekday") else v) for k, v in form.items()})), draw())).props("unelevated color=primary")
                    for ch in m.channels:
                        ui.button(f"{m.labels['test']} {ch}", icon="send", on_click=lambda _e, n=ch: toast(test_channel(state, actor, n))).props("outline")
                    ui.button(m.labels["send_now"], icon="summarize", on_click=lambda: (toast(send_digest_now(state, actor)), draw())).props("outline")
            with ui.column().classes("vsm-card p-3 gap-1"):
                ui.label(f"{m.labels['queue']} · pending {m.counts['PENDING']} · sent {m.counts['SENT']} · failed {m.counts['FAILED']}").classes("font-semibold")
                for q in m.queue[:15]:
                    ui.label(f"#{q.id} {q.channel} · {q.document} · {q.file} · {q.status} · attempts {q.attempts}" + (f" · {q.error}" if q.error else "")).classes("text-xs")

    draw()
