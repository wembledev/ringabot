import os
import stat
from pathlib import Path

from ringabot.envfile import set_value

ROOT = Path(__file__).resolve().parents[1]


def test_set_value_replaces_and_keeps_0600(tmp_path):
    p = tmp_path / "x.env"
    p.write_text("A=1\nXAI_WEBHOOK_SECRET=\n")
    set_value(str(p), "XAI_WEBHOOK_SECRET", "new")
    assert p.read_text() == "A=1\nXAI_WEBHOOK_SECRET=new\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_caddy_exposes_only_public_routes():
    caddy = (ROOT / "deploy/Caddyfile").read_text()
    public = caddy.split("@app")[1]
    assert "@app path /xai/webhook /healthz /mcp\n" in caddy
    for path in ("/mcp",):
        assert path in public
    assert "/calls" not in public


def test_service_can_write_its_database():
    unit = (ROOT / "deploy/ringabot.service").read_text()
    assert "StateDirectory=ringabot" in unit
    assert "ProtectSystem=strict" in unit
