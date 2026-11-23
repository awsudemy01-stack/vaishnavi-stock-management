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
from dataclasses import dataclass, field
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
    args: dict[str, Any] = field(default_factory=dict)
    """Extra tool arguments decided from the question (period, order, horizon)."""


@dataclass(frozen=True)
class FastAnswer:
    """A rendered answer."""

    text: str
    source: str
    link: str | None = None
    intent: str = ""
    tentative: bool = False
    """True when the fast path could not find the item: the LLM (with tool search) may do better; use this text only when no model is available."""


_STOP: Final = frozenset(
    "the a an of is are ka ki ke kya hai hain kitna kitne kitni stock price rate mrp daam dam left bacha bache available item items "
    "please pls batao bata do dikhao tell me show what how much many which kaun kaunsa konsa kon sa or aur abhi ab now today aaj "
    "list sab saara sara pura poora full total all mera mere meri hamara hamare apna shop dukan dukaan mein main me pe par hai hain ji "
    "se sabka sabki haal jankari jaankari chahiye detail details info information "
    "कितना कितनी कितने है हैं का की के स्टॉक दाम कीमत रेट बताओ कौन सा कौनसा अभी सब सारा पूरा लिस्ट सूची दिखाओ".split()
)

_GREETING = re.compile(r"^\W*(hi|hii|hello|hey|namaste|namaskar|hello ji|good (morning|evening|afternoon)|नमस्ते|नमस्कार)\W*$", re.IGNORECASE)
_LOW = re.compile(r"\b(low stock|kam stock|stock kam|running low|below minimum|reorder|कम स्टॉक|स्टॉक कम)\b", re.IGNORECASE)
_OUT = re.compile(r"\b(out of stock|khatam|khatm|khali|finished|empty|zero stock|ख़त्म|खत्म|खाली)\b", re.IGNORECASE)
_OVERVIEW = re.compile(
    r"\b(stock (list|kya hai|dikhao|dikhaiye|batao|status|summary|report|position|overview|details?)|(all|full|total|whole|complete|sab|saara|sara|pura|poora) (stock|items?|inventory)|"
    r"inventory|stock list|items? (in|available in) stock|(kaun ?sa|konsa|which) items?|kya kya hai|what (do we|do i|all) have)\b"
    r"|स्टॉक (सूची|लिस्ट|दिखाओ|क्या है|बताओ)|(सारा|पूरा|सब) स्टॉक",  # no \b around Devanagari: matras are not \w
    re.IGNORECASE,
)
_SALES = re.compile(r"\b(today.?s? (sale|sales|total|collection)|aaj (ka|ki) (total|sale|bikri|kamai|collection)|current sale|आज की (बिक्री|कुल)|आज का (टोटल|कुल))\b", re.IGNORECASE)
# ---- Owner business intents (live run 4): spend, item-wise sales, demand, forecast. Matched before the stock intents
#      because "which item is of no demand" otherwise looks like a stock list, and "kitna item bika" like a stock query.
_SPEND = re.compile(r"\b(kharch|kharcha|spent|spend|spending|expense|expenses|invest(ed|ment|ments)?|paisa laga|paise lage|paisa lagaya|lagaya|lagaye|lagai|purchase total|purchases?|khareed|khareeda|kharida|maal (aaya|mangaya)|capital)\b|खर्च|खरीद|निवेश|पैसा लगा|लगाया", re.IGNORECASE)
_SUPPLIER = re.compile(r"\b(supplier|suppliers|vendor|vendors|distributor|wholesaler|purchase bills?|supplier ?wise|kis ?se (liya|aaya|mangaya))\b|सप्लायर|आपूर्तिकर्ता", re.IGNORECASE)
"""Spend questions that name the supplier side stay on the purchases report; everything else is the investment total."""
_FORECAST = re.compile(r"\b(forecast|predict(ion)?|future|upcoming|expected|anuman|agle|agla|aane ?wal[ae]|next (day|days|week|weeks|month|few)|growth|kitna bikega|bikegi|kya bikega)\b|पूर्वानुमान|अगले|आने वाल", re.IGNORECASE)
_DEMAND_HIGH = re.compile(r"\b(high demand|most demand|zyada demand|jyada demand|high in demand|in demand|best ?sell(er|ers|ing)|top ?sell(er|ers|ing)|most (sold|selling|popular)|sabse (zyada|jyada|adhik) (bikta|bikti|bikne|bika|sale)|fast ?moving|hot items?|popular items?|top items?)\b|ज़्यादा (माँग|मांग|बिकता|बिकने)|सबसे ज़्यादा|सबसे ज्यादा", re.IGNORECASE)
_DEMAND_NONE = re.compile(r"\b(no demand|low demand|less demand|kam demand|not (selling|sold|moving)|nahi bik(ta|ti|te|a|e|i)|bikta (hi )?nahi|unsold|dead stock|slow ?moving|worst ?sell(er|ers|ing)|least (sold|selling)|zero sale|no sale items?|koi demand nahi)\b|कम (माँग|मांग)|नहीं बिक|माँग नहीं|मांग नहीं", re.IGNORECASE)
_ITEM_SALES = re.compile(
    r"\b(har (ek )?item|har (ek )?cheez|each items?|every item|per item|item ?wise|itemwise|product ?wise|"
    r"kaun ?sa item .*\bbik(a|e|i|ri)|kitn[aei] (item|items|cheez|cheezein) .*\bbik(a|e|i|ri)|items? (ne )?bik(a|e|i|ay[ie])|bika hai|bike hain|bikri hui|"
    r"(items?|products?) (have been |were |got )?sold|sold items?|what (all )?(did we |have we |i |we )?(sell|sold)|sale (of|on|for) (each|every|all) (items?|products?)|kya kya bika)"
    r"|हर (एक )?(आइटम|वस्तु|चीज़)|कितने? (आइटम|वस्तु|चीज़ें?) बिक|क्या क्या बिका",
    re.IGNORECASE,
)
_SALE_WORD = re.compile(r"\b(sale|sales|bikri|bikree|kamai|collection|revenue|turnover|total|becha|bech[ae])\b|बिक्री|कमाई", re.IGNORECASE)
_PERIOD_WORDS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\b(so far|till (now|date|today)|until now|up to now|ab tak|abtak|ab ?talak|overall|all time|lifetime|since (the )?(start|beginning|opening)|shuru se|kul mila ?kar|grand total)\b|अब तक|शुरू से|कुल मिलाकर", re.IGNORECASE), "all_time"),
    (re.compile(r"\b(yesterday|kal (ki|ka|ko)? ?(sale|bikri|total|kamai)|beete kal|pichle din)\b|कल की|बीते कल", re.IGNORECASE), "yesterday"),
    (re.compile(r"\b(last week|pichle (hafte|week|saptah)|pichhle hafte|गत सप्ताह)\b|पिछले (हफ़्ते|हफ्ते|सप्ताह)", re.IGNORECASE), "last_week"),
    (re.compile(r"\b(this week|is (hafte|week|saptah)|iss hafte|weekly)\b|इस (हफ़्ते|हफ्ते|सप्ताह)", re.IGNORECASE), "this_week"),
    (re.compile(r"\b(last month|pichle (mahine|month|maheene)|pichhle mahine)\b|पिछले (महीने|माह)", re.IGNORECASE), "last_month"),
    (re.compile(r"\b(this month|is (mahine|month|maheene)|iss mahine|monthly|mahine (ka|ki|mein))\b|इस (महीने|माह)", re.IGNORECASE), "this_month"),
    (re.compile(r"\b(last 7 days|past week|pichle 7 din|7 din)\b", re.IGNORECASE), "last_7_days"),
    (re.compile(r"\b(last 30 days|past month|pichle 30 din|30 din)\b", re.IGNORECASE), "last_30_days"),
    (re.compile(r"\b(this year|is saal|iss saal|yearly|saal (ka|ki|bhar))\b|इस साल", re.IGNORECASE), "this_year"),
    (re.compile(r"\b(today|aaj|aj|pura din|poora din|pure din|din bhar|dinbhar)\b|आज|पूरा दिन|दिन भर", re.IGNORECASE), "today"),
)
_HORIZON = re.compile(r"\b(\d{1,2})\s*(din|days?|दिन)\b|\b(next|agle|agla|aane wale|coming)\s+(day|din|week|hafte|hafta|month|mahine|mahina|few days)\b|अगले\s+(दिन|हफ़्ते|हफ्ते|महीने)|अगला\s+(दिन|हफ़्ता|महीना)", re.IGNORECASE)
_COMPLEX = re.compile(r"\b(compare|comparison|compared|versus|vs|explain|why|kyun|kyon|kyu|than|mukable|mukabla|difference|diwali|holi|navratri|festival)\b|तुलना|क्यों|मुकाबले", re.IGNORECASE)
"""Comparisons, explanations and festival questions need the model (several tools, reasoning)."""


