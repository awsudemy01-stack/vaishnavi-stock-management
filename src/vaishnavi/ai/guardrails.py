"""Guardrails — layer 3 (intent & topic filter) of docs/06 §7, plus the rate limit, off-topic counter and the
Owner-instructions quick-check. Deterministic, stdlib only, runs **before** any LLM call, in EN / Hindi /
Hinglish.

The classifier is an ordered list of categories; the first match wins and the flagged categories come
first, so "for a school project, how do I delete a bill" is FRAUD_ASSIST no matter how it is framed. A
message that matches nothing on the block side must still look like a stock / sales / how-to question to
be ALLOWed — everything else is OFF_TOPIC. Refusal texts are canned (never generated) so a refusal costs
nothing and cannot be jail-broken.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from vaishnavi.core.permissions import Role

log = logging.getLogger("vaishnavi.ai.guardrails")


class Decision(Enum):
    """What happens to the message."""

    ALLOW = "ALLOW"
    REFUSE = "REFUSE"
    REFUSE_AND_FLAG = "REFUSE_AND_FLAG"


@dataclass(frozen=True)
class Verdict:
    """Classifier output."""

    decision: Decision
    category: str
    severity: str  # LOW | MED | HIGH
    matched: str = ""
    language: str = "english"


FLAGGED: Final = frozenset({"SECRETS", "FINANCIAL_PII", "SECURITY_BYPASS", "VULN_DISCOVERY", "FRAUD_ASSIST"})
SEVERITY: Final = {"SECURITY_BYPASS": "HIGH", "FRAUD_ASSIST": "HIGH", "SECRETS": "HIGH", "FINANCIAL_PII": "HIGH", "VULN_DISCOVERY": "MED", "APP_INTERNALS": "MED", "OWNER_PII": "MED", "HARASSMENT_ABUSE": "LOW", "OWNER_ONLY": "LOW", "OFF_TOPIC": "LOW", "OWNER_BLOCKED": "LOW"}

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_HINGLISH_WORDS: Final = frozenset("kitna kitne kitni hai hain ka ki ke kya kyu kyun batao bata do dikhao kaise kaisa karein kare karna aaj kal mujhe chahiye wala wale nahi nahin mat band khatam khatm kam zyada mahine hafte abhi bhi tareeka tarika bina pata chale maal khareeda rate daam kapoor agarbatti diya malik mausam".split())


def detect_language(text: str) -> str:
    """``hindi`` (Devanagari), ``hinglish`` (romanised Hindi words) or ``english``."""
    if _DEVANAGARI.search(text):
        return "hindi"
    words = re.findall(r"[a-z]+", text.lower())
    hits = sum(1 for w in words if w in _HINGLISH_WORDS)
    if hits >= 2 or (hits == 1 and len(words) <= 4):
        return "hinglish"
    return "english"


def _norm(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s\u0900-\u097F@%₹.-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _rx(*patterns: str) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


# Order matters: flagged categories first, then the rest. Patterns are matched on the normalised text.
_BLOCKED: Final[list[tuple[str, list[re.Pattern[str]]]]] = [
    ("SECURITY_BYPASS", _rx(
        r"\b(breach|hack|hacking|intrude|break in(to)?|bypass|backdoor|tamper|manipulate the code|ghusna|ghus(ne|na))\b",
        r"\b(disable|turn off|stop|band|bandh|off kar)\b.{0,30}\b(audit|log|logging|trail|camera|guard)",
        r"\b(audit|log|logging)\b.{0,20}\b(band|bandh|disable|off)\b",
        r"\b(login|log in|enter|access)\b.{0,40}\b(without|bina)\b.{0,20}\b(pin|password|owner|permission)",
        r"\bbina\b.{0,20}\b(pin|password)\b.{0,30}\b(login|owner|admin)",
        r"\b(as|like)\s+(admin|owner|root)\b.{0,30}\bwithout\b",
        r"\b(ignore|forget|disregard)\b.{0,20}\b(previous|prior|all|above|the)\b.{0,20}\b(instructions?|rules?|prompt)",
        r"\b(developer|debug|god|dan|unrestricted|jailbreak)\s*(mode)?\b.{0,40}\b(reveal|show|print|system prompt|instructions)",
        r"\b(system prompt|your instructions|your rules)\b.{0,30}\b(reveal|show|print|tell|dump)",
        r"\b(reveal|show|print|tell|dump)\b.{0,30}\b(system prompt|your instructions|your rules)",
        r"पता चले बिना.*(सिस्टम|घुस)|घुसना|हैक",
    )),
    ("FRAUD_ASSIST", _rx(
        r"\b(delete|remove|erase|hide|hata|hatao|mita|mitao|udaa|udao)\b.{0,30}\b(bill|invoice|sale|receipt|entry|record|movement|transaction)",
        r"\b(bill|invoice|sale|receipt)\b.{0,30}\b(delete|remove|erase|hata|hatao|mita)",
        r"\b(false|fake|wrong|galat|jhoot(h|a)?|lower|less|zero|no)\b.{0,30}\b(figure|sale|sales|total|entry|amount|number)",
        r"\b(update|change|edit|alter|modify|badal|badlo)\b.{0,40}\b(sale|sales|total|amount|figure|stock|bill|entry|record)\b",
        r"\b(sell|bech|bechna|take|le|lena|nikal)\b.{0,40}\b(without|bina)\b.{0,30}\b(owner|malik|bill|billing|knowing|pata|showing|record)",
        r"\b(owner|malik)\b.{0,20}\b(couldnt|could not|cant|cannot|wont|not|na|nahi)\b.{0,20}\b(know|pata|chale|find out|notice)",
        r"\b(stock|inventory)\b.{0,20}\b(kam|less|lower)\b.{0,20}\b(dikha|show)",
        r"\b(cash|paisa|paise|money)\b.{0,30}\b(drawer|counter|galla)\b.{0,40}\b(without|bina|hide|chhupa)",
        r"\brecord(ed)?\b.{0,10}\b(zero|no)\b.{0,10}\bsale",
        r"बिल\s*(हटा|मिटा|डिलीट)|(हटाने|मिटाने)\s*का\s*तरीका|झूठ(ा|ी)?\s*(बिक्री|आंकड़ा|एंट्री)|मालिक\s*को\s*(पता|बताए)\s*(न|बिना)",
    )),
    ("VULN_DISCOVERY", _rx(
        r"\b(bug|bugs|loophole|loopholes|vulnerabilit(y|ies)|weakness|weak point|exploit|flaw|kamzori|kamjori)\b",
        r"\b(crash|freeze|hang|break|kill|overload|ddos)\b.{0,40}\b(app|application|software|billing|system|server|pos)",
        r"\b(app|application|software|billing|system)\b.{0,20}\b(crash|freeze|hang|tod|band|kharab)\b.{0,20}\b(kaise|how|karein|kare)",
        r"कमज़ोरी|कमजोरी|क्रैश|खामी",
    )),
    ("SECRETS", _rx(
        r"\b(pin|password|passcode|paasword|pasword|pass word|otp|recovery key|secret key|api key|token|credentials?)\b",
        r"\b(settings?|config|configuration|secrets?|\.env|toml)\s*(file|files)?\b.{0,20}\b(show|open|read|print|dikhao|batao|dump)|\b(show|open|read|print|dikhao|batao|dump)\b.{0,20}\b(settings?|config|configuration|secrets?)\s*(file|files)?\b",
        r"पासवर्ड|पिन कोड|गुप्त कुंजी",
    )),
    ("FINANCIAL_PII", _rx(
        r"\b(bank|account|a/c|acct|ifsc|upi|vpa|card|debit|credit card|cvv|paytm|gpay|phonepe)\b.{0,30}\b(detail|details|number|no|id|code|owner|malik|customer|shop|kya|what|where|kaha)",
        r"\b(detail|details|number|id)\b.{0,20}\b(bank|account|upi|card|ifsc)\b",
        r"\bwhere\b.{0,20}\bmoney\b.{0,20}\b(go|goes)",
        r"बैंक|खाता\s*नंबर|यूपीआई|कार्ड\s*(नंबर|डिटेल)",
    )),
    ("OWNER_PII", _rx(
        r"\b(owner|malik|boss|proprietor)s?\b.{0,20}\b(phone|mobile|number|whatsapp|address|ghar|home|house|aadhaar|aadhar|pan|dob|birthday|family|wife|personal)",
        r"\b(phone|mobile|address|aadhaar|aadhar|pan card)\b.{0,20}\b(of|ka|ki|ke)\b.{0,10}\b(owner|malik|boss)",
        r"मालिक\s*(का|की|के)\s*(मोबाइल|फ़ोन|फोन|पता|घर|आधार)",
    )),
    ("APP_INTERNALS", _rx(
        r"\b(source|application|app|program|billing)\s*(codes?|code base|codebase)\b",
        r"\b(codes?|code base|codebase)\b.{0,20}\b(share|show|print|send|dikhao|do|batao|dikha)|\b(share|show|print|send|dikhao|dikha|reveal)\b.{0,30}\b(codes?|codebase|source)",
        r"\b(how|kaise)\b.{0,20}\b(built|made|developed|written|coded|bana|banaya)\b.{0,40}\b(app|application|software|billing|this|yeh|ye)",
        r"\b(which|what|kaun|kaunsa|kon)\b.{0,20}\b(database|db|framework|language|library|stack|server)\b",
        r"\b(database|db|table|tables|schema|column|columns|sql)\b.{0,30}\b(show|structure|schema|name|names|list|dikhao)|\bschema\b",
        r"\b(api|endpoint|endpoints|route|routes|port|ports|file path|file paths|folder|directory|stored)\b",
        r"\b(where|kaha|kahan)\b.{0,20}\b(files?|data|database)\b.{0,20}\b(stored|saved|kept|rakha|hai)",
        r"कोड\s*(दिखाओ|बताओ)|किस\s*भाषा\s*में\s*बना|डेटाबेस",
    )),
    ("HARASSMENT_ABUSE", _rx(
        r"\b(idiot|stupid|fool|bastard|bitch|asshole|moron|thief|chor|kamina|kamine|harami|kutta|kutte|saala|sala|gandu|madarchod|bhenchod|chutiya)\b",
        r"चोर|कमीना|हरामी|कुत्ता|गधा",
    )),
]

_OWNER_ONLY: Final = _rx(
    r"\b(sale|sales|total|turnover|revenue|bikri|bikree|kamai|collection)\b",
    r"\b(profit|margin|munafa|faida|loss|nuksan)\b",
    r"\b(purchase|purchases|khareed|khareeda|kharida|bought|supplier)\b",
    r"\b(forecast|predict|prediction|future|demand|trend|bhavishya|aage)\b",
    r"\b(cashier|staff|ramesh|variance|shift report|performance|attendance|who sold|kisne becha)\b",
    r"\b(report|reports|audit|discount audit|void|refund|shrinkage|backup|doctor|logs?)\b",
    r"बिक्री|कुल|मुनाफ़ा|मुनाफा|लाभ|खरीद|पूर्वानुमान|माँग|मांग|रिपोर्ट|कैशियर",
)

_OFF_TOPIC_HINTS: Final = _rx(
    r"\b(joke|jokes|chutkula|cricket|football|match score|ipl|movie|film|song|gaana|poem|kavita|shayari|story|kahani|recipe|weather|mausam|election|politics|chunav|homework|essay|assignment|exam|news|celebrity|horoscope|rashifal|love|girlfriend|boyfriend|marry|shaadi)\b",
    r"कविता|चुटकुला|मौसम|क्रिकेट|फ़िल्म|फिल्म|गाना|कहानी|चुनाव|राशिफल",
)

_ALLOWED: Final = _rx(
    r"\b(stock|inventory|quantity|qty|left|available|availability|bacha|bache|kitna|kitne|kitni|pieces|pcs)\b",
    r"\b(price|rate|mrp|cost|daam|dam|kimat|keemat|paisa|rupees?|rs|₹)\b",
    r"\b(item|items|product|products|saman|samaan|maal|cheez|barcode|code|sku|category)\b",
    r"\b(low|kam|out of stock|khatam|khatm|khali|finish|finished|empty|minimum|reorder)\b",
    r"\b(how (do|can|to)|kaise|kaisa|steps?|help|madad|where is|kahan hai)\b",
    r"\b(bill|billing|hold|resume|scan|scanner|print|printer|receipt|reprint|shift|open|close|login|logout|lock|f3|find|weigh|weight|discount|offer|payment|upi|cash|card)\b",
    r"\b(sale|sales|sell|sold|selling|becha|bechi|bikri|total|turnover|profit|purchase|forecast|demand|dead stock|top|best|worst|slow|fast|report|cashier|variance|backup|status|health|integrity|setting|settings|error|e-[a-z]+-\d+)\b",
    r"\b(compare|comparison|trend|today|yesterday|week|month|year|aaj|kal|hafta|hafte|mahina|mahine|diwali|holi|navratri|festival|tyohar)\b",
    r"\b(hi|hello|hey|namaste|namaskar|hello ji|good (morning|evening|afternoon)|thanks|thank you|dhanyavad|shukriya)\b",
    r"[\u0900-\u097F]{2,}",  # any Devanagari question that passed the block lists is taken as on-topic
    r"\b(camphor|kapoor|kapur|agarbatti|dhoop|diya|ghee|wick|wicks|batti|idol|murti|kumkum|haldi|chandan|roli|cloth|vastra|mala|garland|flower|phool|thali|lamp)\b",
)


def classify(text: str, *, role: Role, extra_blocked: Iterable[str] = ()) -> Verdict:
    """Layer-3 decision for one message (pure)."""
    language = detect_language(text)
    norm = _norm(text)
    for category, patterns in _BLOCKED:
        for p in patterns:
            m = p.search(norm)
            if m:
                decision = Decision.REFUSE_AND_FLAG if category in FLAGGED else Decision.REFUSE
                log.info("GUARDRAIL decision=%s category=%s role=%s lang=%s match=%r", decision.value, category, role.value, language, m.group(0)[:40])
                return Verdict(decision, category, SEVERITY[category], m.group(0), language)
    for phrase in extra_blocked:
        ph = _norm(phrase)
        if ph and ph in norm:
            log.info("GUARDRAIL decision=REFUSE category=OWNER_BLOCKED role=%s phrase=%r", role.value, ph)
            return Verdict(Decision.REFUSE, "OWNER_BLOCKED", "LOW", ph, language)
    if role is not Role.OWNER:
        for p in _OWNER_ONLY:
            m = p.search(norm)
            if m:
                log.info("GUARDRAIL decision=REFUSE category=OWNER_ONLY role=%s match=%r", role.value, m.group(0))
                return Verdict(Decision.REFUSE, "OWNER_ONLY", "LOW", m.group(0), language)
    for p in _OFF_TOPIC_HINTS:
        m = p.search(norm)
        if m:
            log.info("GUARDRAIL decision=REFUSE category=OFF_TOPIC role=%s lang=%s match=%r", role.value, language, m.group(0))
            return Verdict(Decision.REFUSE, "OFF_TOPIC", "LOW", m.group(0), language)
    for p in _ALLOWED:
        m = p.search(norm)
        if m:
            log.debug("GUARDRAIL decision=ALLOW role=%s lang=%s match=%r", role.value, language, m.group(0))
            return Verdict(Decision.ALLOW, "ALLOWED", "LOW", m.group(0), language)
    log.info("GUARDRAIL decision=REFUSE category=OFF_TOPIC role=%s lang=%s", role.value, language)
    return Verdict(Decision.REFUSE, "OFF_TOPIC", "LOW", "", language)


# ----------------------------------------------------------------------------- refusal templates
_REFUSALS: Final[dict[str, dict[str, str]]] = {
    "english": {
        "default": "Sorry — I can only help with stock, sales and how to use this app.",
        "flag": " This request has been reported to the owner.",
        "OWNER_ONLY": "Only the owner can see this. I can help with stock and price questions.",
        "OFF_TOPIC": "I can only answer questions about stock, sales and this app. Try: \"stock of camphor\" or \"low stock\".",
        "HARASSMENT_ABUSE": "Let's keep it about the shop. Ask me about stock or prices.",
    },
    "hindi": {
        "default": "क्षमा करें, मैं केवल स्टॉक, बिक्री और ऐप के उपयोग से जुड़े प्रश्नों का उत्तर दे सकता हूँ।",
        "flag": " यह अनुरोध मालिक को रिपोर्ट किया गया है।",
        "OWNER_ONLY": "यह जानकारी केवल मालिक देख सकते हैं। मैं स्टॉक और कीमत से जुड़े सवालों में मदद कर सकता हूँ।",
        "OFF_TOPIC": "मैं केवल स्टॉक, बिक्री और इस ऐप के बारे में बता सकता हूँ। पूछें: \"कपूर का स्टॉक\" या \"कम स्टॉक\"।",
        "HARASSMENT_ABUSE": "चलिए दुकान की बात करते हैं। स्टॉक या कीमत पूछें।",
    },
    "hinglish": {
        "default": "Sorry — main sirf stock, sales aur app ke use se jude sawaalon mein madad kar sakta hoon.",
        "flag": " Yeh request owner ko report kar di gayi hai.",
        "OWNER_ONLY": "Yeh jaankari sirf owner dekh sakte hain. Main stock aur price ke sawaalon mein madad kar sakta hoon.",
        "OFF_TOPIC": "Main sirf stock, sales aur is app ke baare mein bata sakta hoon. Poochhein: \"camphor kitna hai\" ya \"kam stock\".",
        "HARASSMENT_ABUSE": "Dukaan ki baat karte hain. Stock ya price poochhein.",
    },
}


def refusal(verdict: Verdict, language: str) -> str:
    """Canned refusal in the user's language (flagged categories add the 'reported' sentence)."""
    table = _REFUSALS.get(language, _REFUSALS["english"])
    text = table.get(verdict.category, table["default"])
    if verdict.decision is Decision.REFUSE_AND_FLAG:
        text += table["flag"]
    return text


