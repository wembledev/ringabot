"""Destination policy (any valid number except the always-blocked set) and rate limits."""

import json
from dataclasses import replace

import pytest

from conftest import FakeXai
from ringabot.calls import RateLimited
from ringabot.config import Settings
from ringabot.destinations import check_destination
from ringabot.store import Store
from test_mcp import NoRunManager, _client, _tool

ORDINARY = [
    "+16045550123",
    "604-555-0123",
    "(212) 555-0123",
    "1 604 555 0123",
    "+12125550123",
    "+442079460555",
    "0044 20 7946 0555",
    "+61255501234",
    "+493023125555",
    "+33199005555",
]


@pytest.mark.parametrize("number", ORDINARY)
def test_ordinary_numbers_are_allowed_with_no_allow_list(number):
    d = check_destination(number)
    assert d.allowed and d.category == "ok" and d.e164.startswith("+")


def test_formats_normalise_to_e164():
    assert check_destination("604-555-0123").e164 == "+16045550123"
    assert check_destination("0044 20 7946 0555").e164 == "+442079460555"


@pytest.mark.parametrize("code", ["911", "112", "999", "000", "988", "110", "119", "9-1-1", "+911"])
def test_emergency_numbers_are_always_blocked(code):
    d = check_destination(code, allowed=["+9", "+1", "+"])
    assert not d.allowed and d.category == "emergency"


@pytest.mark.parametrize("code", ["211", "311", "411", "511", "611", "711", "811"])
def test_n11_service_codes_are_blocked(code):
    d = check_destination(code)
    assert not d.allowed and d.category == "n11"


PREMIUM = {
    "us 900": "+1 900 234 5678",
    "us 976 exchange": "+1 212 976 1234",
    "us directory assistance": "212-555-1212",
    "itu premium 979": "+979 1000 00000",
    "satellite 881": "+881 2345 6789",
    "network 882": "+882 1234 5678",
    "network 883": "+883 5100 0000 1",
    "inmarsat 870": "+870 7731 2345",
    "uk 09": "+44 9012 345678",
    "uk 070 personal": "+44 7012 345678",
    "uk 0871": "+44 8712 345678",
    "de 0137": "+49 137 1234567",
    "fr 089": "+33 8 91 23 45 67",
    "es 803": "+34 803 123 456",
    "it 899": "+39 899 123 456",
    "it 144": "+39 144 123 456",
    "au 1900": "+61 1900 123 456",
    "nz 0900": "+64 900 123 456",
    "nl 0900": "+31 900 1234",
    "be 0900": "+32 900 12345",
    "ch 0900": "+41 900 123 456",
    "at 0900": "+43 900 1234567",
    "ie 1550": "+353 1550 123456",
    "pl 70x": "+48 70 123 4567",
    "se 0939": "+46 939 1234",
    "jp 0990": "+81 990 123 456",
    "mx 900": "+52 900 123 4567",
}


@pytest.mark.parametrize("name", list(PREMIUM))
def test_premium_and_costly_ranges_are_blocked(name):
    d = check_destination(PREMIUM[name], allowed=["+"])
    assert not d.allowed, name
    assert d.category in ("premium", "intl_premium"), (name, d)


@pytest.mark.parametrize(
    "number",
    [
        "",
        "abc",
        "12345",
        "604-555-012",
        "+1 604 123 4567",
        "+1 604 555 01234",
        "+999 123 456 789",
        "442071838750",
        "+0 123456",
        "+1 (011) 555-0123",
    ],
)
def test_invalid_numbers_are_blocked(number):
    d = check_destination(number)
    assert not d.allowed and d.category in ("invalid", "n11"), (number, d)


def test_the_allow_list_only_narrows_and_never_overrides():
    mine = ["+1604"]
    assert check_destination("+16045550123", allowed=mine).allowed
    d = check_destination("+12125550123", allowed=mine)
    assert not d.allowed and d.category == "not_allowed" and "ALLOWED_DESTINATIONS" in d.reason
    assert not check_destination("+1 900 234 5678", allowed=["+1900"]).allowed
    assert not check_destination("911", allowed=["911"]).allowed
    assert check_destination("604 555 0123", allowed=["6045550123"]).allowed


def test_own_number_and_blocklist():
    assert check_destination("604-555-0100", own_did="6045550100").category == "self"
    d = check_destination("+442079460555", blocked=["+44207"])
    assert not d.allowed and d.category == "blocked"
    assert check_destination("+16045550123", blocked=["+44207"]).allowed


def test_settings_defaults_and_env():
    s = Settings.from_env({})
    assert s.allowed_destinations == [] and s.blocked_destinations == []
    assert (s.rate_limit_per_hour, s.rate_limit_per_day) == (10, 30)
    s = Settings.from_env(
        {
            "ALLOWED_DESTINATIONS": "+1604, +1778",
            "RINGABOT_BLOCKED_DESTINATIONS": "+44",
            "RINGABOT_RATE_LIMIT_PER_HOUR": "3",
            "RINGABOT_RATE_LIMIT_PER_DAY": "0",
        }
    )
    assert s.allowed_destinations == ["+1604", "+1778"] and s.blocked_destinations == ["+44"]
    assert (s.rate_limit_per_hour, s.rate_limit_per_day) == (3, 0)


