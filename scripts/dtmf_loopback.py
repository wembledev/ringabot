#!/usr/bin/env python3
"""Local check that baresip can send DTMF: two baresip processes call each other on loopback.

No carrier, no xAI, no phone number is involved. Run: python3 scripts/dtmf_loopback.py
Exit 0 and "OK" if the far side saw CALL_DTMF_START for each digit sent with `sndcode`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

MODULES = "/usr/lib/baresip/modules"
DIGITS = "12#"


def start(name: str, sip_port: int, ctrl_port: int, answer: str):
    # baresip also binds the TLS port next to the SIP port, so keep the ports apart.
    d = tempfile.mkdtemp(prefix=f"bs-{name}-")
    os.chmod(d, 0o700)
    lines = [
        f"sip_listen 127.0.0.1:{sip_port}", f"module_path {MODULES}",
        "audio_player alsa,null", "audio_source alsa,null", "audio_srate 8000",
        "audio_channels 1", f"ctrl_tcp_listen 127.0.0.1:{ctrl_port}", "module g711.so",
        "module alsa.so", "module account.so", "module ctrl_tcp.so", "module menu.so",
        "module uuid.so", "",
    ]  # fmt: skip
    with open(f"{d}/config", "w") as handle:
        handle.write("\n".join(lines))
    with open(f"{d}/accounts", "w") as handle:
        handle.write(f"<sip:{name}@127.0.0.1:{sip_port}>;regint=0;answermode={answer}\n")
    proc = subprocess.Popen(
        ["baresip", "-f", d], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    return d, proc


class Ctl:
    def __init__(self, port: int) -> None:
        self.port, self.buf, self.events = port, b"", []

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)

    def send(self, command: str, params: str = "") -> None:
        body = json.dumps({"command": command, "params": params}).encode()
        self.writer.write(f"{len(body)}:".encode() + body + b",")

    async def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.buf += await asyncio.wait_for(self.reader.read(65536), 0.2)
            except TimeoutError:
                continue
            while m := re.match(rb"(\d+):", self.buf):
                stop = m.end() + int(m.group(1))
                if len(self.buf) < stop + 1:
                    break
                self.events.append(json.loads(self.buf[m.end() : stop]))
                self.buf = self.buf[stop + 1 :]


async def main() -> int:
    if shutil.which("baresip") is None:
        print("baresip is not installed")
        return 2
    dir_a, proc_a = start("a", 5070, 47130, "manual")
    dir_b, proc_b = start("b", 5090, 47131, "auto")
    try:
        await asyncio.sleep(3)
        a, b = Ctl(47130), Ctl(47131)
        await a.connect()
        await b.connect()
        a.send("dial", "sip:b@127.0.0.1:5090")
        await asyncio.gather(a.pump(5), b.pump(5))
        a.send("sndcode", DIGITS)
        await asyncio.gather(a.pump(3), b.pump(3))
        seen = "".join(str(e.get("param")) for e in b.events if e.get("type") == "CALL_DTMF_START")
        print(f"sent {DIGITS!r}, far side saw {seen!r}")
        print("OK" if seen == DIGITS else "FAIL")
        return 0 if seen == DIGITS else 1
    finally:
        for proc in (proc_a, proc_b):
            proc.terminate()
        for d in (dir_a, dir_b):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