# ----------------------------------------------------------------------------- limits
class RateLimiter:
    """Sliding one-minute window per user (``ai.max_messages_per_minute``)."""

    def __init__(self, *, max_per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        self._max = max_per_minute
        self._now = now
        self._hits: dict[int, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int) -> bool:
        """True when the message may proceed."""
        with self._lock:
            t = self._now()
            window = [h for h in self._hits.get(user_id, []) if t - h < 60]
            if len(window) >= self._max:
                self._hits[user_id] = window
                log.warning("GUARDRAIL_RATE_LIMIT user=%s per_minute=%d code=E-GRD-004", user_id, self._max)
                return False
            window.append(t)
            self._hits[user_id] = window
            return True


class OffTopicCounter:
    """``limit`` off-topic messages in a session → "stock questions only" for ``lockout_minutes``."""

    def __init__(self, *, limit: int, lockout_minutes: int, now: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._lockout = lockout_minutes * 60
        self._now = now
        self._counts: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str) -> bool:
        """Count one off-topic message; returns True when this one triggered the lockout."""
        with self._lock:
            n = self._counts.get(session_id, 0) + 1
            self._counts[session_id] = n
            if n >= self._limit:
                self._locked_until[session_id] = self._now() + self._lockout
                self._counts[session_id] = 0
                log.warning("GUARDRAIL_OFF_TOPIC_LOCK session=%s minutes=%d", session_id, self._lockout // 60)
                return True
            return False

    def is_locked(self, session_id: str) -> bool:
        """Restricted mode active?"""
        with self._lock:
            until = self._locked_until.get(session_id)
            return until is not None and self._now() < until


# ----------------------------------------------------------------------------- owner instructions quick-check
_WIDENING: Final = _rx(
    r"\bignore\b.{0,30}\b(core|rules?|guardrails?|restrictions?|instructions?)",
    r"\b(no|without)\s+(restrictions?|limits?|rules?)\b",
    r"\b(answer|reply to)\s+(any|every|all)\s+(question|questions|request)",
    r"\b(share|reveal|show|give)\b.{0,30}\b(bank|upi|account|card|password|pin|key|code|source|codebase|internals?)",
    r"\b(bank|upi|account number|card|password|pin|api key|recovery key)\b",
    r"\b(delete|edit|change)\b.{0,20}\b(bill|sale|record|stock)",
)


def quick_check_instructions(text: str) -> list[str]:
    """Problems that stop an Owner-instructions version from activating (docs/06 §7 layer 4)."""
    norm = _norm(text)
    problems = [f"instruction widens the assistant's scope or touches protected data: '{m.group(0)}'" for p in _WIDENING if (m := p.search(norm))]
    if len(text) > 4000:
        problems.append("instructions longer than 4000 characters")
    if problems:
        log.warning("PROMPT_QUICK_CHECK_FAILED problems=%d", len(problems))
    return problems