def _open(settings):
    return replace(settings, allowed_destinations=[], rate_limit_per_hour=0, rate_limit_per_day=0)


def test_place_call_dials_any_valid_number_when_no_allow_list(settings):
    client, manager = _client(_open(settings))
    err, data = _tool(client, "place_call", {"number": "+442079460555", "briefing": "a"})
    assert not err and data["call_id"].startswith("out_")
    assert manager.get_record(data["call_id"])["to"] == "+442079460555"


@pytest.mark.parametrize(
    ("number", "fragment"),
    [
        ("911", "emergency"),
        ("411", "N11"),
        ("+1 900 234 5678", "900"),
        ("+979 1000 00000", "international premium"),
        ("+1 604 123 4567", "valid"),
    ],
)
def test_place_call_refuses_blocked_numbers_with_the_reason(settings, number, fragment):
    client, manager = _client(_open(settings))
    err, data = _tool(client, "place_call", {"number": number, "briefing": "a"})
    assert err and fragment in data["error"]
    assert not manager.active


def test_allow_list_still_restricts_place_call(settings):
    client, _ = _client(replace(_open(settings), allowed_destinations=["+1604"]))
    err, data = _tool(client, "place_call", {"number": "+12125550123", "briefing": "a"})
    assert err and "ALLOWED_DESTINATIONS" in data["error"]


# ---- rate limits


def test_rate_check_windows_and_retry_after(tmp_path):
    store = Store(str(tmp_path / "r.sqlite"))
    t0 = 1_000_000.0
    assert store.rate_check("acct", 2, 5, now=t0) == (True, 0, "")
    store.rate_record("acct", t0)
    store.rate_record("acct", t0 + 600)
    ok, retry, which = store.rate_check("acct", 2, 5, now=t0 + 700)
    assert not ok and which == "hour" and retry == 3600 - 700  # the first call ages out
    assert store.rate_check("acct", 2, 5, now=t0 + 3601)[0]  # an hour later: free again
    assert store.rate_check("other", 2, 5, now=t0 + 700)[0]  # per account


def test_daily_limit_and_zero_means_off(tmp_path):
    store = Store(str(tmp_path / "r.sqlite"))
    t0 = 2_000_000.0
    for i in range(3):
        store.rate_record("a", t0 + i * 4000)  # spaced so the hourly limit never trips
    ok, retry, which = store.rate_check("a", 10, 3, now=t0 + 3 * 4000)
    assert not ok and which == "day" and retry == 86400 - 3 * 4000
    assert store.rate_check("a", 0, 0, now=t0 + 3 * 4000)[0]


def test_rate_limit_is_persisted_across_restarts(tmp_path):
    path = str(tmp_path / "r.sqlite")
    Store(path).rate_record("a", 3_000_000.0)
    ok, _, _ = Store(path).rate_check("a", 1, 0, now=3_000_100.0)
    assert not ok


def test_place_call_rate_limit_error_has_retry_after(settings):
    s = replace(_open(settings), rate_limit_per_hour=2, max_concurrent_calls=10)
    client, manager = _client(s)
    for _ in range(2):
        err, _ = _tool(client, "place_call", {"number": "+12125550123", "briefing": "a"})
        assert not err
    err, data = _tool(client, "place_call", {"number": "+12125550123", "briefing": "a"})
    assert err and "rate limit: 2 calls per hour" in data["error"]
    assert 3590 <= data["retry_after_s"] <= 3600 and "retry after" in data["error"]
    assert len(manager.active) == 2  # the refused call never started


def test_rate_limits_are_per_account_and_invalid_calls_do_not_count(settings):
    s = replace(_open(settings), rate_limit_per_hour=1, max_concurrent_calls=10)
    manager = NoRunManager(s, xai=FakeXai())
    manager.attach_store(Store(s.db_path))
    assert manager.place("911", "a")[0] is None  # refused by policy: not counted
    assert manager.place("+12125550123", "a", account_id="acct-1")[0]
    limited = manager.place("+12125550123", "a", account_id="acct-1")
    assert limited[0] is None and isinstance(limited[1], RateLimited)
    assert manager.place("+12125550123", "a", account_id="acct-2")[0]  # another account
    assert manager.place("+12125550123", "a")[0]  # the admin bucket is separate again


def test_http_call_rate_limit_is_429_with_retry_after_header(settings):
    from starlette.testclient import TestClient

    from ringabot.app import create_app

    s = replace(_open(settings), rate_limit_per_hour=1, max_concurrent_calls=10)
    app = create_app(s, NoRunManager(s, xai=FakeXai()))
    client = TestClient(app)
    auth = {"authorization": "Bearer admin-placeholder"}
    body = {"number": "+12125550123", "briefing": "a"}
    assert client.post("/call", json=body, headers=auth).status_code == 202
    r = client.post("/call", json=body, headers=auth)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 3000
    assert json.loads(r.text)["retry_after_s"] == int(r.headers["retry-after"])
