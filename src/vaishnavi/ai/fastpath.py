"""Fast path — answers routine counter questions without the LLM (docs/06 §4a).

A small set of deterministic intents in EN / Hindi / Hinglish is matched **before** the model is involved
and answered from the same tools the LLM would call, through bilingual templates. < 100 ms, works when the
provider is down or switched off, and is the *only* path for ``provider = none``. Role rules are the
guardrail's job; the fast path simply refuses to answer an Owner-only intent for staff (returns None so the
caller's guardrail refusal stands).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from vaishnavi.core.permissions import Role

log = logging.getLogger("vaishnavi.ai.fastpath")

ToolRunner = Callable[..., dict[str, Any]]
"""``run_tool(name, **args) -> result dict`` (the ToolRegistry's JSON shape)."""


@dataclass(frozen=True)
class FastIntent:
    """A matched intent."""

    name: str
    tool: str
    query: str | None = None
    owner_only: bool = False


@dataclass(frozen=True)
class FastAnswer:
    """A rendered answer."""

    text: str
    source: str
    link: str | None = None
    intent: str = ""


_STOP: Final = frozenset("the a an of is are ka ki ke kya hai hain kitna kitne kitni stock price rate mrp daam dam left bacha bache available item please pls batao bata do dikhao tell me show what how much many कितना कितनी कितने है हैं का की के स्टॉक दाम कीमत रेट बताओ".split())

_GREETING = re.compile(r"^\W*(hi|hii|hello|hey|namaste|namaskar|hello ji|good (morning|evening|afternoon)|नमस्ते|नमस्कार)\W*$", re.IGNORECASE)
_LOW = re.compile(r"\b(low stock|kam stock|stock kam|running low|below minimum|reorder|कम स्टॉक|स्टॉक कम)\b", re.IGNORECASE)
_OUT = re.compile(r"\b(out of stock|khatam|khatm|khali|finished|empty|zero stock|ख़त्म|खत्म|खाली)\b", re.IGNORECASE)
_SALES = re.compile(r"\b(today.?s? (sale|sales|total|collection)|aaj (ka|ki) (total|sale|bikri|kamai|collection)|current sale|आज की (बिक्री|कुल)|आज का (टोटल|कुल))\b", re.IGNORECASE)
_HOWTO = re.compile(r"^\W*(how (do|can|to) (i|we)?\s*|(.+?)\s+kaise\s+(kare|karein|karu|karte|hota hai)\W*$|कैसे)", re.IGNORECASE)
_PRICE = re.compile(r"\b(price|rate|mrp|daam|dam|kimat|keemat|cost|कीमत|दाम|रेट)\b", re.IGNORECASE)
_STOCK = re.compile(r"\b(stock|kitna|kitne|kitni|left|bacha|bache|available|quantity|qty|कितना|कितनी|कितने|स्टॉक)\b", re.IGNORECASE)


def _subject(text: str, *words: str) -> str:
    """The product words: the message minus intent/stop words and filler ("stock of X" → "x")."""
    clean = re.sub(r"[^\w\s\u0900-\u097F-]", " ", text.lower())
    for w in words:
        clean = re.sub(rf"\b{re.escape(w)}\b", " ", clean)
    tokens = [t for t in clean.split() if t not in _STOP and t not in ("of", "for", "me", "ki", "ka", "ke", "wala", "wale", "hai", "kya", "क्या", "का")]
    return " ".join(tokens).strip()


def match(text: str) -> FastIntent | None:
    """Recognise one of the documented intents; None → go to the LLM."""
    t = text.strip()
    if not t or len(t) > 160:
        return None
    if _GREETING.match(t):
        return FastIntent("greeting", "greeting")
    simple = len(t.split()) <= 6  # longer questions carry comparisons/conditions → the LLM
    if simple and _LOW.search(t):
        return FastIntent("low_stock", "low_stock_items")
    if simple and _OUT.search(t):
        return FastIntent("out_of_stock", "out_of_stock_items")
    if simple and _SALES.search(t):
        return FastIntent("sales_today", "sales_summary", owner_only=True)
    m = _HOWTO.search(t)
    if m:
        topic = re.sub(r"^\W*how (do|can|to) (i|we)?\s*", "", t, flags=re.IGNORECASE)
        topic = re.sub(r"\s+kaise\s+(kare|karein|karu|karte|hota hai)\W*$", "", topic, flags=re.IGNORECASE).strip(" ?.!")
        topic = re.sub(r"कैसे.*$", "", topic).strip(" ?।")
        if topic:
            return FastIntent("how_to", "explain_screen", query=topic)
    if _PRICE.search(t):
        subject = _subject(t, "price", "rate", "mrp", "daam", "dam", "kimat", "keemat", "cost", "कीमत", "दाम", "रेट")
        if subject:
            return FastIntent("price_of", "item_price", query=subject)
    if _STOCK.search(t):
        subject = _subject(t, "stock", "kitna", "kitne", "kitni", "left", "bacha", "bache", "available", "quantity", "qty", "कितना", "कितनी", "कितने", "स्टॉक")
        if subject and len(subject.split()) <= 4:
            return FastIntent("stock_of", "get_stock", query=subject)
    return None


# ----------------------------------------------------------------------------- templates
_T: Final[dict[str, dict[str, str]]] = {
    "english": {
        "greeting": "Namaste! Ask me about stock or prices — e.g. \"stock of camphor\", \"low stock\".",
        "greeting_owner": "Namaste! Ask me about stock, prices, today's sales, demand or the forecast.",
        "stock": "{name}: {qty} {unit} left (min {min_stock}).",
        "stock_cat": "{name}: {qty} {unit} left (min {min_stock}) · {category}.",
        "not_found": "\"{query}\" not found in the catalogue. Try another spelling or the item code.",
        "price": "{name}: {price} per {unit} (MRP {mrp}).",
        "low_none": "No item is below its minimum right now.",
        "low": "{count} item(s) at or below minimum: {items}.",
        "out_none": "Nothing is out of stock right now.",
        "out": "Out of stock ({count}): {items}.",
        "sales": "Today {net} · {bills} bills · average {avg_bill} · cash {cash_pct}%. Source: sales_summary, {period}.",
        "howto": "{title}: {steps}",
        "howto_none": "I could not find that in the user guide. Open Help from the menu, or ask the owner.",
        "offline": "The assistant is offline — simple stock and price questions are still answered.",
    },
    "hindi": {
        "greeting": "नमस्ते! स्टॉक या कीमत पूछें — जैसे \"कपूर का स्टॉक\", \"कम स्टॉक\"।",
        "greeting_owner": "नमस्ते! स्टॉक, कीमत, आज की बिक्री, माँग या पूर्वानुमान पूछें।",
        "stock": "{name}: {qty} {unit} बचे हैं (न्यूनतम {min_stock})।",
        "stock_cat": "{name}: {qty} {unit} बचे हैं (न्यूनतम {min_stock}) · {category}।",
        "not_found": "\"{query}\" कैटलॉग में नहीं मिला। दूसरी स्पेलिंग या आइटम कोड आज़माएँ।",
        "price": "{name}: {price} प्रति {unit} (MRP {mrp})।",
        "low_none": "अभी कोई वस्तु न्यूनतम से नीचे नहीं है।",
        "low": "{count} वस्तु(एँ) न्यूनतम पर या नीचे: {items}।",
        "out_none": "अभी कुछ भी ख़त्म नहीं है।",
        "out": "ख़त्म ({count}): {items}।",
        "sales": "आज {net} · {bills} बिल · औसत {avg_bill} · नकद {cash_pct}%। स्रोत: sales_summary, {period}।",
        "howto": "{title}: {steps}",
        "howto_none": "यह उपयोगकर्ता गाइड में नहीं मिला। मेनू से सहायता खोलें, या मालिक से पूछें।",
        "offline": "सहायक ऑफ़लाइन है — स्टॉक और कीमत के सरल सवालों के जवाब फिर भी मिलेंगे।",
    },
    "hinglish": {
        "greeting": "Namaste! Stock ya price poochhein — jaise \"camphor kitna hai\", \"kam stock\".",
        "greeting_owner": "Namaste! Stock, price, aaj ki sale, demand ya forecast poochhein.",
        "stock": "{name}: {qty} {unit} bache hain (min {min_stock}).",
        "stock_cat": "{name}: {qty} {unit} bache hain (min {min_stock}) · {category}.",
        "not_found": "\"{query}\" catalogue mein nahi mila. Doosri spelling ya item code try karein.",
        "price": "{name}: {price} per {unit} (MRP {mrp}).",
        "low_none": "Abhi koi item minimum se neeche nahi hai.",
        "low": "{count} item minimum par ya neeche: {items}.",
        "out_none": "Abhi kuch bhi khatam nahi hai.",
        "out": "Khatam ({count}): {items}.",
        "sales": "Aaj {net} · {bills} bills · average {avg_bill} · cash {cash_pct}%. Source: sales_summary, {period}.",
        "howto": "{title}: {steps}",
        "howto_none": "Yeh user guide mein nahi mila. Menu se Help kholein, ya owner se poochhein.",
        "offline": "Assistant offline hai — stock aur price ke simple sawaal phir bhi answer honge.",
    },
}


def template(language: str, key: str) -> str:
    """Template text for a language (English fallback)."""
    return _T.get(language, _T["english"]).get(key, _T["english"][key])


def _items(rows: list[dict[str, Any]], *, with_qty: bool, limit: int = 10) -> str:
    parts = [f"{r.get('name')} ({r.get('qty')})" if with_qty else str(r.get("name")) for r in rows[:limit]]
    return ", ".join(parts) + (" …" if len(rows) > limit else "")


def answer(text: str, *, role: Role, language: str, run_tool: ToolRunner) -> FastAnswer | None:
    """Match and answer; None when no intent matches or the intent is Owner-only for a staff user."""
    intent = match(text)
    if intent is None:
        return None
    if intent.owner_only and role is not Role.OWNER:
        log.info("AI_FASTPATH intent=%s refused_for_role=%s", intent.name, role.value)
        return None
    tpl = lambda key: template(language, key)  # noqa: E731
    try:
        if intent.tool == "greeting":
            out = FastAnswer(tpl("greeting_owner" if role is Role.OWNER else "greeting"), "greeting", None, intent.name)
        elif intent.tool == "get_stock":
            r = run_tool("get_stock", query=intent.query)
            if not r.get("found"):
                out = FastAnswer(tpl("not_found").format(query=intent.query), "get_stock", None, intent.name)
            else:
                key = "stock_cat" if r.get("category") else "stock"
                out = FastAnswer(tpl(key).format(name=r.get("name"), qty=r.get("qty"), unit=r.get("unit", ""), min_stock=r.get("min_stock", "-"), category=r.get("category", "")), "get_stock", f"/stock?product={r.get('product_id')}" if r.get("product_id") else None, intent.name)
        elif intent.tool == "item_price":
            r = run_tool("item_price", query=intent.query)
            out = FastAnswer(tpl("price").format(name=r.get("name"), price=r.get("price"), unit=r.get("unit", ""), mrp=r.get("mrp", "-")) if r.get("found") else tpl("not_found").format(query=intent.query), "item_price", None, intent.name)
        elif intent.tool == "low_stock_items":
            r = run_tool("low_stock_items", limit=10)
            rows = list(r.get("items") or [])
            out = FastAnswer(tpl("low").format(count=r.get("count", len(rows)), items=_items(rows, with_qty=True)) if rows else tpl("low_none"), "low_stock_items", "/reports/low_stock", intent.name)
        elif intent.tool == "out_of_stock_items":
            r = run_tool("out_of_stock_items", limit=10)
            rows = list(r.get("items") or [])
            out = FastAnswer(tpl("out").format(count=r.get("count", len(rows)), items=_items(rows, with_qty=False)) if rows else tpl("out_none"), "out_of_stock_items", "/reports/low_stock", intent.name)
        elif intent.tool == "sales_summary":
            r = run_tool("sales_summary", period="today")
            out = FastAnswer(tpl("sales").format(net=r.get("net"), bills=r.get("bills"), avg_bill=r.get("avg_bill"), cash_pct=r.get("cash_pct", 0), period=r.get("period", "")), "sales_summary", "/reports/sales", intent.name)
        elif intent.tool == "explain_screen":
            r = run_tool("explain_screen", query=intent.query)
            out = FastAnswer(tpl("howto").format(title=r.get("title"), steps=" → ".join(r.get("steps") or [])) if r.get("found") else tpl("howto_none"), "explain_screen", r.get("link"), intent.name)
        else:  # pragma: no cover - every tool above is enumerated
            return None
    except Exception as exc:  # noqa: BLE001 - the fast path must never raise into the widget
        log.error("AI_FASTPATH_FAILED intent=%s error=%s", intent.name, exc)
        return None
    log.info("AI_FASTPATH intent=%s tool=%s role=%s lang=%s", intent.name, intent.tool, role.value, language)
    return out
