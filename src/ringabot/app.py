"""HTTP surface. Webhooks return in milliseconds; calls run as background tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ringabot.calls import CallManager
from ringabot.config import Settings
from ringabot.mcp import make_endpoint
from ringabot.plugins import Plugin, PluginContext, PluginError, load_plugins
from ringabot.signature import SignatureError, Verifier
from ringabot.store import Admin, Store

log = logging.getLogger("ringabot.app")
MAX_BODY = 64 * 1024
PURGE_INTERVAL_SECONDS = 6 * 3600


def _bearer_token(request: Request) -> str | None:
    scheme, _, got = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    got = got.strip()
    return got or None


def _bearer_ok(request: Request, token: str) -> bool:
    got = _bearer_token(request)
    if not token or got is None:
        return False
    return hmac.compare_digest(got.encode(), token.encode())


def create_app(
    settings: Settings | None = None,
    manager: CallManager | None = None,
    store: Store | None = None,
    plugins: list[Plugin] | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    manager = manager or CallManager(settings)
    store = store or Store(settings.db_path)
    manager.attach_store(store)
    if plugins is None:
        try:
            plugins = load_plugins(settings.plugins, PluginContext(settings, store, manager))
        except PluginError as exc:
            raise SystemExit(f"refusing to start: {exc}") from exc
    verifier = Verifier(settings.webhook_secret)

    def identify(request: Request):
        token = _bearer_token(request)
        if token is None:
            return None
        if settings.api_token and hmac.compare_digest(token.encode(), settings.api_token.encode()):
            return Admin()
        for plugin in plugins:
            if (who := plugin.identify(token)) is not None:
                return who
        return None

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def xai_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            verifier.verify(request.headers, body)
        except SignatureError as exc:
            log.warning("rejected webhook: %s", exc)
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        etype = event.get("type", "")
        if etype == "realtime.call.incoming":
            ok, why = manager.accept(event.get("data") or {})
            return JSONResponse({"accepted": ok, "detail": why})
        log.info("ignored webhook type %s", etype[:64])
        return JSONResponse({"ignored": etype[:64]})

    def _denied(request: Request) -> JSONResponse | None:
        if not _bearer_ok(request, settings.api_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def place_call(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad json"}, status_code=400)
        call_id, detail = manager.place(
            str(payload.get("number", "")),
            str(payload.get("briefing", "")),
            callback_url=str(payload.get("callback_url", "")),
            task=payload.get("task"),
        )
        if call_id is None:
            if hasattr(detail, "retry_after"):
                return JSONResponse(
                    {"error": str(detail), "retry_after_s": detail.retry_after},
                    status_code=429,
                    headers={"Retry-After": str(detail.retry_after)},
                )
            return JSONResponse({"error": detail}, status_code=400)
        return JSONResponse({"call_id": call_id, "status": detail}, status_code=202)

    async def list_calls(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        return JSONResponse({"calls": manager.all_calls("any")})

    async def hangup(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        call = manager.active.get(request.path_params["call_id"])
        if call is None:
            return JSONResponse({"error": "no such active call"}, status_code=404)
        manager.spawn(call.hangup("api"))
        return JSONResponse({"ok": True}, status_code=202)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def purge_loop():
            while True:
                if n := manager.purge_transcripts():
                    log.info("retention: purged transcripts of %d call(s)", n)
                await asyncio.sleep(PURGE_INTERVAL_SECONDS)

        task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            task.cancel()

    routes = [
        Route("/healthz", health),
        Route("/xai/webhook", xai_webhook, methods=["POST"]),
        Route("/call", place_call, methods=["POST"]),
        Route("/calls", list_calls),
        Route("/calls/{call_id}/hangup", hangup, methods=["POST"]),
        Route("/mcp", make_endpoint(manager, identify, store, plugins), methods=["POST", "GET"]),
    ]
    for plugin in plugins:
        routes.extend(plugin.routes())
    return Starlette(lifespan=lifespan, routes=routes)
