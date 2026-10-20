"""ringabot serve | doctor | register-number"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ringabot.calls import e164
from ringabot.config import Settings
from ringabot.envfile import set_value
from ringabot.signature import SignatureError, Verifier
from ringabot.xai import XaiClient


def _import_call(s: Settings, path: str) -> int:
    import json

    from ringabot.store import Store

    data = json.load(open(path, encoding="utf-8"))
    record = data.get("record", data)
    if "call_id" not in record:
        print("no call_id in file")
        return 1
    record.setdefault("direction", "outbound")
    Store(s.db_path).save_call(record)
    print(f"imported {record['call_id']} into {s.db_path}")
    return 0


async def _reanalyze(s: Settings, call_id: str) -> int:
    import json

    from ringabot.postcall import call_result, finalize
    from ringabot.store import Store

    store = Store(s.db_path)
    record = store.load_call(call_id)
    if record is None:
        print("no such call")
        return 1
    await finalize(s, record, callback=False)
    store.save_call(record)
    print(json.dumps(call_result(record), indent=1))
    return 0


async def _doctor(s: Settings) -> int:
    problems = 0

    def line(ok: bool, msg: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(("ok   " if ok else "FAIL ") + msg)

    line(bool(s.xai_api_key), "XAI_API_KEY set")
    try:
        Verifier(s.webhook_secret)
        line(True, "XAI_WEBHOOK_SECRET set and well formed")
    except SignatureError as exc:
        line(False, f"XAI_WEBHOOK_SECRET: {exc}")
    line(len(s.api_token) >= 32, "API_TOKEN set, 32+ chars (admin + public /mcp)")
    line(bool(s.did), "DID set")
    line(s.public_url.startswith("https://"), "PUBLIC_URL is https")
    line(s.host in ("127.0.0.1", "::1", "localhost"), f"listener bound to loopback ({s.host})")
    line(s.outbound_ready, "outbound SIP settings present (SIP_SERVER, SIP_USERNAME, SIP_PASSWORD)")
    if s.outbound_ready:
        import shutil
        from pathlib import Path

        line(shutil.which("baresip") is not None, "baresip installed")
        line(Path("/proc/asound/Loopback").exists(), "snd-aloop loaded")
        scope = (
            f"restricted to {len(s.allowed_destinations)} prefix(es)"
            if s.allowed_destinations
            else "any valid number except emergency, N11, premium and invalid"
        )
        line(True, f"destination policy: {scope}")
    if s.xai_api_key:
        try:
            numbers = await XaiClient(s.xai_api_key).list_numbers()
            line(True, "xAI key accepted")
            mine = [n for n in numbers if n.get("phone_number") == e164(s.did)] if s.did else []
            line(bool(mine), "DID registered with xAI as Direct SIP")
            if mine:
                line(bool(mine[0].get("webhook_id")), "DID dispatches to a webhook")
        except Exception as exc:  # noqa: BLE001
            line(False, f"xAI API check: {type(exc).__name__}")
    print("doctor: clean" if problems == 0 else f"doctor: {problems} problem(s)")
    return 0 if problems == 0 else 1


async def _register(s: Settings, env_file: str, name: str) -> int:
    if not (s.xai_api_key and s.did and s.webhook_url.startswith("https://")):
        print("need XAI_API_KEY, DID and an https PUBLIC_URL", file=sys.stderr)
        return 2
    client = XaiClient(s.xai_api_key)
    number = e164(s.did)
    existing = [n for n in await client.list_numbers() if n.get("phone_number") == number]
    if existing:
        print(
            f"{number} is already registered ({existing[0].get('phone_number_id')}); not changing"
        )
        return 1
    body: dict = {
        "origin": "byo_trunk",
        "name": name,
        "phone_number": number,
        "webhook": {"name": name, "url": s.webhook_url},
    }
    if s.sip_allowed_cidrs:
        body["sip_auth"] = {"allowed_addresses": s.sip_allowed_cidrs}
    resp = await client.create_number(body)
    secret = (resp.get("webhook") or {}).get("dispatch_signing_secret", "")
    if secret:
        set_value(env_file, "XAI_WEBHOOK_SECRET", secret)
    pn = resp.get("phone_number") or {}
    print(f"registered {number} as {pn.get('phone_number_id')} -> {s.webhook_url}")
    print("signing secret saved to env file" if secret else "WARNING: no signing secret returned")
    print(f"route your carrier to sip:{number}@{pn.get('sip_host', 'sip.voice.x.ai')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ringabot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("doctor")
    imp = sub.add_parser("import-call", help="load an exported call record into sqlite")
    imp.add_argument("file")
    rea = sub.add_parser("reanalyze", help="rerun the post-call analysis on a stored call")
    rea.add_argument("call_id")
    reg = sub.add_parser("register-number", help="register DID with xAI Direct SIP + webhook")
    reg.add_argument("--env-file", default="/etc/ringabot/ringabot.env")
    reg.add_argument("--name", default="ringabot")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    s = Settings.from_env()
    if args.cmd == "doctor":
        return asyncio.run(_doctor(s))
    if args.cmd == "import-call":
        return _import_call(s, args.file)
    if args.cmd == "reanalyze":
        return asyncio.run(_reanalyze(s, args.call_id))
    if args.cmd == "register-number":
        return asyncio.run(_register(s, args.env_file, args.name))
    import uvicorn

    from ringabot.app import create_app

    uvicorn.run(create_app(s), host=s.host, port=s.port, access_log=False, log_level="info")
    return 0