_PAST = re.compile(r"\b(bik[aei]|biki|thi|tha|the|hua|hui|hue|kiya|kiye|sold|was|were|did|bech[ae])\b|बिका|बिकी|बिके|थी|था|हुआ|हुई", re.IGNORECASE)
_BARE_KAL = re.compile(r"\bkal\b|कल", re.IGNORECASE)
_STOCK_NOUN = re.compile(r"\b(stock|inventory|left|bacha|bache|available|quantity|qty)\b|स्टॉक|बचा|बचे", re.IGNORECASE)


def period_of(text: str, default: str) -> str:
    """Period word in the question → a ``PERIODS`` key ("ab tak" → all_time, "kal ki sale" → yesterday, "aaj" → today).

    "kal" alone means yesterday *or* tomorrow; with a past-tense verb ("kal kitna bika") it is yesterday.
    """
    for pattern, period in _PERIOD_WORDS:
        if pattern.search(text):
            return period
    if _BARE_KAL.search(text) and _PAST.search(text):
        return "yesterday"
    return default


def horizon_of(text: str, default: int = 7) -> int:
    """Days ahead for a forecast question ("next week" → 7, "agle mahine" → 30, "10 din" → 10, "next day" → 1)."""
    m = _HORIZON.search(text)
    if not m:
        return default
    if m.group(1):
        return max(1, min(90, int(m.group(1))))
    unit = (m.group(4) or m.group(5) or m.group(6) or "").lower()
    if unit in ("day", "din", "दिन"):
        return 1
    if unit in ("month", "mahine", "mahina", "महीने", "महीना"):
        return 30
    if unit == "few days":
        return 3
    return 7
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
    if not t or len(t) > 200:
        return None
    if _GREETING.match(t):
        return FastIntent("greeting", "greeting")
    words = len(t.split())
    simple = words <= 6  # longer questions carry comparisons/conditions → the LLM
    business = words <= 16 and not _COMPLEX.search(t)  # the Owner's business questions are naturally longer ("maine shop mein kitna paisa ab tak kharch kiya hai")
    if business and _SPEND.search(t) and not _PRICE.search(t):
        if _SUPPLIER.search(t):
            return FastIntent("spend_supplier", "purchases_summary", owner_only=True, args={"period": period_of(t, "all_time")})
        return FastIntent("spend", "investment_summary", owner_only=True, args={"period": period_of(t, "all_time")})
    if business and _FORECAST.search(t) and not _LOW.search(t):
        return FastIntent("forecast", "forecast_sales", owner_only=True, args={"horizon_days": horizon_of(t)})
    if business and _DEMAND_NONE.search(t):
        return FastIntent("no_demand", "dead_stock", owner_only=True, args={"days": 30})
    if business and _DEMAND_HIGH.search(t):
        return FastIntent("high_demand", "demand_ranking", owner_only=True, args={"period": period_of(t, "last_30_days"), "order": "top"})
    if business and _ITEM_SALES.search(t):
        return FastIntent("item_sales", "demand_ranking", owner_only=True, args={"period": period_of(t, "today"), "order": "top", "limit": 15})
    if simple and _LOW.search(t):
        return FastIntent("low_stock", "low_stock_items")
    if simple and _OUT.search(t):
        return FastIntent("out_of_stock", "out_of_stock_items")
    if simple and _SALES.search(t):
        return FastIntent("sales_today", "sales_summary", owner_only=True, args={"period": "today"})
    if business and _SALE_WORD.search(t) and not _PRICE.search(t) and not _STOCK_NOUN.search(t):
        # "kal ki sale", "is mahine ka total", "how much did we sell this week"
        return FastIntent("sales_period", "sales_summary", owner_only=True, args={"period": period_of(t, "today")})
    if len(t.split()) <= 8 and _OVERVIEW.search(t):  # "kaun sa item or stock hai abhi" is seven words
        return FastIntent("stock_overview", "stock_overview")
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
        "did_you_mean": "\"{query}\" not found. Did you mean: {items}?",
        "overview": "{items_total} items in the catalogue · {in_stock} in stock · {low} low · {out} out. Most stock: {top}.",
        "overview_out": " Out of stock: {out_names}.",
        "price": "{name}: {price} per {unit} (MRP {mrp}).",
        "low_none": "No item is below its minimum right now.",
        "low": "{count} item(s) at or below minimum: {items}.",
        "out_none": "Nothing is out of stock right now.",
        "out": "Out of stock ({count}): {items}.",
        "sales": "Today {net} · {bills} bills · average {avg_bill} · cash {cash_pct}%. Source: sales_summary, {period}.",
        "sales_period": "Sales {period}: {net} net · {bills} bills · average {avg_bill} · cash {cash_pct}%.",
        "spend": "Supplier purchases ({period}): {total} across {purchases} purchase bill(s). By supplier: {items}. Only supplier bills booked under Stock → Purchase are counted; for the full investment ask \"how much have I invested so far\".",
        "spend_none": "No purchases are booked for {period}. Book supplier bills under Stock → Purchase and I can total them.",
        "invest": "Investment in stock ({period}{since}): {total} — opening stock {opening} + purchases {purchases} + corrections {corrections}, across {items_count} item(s). Stock on the shelf today is worth {stock_value_now} at cost. By category: {categories}.{estimated}",
        "invest_estimated": " {estimated_items} item(s) have no purchase price yet and are valued at selling price — set it under Products → Purchase price.",
        "invest_since": ", since {since}",
        "invest_none": "No stock has been entered for {period} — add opening stock or book a purchase under Stock, then I can total your investment.",
        "item_sales": "Item-wise sales {period} ({count} items): {items}.",
        "item_sales_none": "Nothing sold {period}.",
        "high_demand": "Most in demand {period}: {items}.",
        "no_demand": "No demand (in stock, nothing sold in {days} days) — {count} item(s) worth {value_total}: {items}.",
        "no_demand_none": "Every item in stock has sold at least once in the last {days} days.",
        "forecast": "Next {horizon} day(s): about {expected_units} units expected across {with_forecast} items ({without} items have too little history yet).{reorder}",
        "forecast_reorder": " Order soon: {items}.",
        "forecast_none": "No forecast yet — it needs about 4 weeks of sales history. Keep billing through the app and ask again.",
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
        "did_you_mean": "\"{query}\" नहीं मिला। क्या आपका मतलब था: {items}?",
        "overview": "कैटलॉग में {items_total} वस्तुएँ · {in_stock} स्टॉक में · {low} कम · {out} ख़त्म। सबसे ज़्यादा स्टॉक: {top}।",
        "overview_out": " ख़त्म: {out_names}।",
        "price": "{name}: {price} प्रति {unit} (MRP {mrp})।",
        "low_none": "अभी कोई वस्तु न्यूनतम से नीचे नहीं है।",
        "low": "{count} वस्तु(एँ) न्यूनतम पर या नीचे: {items}।",
        "out_none": "अभी कुछ भी ख़त्म नहीं है।",
        "out": "ख़त्म ({count}): {items}।",
        "sales": "आज {net} · {bills} बिल · औसत {avg_bill} · नकद {cash_pct}%। स्रोत: sales_summary, {period}।",
        "sales_period": "बिक्री {period}: {net} शुद्ध · {bills} बिल · औसत {avg_bill} · नकद {cash_pct}%।",
        "spend": "सप्लायर खरीद ({period}): {total}, {purchases} खरीद बिल। सप्लायर अनुसार: {items}। सिर्फ़ स्टॉक → खरीद में दर्ज सप्लायर बिल गिने जाते हैं; पूरे निवेश के लिए पूछें \"अब तक कितना निवेश किया\"।",
        "spend_none": "{period} की कोई खरीद दर्ज नहीं है। सप्लायर बिल स्टॉक → खरीद में दर्ज करें, फिर मैं जोड़ दूँगा।",
        "invest": "स्टॉक में निवेश ({period}{since}): {total} — शुरुआती स्टॉक {opening} + खरीद {purchases} + सुधार {corrections}, {items_count} वस्तुओं में। आज शेल्फ़ पर रखे स्टॉक का लागत मूल्य {stock_value_now} है। श्रेणी अनुसार: {categories}।{estimated}",
        "invest_estimated": " {estimated_items} वस्तु(ओं) की खरीद कीमत दर्ज नहीं है, इसलिए बिक्री कीमत पर गिनी गई — आइटम → खरीद कीमत में भरें।",
        "invest_since": ", {since} से",
        "invest_none": "{period} में कोई स्टॉक दर्ज नहीं हुआ — स्टॉक में शुरुआती स्टॉक या खरीद दर्ज करें, फिर मैं आपका निवेश जोड़ दूँगा।",
        "item_sales": "आइटम-वार बिक्री {period} ({count} वस्तुएँ): {items}।",
        "item_sales_none": "{period} कुछ नहीं बिका।",
        "high_demand": "सबसे ज़्यादा माँग {period}: {items}।",
        "no_demand": "कोई माँग नहीं ({days} दिन में स्टॉक में पर नहीं बिका) — {count} वस्तु(एँ), मूल्य {value_total}: {items}।",
        "no_demand_none": "स्टॉक की हर वस्तु पिछले {days} दिनों में कम से कम एक बार बिकी है।",
        "forecast": "अगले {horizon} दिन: लगभग {expected_units} इकाइयाँ, {with_forecast} वस्तुओं में ({without} वस्तुओं का इतिहास अभी कम है)।{reorder}",
        "forecast_reorder": " जल्दी मँगाएँ: {items}।",
        "forecast_none": "अभी पूर्वानुमान नहीं — लगभग 4 हफ़्ते की बिक्री चाहिए। ऐप से बिल बनाते रहें और फिर पूछें।",
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
        "did_you_mean": "\"{query}\" nahi mila. Kya aapka matlab tha: {items}?",
        "overview": "Catalogue mein {items_total} items · {in_stock} stock mein · {low} kam · {out} khatam. Sabse zyada stock: {top}.",
        "overview_out": " Khatam: {out_names}.",
        "price": "{name}: {price} per {unit} (MRP {mrp}).",
        "low_none": "Abhi koi item minimum se neeche nahi hai.",
        "low": "{count} item minimum par ya neeche: {items}.",
        "out_none": "Abhi kuch bhi khatam nahi hai.",
        "out": "Khatam ({count}): {items}.",
        "sales": "Aaj {net} · {bills} bills · average {avg_bill} · cash {cash_pct}%. Source: sales_summary, {period}.",
        "sales_period": "Sale {period}: {net} net · {bills} bills · average {avg_bill} · cash {cash_pct}%.",
        "spend": "Supplier purchase ({period}): {total}, {purchases} purchase bill. Supplier-wise: {items}. Sirf Stock → Purchase mein darj supplier bills gine jaate hain; poore investment ke liye poochhein \"ab tak kitna invest kiya\".",
        "spend_none": "{period} ki koi purchase darj nahi hai. Supplier bills Stock → Purchase mein darj karein, phir main total bata dunga.",
        "invest": "Stock mein investment ({period}{since}): {total} — opening stock {opening} + purchases {purchases} + corrections {corrections}, {items_count} items mein. Aaj shelf par rakhe stock ki cost value {stock_value_now} hai. Category-wise: {categories}.{estimated}",
        "invest_estimated": " {estimated_items} item ki purchase price darj nahi hai, isliye selling price par gini gayi — Products → Purchase price mein bharein.",
        "invest_since": ", {since} se",
        "invest_none": "{period} mein koi stock darj nahi hua — Stock mein opening stock ya purchase darj karein, phir main aapka investment jod dunga.",
        "item_sales": "Item-wise sale {period} ({count} items): {items}.",
        "item_sales_none": "{period} kuch nahi bika.",
        "high_demand": "Sabse zyada demand {period}: {items}.",
        "no_demand": "No demand ({days} din mein stock mein par nahi bika) — {count} item, value {value_total}: {items}.",
        "no_demand_none": "Stock ka har item pichle {days} din mein kam se kam ek baar bika hai.",
        "forecast": "Agle {horizon} din: lagbhag {expected_units} units, {with_forecast} items mein ({without} items ki history abhi kam hai).{reorder}",
        "forecast_reorder": " Jaldi mangayein: {items}.",
        "forecast_none": "Abhi forecast nahi — lagbhag 4 hafte ki sale history chahiye. App se bill banate rahein aur phir poochhein.",
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

    def not_found(query: str, source: str) -> FastAnswer:
        # Before giving up, look for near names (the owner typed "match box", the product is "Match stick").
        try:
            hits = list((run_tool("search_products", query=query, limit=3) or {}).get("items") or [])
        except Exception:  # noqa: BLE001 - suggestions are optional
            hits = []
        if hits:
            items = ", ".join(f"{h.get('name')} ({h.get('qty')})" for h in hits)
            return FastAnswer(tpl("did_you_mean").format(query=query, items=items), source, None, intent.name, tentative=True)
        return FastAnswer(tpl("not_found").format(query=query), source, None, intent.name, tentative=True)

    try:
        if intent.tool == "greeting":
            out = FastAnswer(tpl("greeting_owner" if role is Role.OWNER else "greeting"), "greeting", None, intent.name)
        elif intent.tool == "get_stock":
            r = run_tool("get_stock", query=intent.query)
            if not r.get("found"):
                out = not_found(str(intent.query), "get_stock")
            else:
                key = "stock_cat" if r.get("category") else "stock"
                out = FastAnswer(tpl(key).format(name=r.get("name"), qty=r.get("qty"), unit=r.get("unit", ""), min_stock=r.get("min_stock", "-"), category=r.get("category", "")), "get_stock", f"/stock?product={r.get('product_id')}" if r.get("product_id") else None, intent.name)
        elif intent.tool == "item_price":
            r = run_tool("item_price", query=intent.query)
            out = FastAnswer(tpl("price").format(name=r.get("name"), price=r.get("price"), unit=r.get("unit", ""), mrp=r.get("mrp", "-")), "item_price", None, intent.name) if r.get("found") else not_found(str(intent.query), "item_price")
        elif intent.tool == "stock_overview":
            r = run_tool("stock_overview", limit=8)
            top = ", ".join(f"{t.get('name')} ({t.get('qty')} {t.get('unit', '')})".replace(" )", ")") for t in (r.get("top") or [])) or "-"
            text = tpl("overview").format(items_total=r.get("items_total", 0), in_stock=r.get("in_stock", 0), low=r.get("low", 0), out=r.get("out", 0), top=top)
            if r.get("out_names"):
                text += tpl("overview_out").format(out_names=", ".join(r["out_names"]))
            out = FastAnswer(text, "stock_overview", "/stock", intent.name)
        elif intent.tool == "low_stock_items":
            r = run_tool("low_stock_items", limit=10)
            rows = list(r.get("items") or [])
            out = FastAnswer(tpl("low").format(count=r.get("count", len(rows)), items=_items(rows, with_qty=True)) if rows else tpl("low_none"), "low_stock_items", "/reports/low_stock", intent.name)
        elif intent.tool == "out_of_stock_items":
            r = run_tool("out_of_stock_items", limit=10)
            rows = list(r.get("items") or [])
            out = FastAnswer(tpl("out").format(count=r.get("count", len(rows)), items=_items(rows, with_qty=False)) if rows else tpl("out_none"), "out_of_stock_items", "/reports/low_stock", intent.name)
        elif intent.tool == "sales_summary":
            r = run_tool("sales_summary", period=intent.args.get("period", "today"))
            key = "sales" if intent.name == "sales_today" else "sales_period"
            out = FastAnswer(tpl(key).format(net=r.get("net"), bills=r.get("bills"), avg_bill=r.get("avg_bill"), cash_pct=r.get("cash_pct", 0), period=r.get("period", "")), "sales_summary", "/reports/sales", intent.name)
        elif intent.tool == "purchases_summary":
            r = run_tool("purchases_summary", period=intent.args.get("period", "all_time"))
            rows = list(r.get("items") or [])
            period = r.get("period", "")
            if rows:
                items = ", ".join(f"{x.get('supplier') or '-'} {x.get('total')}" for x in rows[:8]) + (" …" if len(rows) > 8 else "")
                out = FastAnswer(tpl("spend").format(period=period, total=r.get("total"), purchases=sum(int(x.get("purchases") or 0) for x in rows), items=items), "purchases_summary", "/reports/purchases", intent.name)
            else:
                out = FastAnswer(tpl("spend_none").format(period=period), "purchases_summary", "/stock/purchase", intent.name)
        elif intent.tool == "investment_summary":
            r = run_tool("investment_summary", period=intent.args.get("period", "all_time"), limit=6)
            period = r.get("period", "")
            if int(r.get("items_count") or 0) > 0:
                cats = ", ".join(f"{x.get('category') or '-'} {x.get('amount')}" for x in (r.get("categories") or [])[:6]) or "-"
                since = tpl("invest_since").format(since=r["since"]) if r.get("since") else ""
                estimated = tpl("invest_estimated").format(estimated_items=r.get("estimated_items")) if int(r.get("estimated_items") or 0) else ""
                out = FastAnswer(tpl("invest").format(period=period, since=since, total=r.get("total"), opening=r.get("opening_stock"), purchases=r.get("purchases"), corrections=r.get("corrections"), items_count=r.get("items_count"), stock_value_now=r.get("stock_value_now"), categories=cats, estimated=estimated), "investment_summary", "/reports/investment", intent.name)
            else:
                out = FastAnswer(tpl("invest_none").format(period=period), "investment_summary", "/stock", intent.name)
        elif intent.tool == "demand_ranking":
            r = run_tool("demand_ranking", period=intent.args.get("period", "today"), order=intent.args.get("order", "top"), limit=intent.args.get("limit", 10))
            rows = list(r.get("items") or [])
            period = r.get("period", "")
            items = ", ".join(f"{x.get('name')} × {x.get('qty')} = {x.get('amount')}" for x in rows[:15]) + (" …" if len(rows) > 15 else "")
            if intent.name == "high_demand":
                out = FastAnswer(tpl("high_demand").format(period=period, items=items) if rows else tpl("item_sales_none").format(period=period), "demand_ranking", "/reports/top", intent.name)
            else:
                out = FastAnswer(tpl("item_sales").format(period=period, count=r.get("count", len(rows)), items=items) if rows else tpl("item_sales_none").format(period=period), "demand_ranking", "/reports/top", intent.name)
        elif intent.tool == "dead_stock":
            r = run_tool("dead_stock", days=intent.args.get("days", 30), limit=15)
            rows = list(r.get("items") or [])
            items = ", ".join(f"{x.get('name')} ({x.get('qty')})" for x in rows) + (" …" if int(r.get("count", 0)) > len(rows) else "")
            out = FastAnswer(tpl("no_demand").format(days=r.get("days"), count=r.get("count", len(rows)), value_total=r.get("value_total"), items=items) if rows else tpl("no_demand_none").format(days=r.get("days")), "dead_stock", "/reports/dead", intent.name)
        elif intent.tool == "forecast_sales":
            h = intent.args.get("horizon_days", 7)
            r = run_tool("forecast_sales", horizon_days=h)
            if not r.get("items_with_forecast"):
                out = FastAnswer(tpl("forecast_none"), "forecast_sales", "/reports/forecast", intent.name)
            else:
                reorder = ""
                try:
                    plan = list((run_tool("reorder_plan", horizon_days=h, limit=5) or {}).get("items") or [])
                except Exception:  # noqa: BLE001 - optional detail
                    plan = []
                if plan:
                    reorder = tpl("forecast_reorder").format(items=", ".join(f"{x.get('name')} +{x.get('order_qty')}" for x in plan))
                out = FastAnswer(tpl("forecast").format(horizon=r.get("horizon_days", h), expected_units=r.get("expected_units"), with_forecast=r.get("items_with_forecast"), without=r.get("items_without_history", 0), reorder=reorder), "forecast_sales", "/reports/forecast", intent.name)
        elif intent.tool == "explain_screen":
            r = run_tool("explain_screen", query=intent.query)
            out = FastAnswer(tpl("howto").format(title=r.get("title"), steps=" → ".join(r.get("steps") or [])) if r.get("found") else tpl("howto_none"), "explain_screen", r.get("link"), intent.name)
        else:  # pragma: no cover - every tool above is enumerated
            return None
    except Exception as exc:  # noqa: BLE001 - the fast path must never raise into the widget
        log.error("AI_FASTPATH_FAILED intent=%s error=%s", intent.name, exc)
        return None
    log.info("AI_FASTPATH intent=%s tool=%s role=%s lang=%s tentative=%s", intent.name, intent.tool, role.value, language, out.tentative)
    return out
