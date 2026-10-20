import base64

import pytest

from ringabot.config import Settings

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()


class FakeXai:
    def __init__(self):
        self.hangups: list[str] = []

    async def hangup(self, call_id):
        self.hangups.append(call_id)
        return 200


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(
        {
            "XAI_API_KEY": "test-key-placeholder",
            "XAI_WEBHOOK_SECRET": SECRET,
            "API_TOKEN": "admin-placeholder",
            "DID": "6045550100",
            "PUBLIC_URL": "https://ringabot.example",
            "SIP_SERVER": "pop.example.net",
            "SIP_USERNAME": "100000_test",
            "SIP_PASSWORD": "sip-placeholder",
            "ALLOWED_DESTINATIONS": "+16045550111",
            "IDLE_TIMEOUT": "30",
            "RINGABOT_DB": str(tmp_path / "ringabot.sqlite"),
        }
    )
