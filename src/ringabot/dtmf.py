"""send_dtmf: keypad digits for phone menus, sent through baresip.

What this stack can do: the outbound audio path is baresip (SIP to the carrier) plus a
WebSocket to xAI. xAI's voice API only reports DTMF received on its own SIP sessions and
documents no way to send it, and Ringabot's xAI leg is a WebSocket anyway. baresip can send
RFC 2833/4733 telephone-events with its `sndcode` command (checked in a local baresip to baresip
loopback: the far side saw CALL_DTMF_START for each digit). So digits are sent on the SIP side;
the carrier and the far-end phone system must accept RFC 2833 (Voip.ms does by default).
"""

from __future__ import annotations

import re

TOOL_NAME = "send_dtmf"
MAX_BURST = 6  # digits in one tool call
MAX_PER_LEVEL = 6  # digits between two prompts from the other side
MAX_PER_CALL = 40
_DIGITS = re.compile(r"[0-9*#]+")

DTMF_TOOL = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Press keypad digits on the phone call to move through an automated menu. Use it only "
        "after you have heard the options, with the digit(s) of the option that best fits the "
        "goal. Digits are 0-9, * and #, at most 6 at a time. After pressing, stop and listen "
        "for the next prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "description": "Keys to press, for example 2 or 1234#. Only 0-9, * and #.",
            }
        },
        "required": ["digits"],
    },
}

DTMF_INSTRUCTIONS = (
    "Phone menus: if an automated menu answers, listen to the whole menu first. Pick the "
    "option that best fits the goal of this call (for a general question, general inquiries or "
    "reception; otherwise the department you need) and press it with the send_dtmf tool, then "
    "stop and listen for the next prompt. Never press keys at random. If no option fits, "
    "press the one for an operator or reception once, or ask for a person by voice. Do not "
    "enter personal or payment details on the keypad unless the task says to. If the tool "
    "refuses more key presses, ask for a human or reception by voice, or say goodbye and "
    "end the call."
)

REFUSED_TOO_MANY = (
    "Too many key presses without a new prompt from the other side. Do not press more keys: "
    "ask for a human or for reception by voice, or say goodbye and end the call."
)


class DtmfError(ValueError):
    pass


def validate_digits(raw) -> str:
    digits = str(raw or "").strip().replace(" ", "")
    if not digits or not _DIGITS.fullmatch(digits):
        raise DtmfError("digits must be only 0-9, * and #")
    if len(digits) > MAX_BURST:
        raise DtmfError(f"send at most {MAX_BURST} digits at a time")
    return digits


class DtmfGuard:
    """Loop guard. A menu level ends when the other side speaks a new prompt."""

    def __init__(self) -> None:
        self.level = 0
        self.total = 0

    def new_prompt(self) -> None:
        self.level = 0

    def allow(self, digits: str) -> str | None:
        """None if these digits may be sent, else the error to give the agent."""
        if self.total + len(digits) > MAX_PER_CALL or self.level + len(digits) > MAX_PER_LEVEL:
            return REFUSED_TOO_MANY
        return None

    def record(self, digits: str) -> None:
        self.level += len(digits)
        self.total += len(digits)
