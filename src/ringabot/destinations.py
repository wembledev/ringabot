"""Who may be dialed. Any valid number, except what is always blocked.

Always blocked, even if allow-listed:
- emergency numbers (911, 112, 999, 000, 988, 110, 119) and N11 service codes (211 to 911);
- invalid numbers: not E.164, wrong NANP structure (N rules), unassigned ranges, short codes;
- NANP premium: area code 900, exchange 976, directory assistance (555-1212);
- international premium and costly ranges: +979 (ITU international premium rate service),
  +881 / +882 / +883 (mobile satellite and international networks), +870 to +874 (Inmarsat
  satellite), +878 (universal personal telecommunication);
- numbers that Google's libphonenumber metadata (built from each country's national numbering
  plan: Ofcom for the UK, Bundesnetzagentur for Germany, ARCEP for France, ACMA for Australia,
  and so on) classes as PREMIUM_RATE, SHARED_COST or PERSONAL_NUMBER, for example UK 09 and 070,
  Germany 0137, France 089, Spain 803, Italy 89x and 144, Australia 1900, Netherlands 0900,
  Belgium 090, Switzerland 090, Austria 0900, Ireland 15xx, Poland 70x, Sweden 0939, Japan 0990
  and Mexico 900;
- your own number, and anything in RINGABOT_BLOCKED_DESTINATIONS.

ALLOWED_DESTINATIONS, when set, is an extra restriction on top of this, never a way around it.
The metadata is only as good as the installed phonenumbers release; keep it updated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberType

EMERGENCY = frozenset({"911", "112", "999", "000", "988", "110", "119"})
N11 = frozenset(f"{d}11" for d in range(2, 10))
INTL_BLOCKED_PREFIXES = ("979", "881", "882", "883", "870", "871", "872", "873", "874", "878")
BLOCKED_TYPES = {
    PhoneNumberType.PREMIUM_RATE: "premium-rate number",
    PhoneNumberType.SHARED_COST: "shared-cost number",
    PhoneNumberType.PERSONAL_NUMBER: "personal-number range (often premium)",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    e164: str
    category: str
    reason: str


def _no(category: str, reason: str, e164: str = "") -> Decision:
    return Decision(False, e164, category, reason)


def _digits_form(raw: str) -> tuple[str, str]:
    """Return (digits with country code, problem). Accepts +CC..., 00CC..., 10/11-digit NANP."""
    text = re.sub(r"[\s().\-]", "", raw or "")
    if not re.fullmatch(r"\+?\d+", text):
        return "", "not a phone number"
    plus = text.startswith("+")
    digits = text.lstrip("+")
    if not plus and digits.startswith("00") and len(digits) > 4:
        return digits[2:], ""
    if plus:
        return digits, ""
    if len(digits) == 10:
        return "1" + digits, ""
    if len(digits) == 11 and digits.startswith("1"):
        return digits, ""
    return "", "include the country code with + (or use a 10-digit North American number)"


def check_destination(
    raw: str,
    allowed: list[str] | tuple[str, ...] = (),
    blocked: list[str] | tuple[str, ...] = (),
    own_did: str = "",
) -> Decision:
    bare = re.sub(r"[\s().\-+]", "", raw or "")
    if bare in EMERGENCY:
        return _no("emergency", f"{bare} is an emergency number and is never dialed")
    if bare in N11:
        return _no("n11", f"{bare} is an N11 service code and is never dialed")
    digits, problem = _digits_form(raw)
    if problem:
        return _no("invalid", problem)
    e164 = "+" + digits
    if digits in EMERGENCY or digits in N11:
        return _no("emergency", f"{digits} is an emergency or service code")
    if any(digits.startswith(p) for p in INTL_BLOCKED_PREFIXES):
        return _no("intl_premium", "international premium or satellite range is blocked", e164)
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return _no("invalid", "not a valid E.164 number", e164)
    if not phonenumbers.is_valid_number(parsed):
        return _no("invalid", "not a valid number for its country", e164)
    if digits.startswith("1") and len(digits) == 11:
        area, exchange, line = digits[1:4], digits[4:7], digits[7:]
        if area == "900":
            return _no("premium", "NANP 900 premium-rate number", e164)
        if exchange == "976":
            return _no("premium", "NANP 976 premium-rate exchange", e164)
        if exchange == "555" and line == "1212":
            return _no("premium", "directory assistance (555-1212) is a paid service", e164)
        if area in N11 or exchange in N11:
            return _no("n11", "N11 code inside a North American number", e164)
    kind = phonenumbers.number_type(parsed)
    if kind in BLOCKED_TYPES:
        return _no("premium", f"{BLOCKED_TYPES[kind]} is blocked", e164)
    own = re.sub(r"\D", "", own_did or "")
    if own and (digits == own or ("1" + own) == digits):
        return _no("self", "that is Ringabot's own number", e164)
    if any(e164.startswith(_prefix(p)) for p in blocked if p.strip()):
        return _no("blocked", "number is in RINGABOT_BLOCKED_DESTINATIONS", e164)
    if allowed and not any(e164.startswith(_prefix(p)) for p in allowed if p.strip()):
        return _no("not_allowed", "number not in ALLOWED_DESTINATIONS", e164)
    return Decision(True, e164, "ok", "allowed")


def _prefix(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits
