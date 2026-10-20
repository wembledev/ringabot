"""baresip for outbound calls: one process per call, controlled over ctrl_tcp.

Ubuntu 24.04 ships baresip 1.0.0. Its ctrl_tcp speaks netstring JSON. The SIP URI
user is the carrier sub-account; the DID is only the display name (Voip.ms returns
403 when the URI user is the DID). Audio goes through an snd-aloop PCM.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

MODULE_CANDIDATES = (
    "/usr/lib/baresip/modules",
    "/usr/lib/x86_64-linux-gnu/baresip/modules",
    "/usr/local/lib/baresip/modules",
)


class SipError(RuntimeError):
    pass


def safe_token(value: str, field: str) -> str:
    if not value or any(ch in value for ch in " \t\r\n;\"'<>"):
        raise SipError(f"unsafe_{field}")
    return value


def module_dir() -> str:
    for candidate in MODULE_CANDIDATES:
        if Path(candidate).is_dir():
            return candidate
    return MODULE_CANDIDATES[0]


def account_line(*, user: str, password: str, server: str, did: str) -> str:
    user = safe_token(user, "sip_user")
    display = f'"{safe_token(did, "did")}" ' if did else ""
    return (
        f"{display}<sip:{user}@{safe_token(server, 'sip_server')}>;auth_user={user};"
        f"auth_pass={safe_token(password, 'sip_password')};regint=0;answermode=manual"
    )


def config_text(ctrl_port: int, pcm: str) -> str:
    return "\n".join(
        [
            "sip_listen 0.0.0.0:0",
            f"module_path {module_dir()}",
            f"audio_player alsa,{pcm}",
            f"audio_source alsa,{pcm}",
            "audio_srate 8000",
            "audio_channels 1",
            "audio_buffer 20-160",
            f"ctrl_tcp_listen 127.0.0.1:{ctrl_port}",
            "module g711.so",
            "module alsa.so",
            "module account.so",
            "module ctrl_tcp.so",
            "module menu.so",
            "module uuid.so",
            "",
        ]
    )


def dial_uri(number: str, server: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"sip:{safe_token(digits, 'number')}@{safe_token(server, 'sip_server')}"


def encode_ctrl(command: str, params: str = "") -> bytes:
    body = json.dumps({"command": command, "params": params}, separators=(",", ":")).encode()
    return f"{len(body)}:".encode() + body + b","


def drain_frames(buf: bytearray) -> list[bytes]:
    """Pull complete netstrings (``len:json,``) out of buf; leave partial input."""
    frames: list[bytes] = []
    while buf:
        if buf[0] in b" \t\r\n":
            del buf[0]
            continue
        colon = buf.find(b":", 0, 10)
        if colon < 0:
            if len(buf) > 10:
                raise ValueError("bad_frame")
            break
        length_text = bytes(buf[:colon])
        if not length_text.isdigit():
            raise ValueError("bad_frame")
        length = int(length_text)
        if length > 65536:
            raise ValueError("frame_too_long")
        end = colon + 1 + length
        if len(buf) < end + 1:
            break
        if buf[end] != ord(","):
            raise ValueError("bad_netstring")
        frames.append(bytes(buf[colon + 1 : end]))
        del buf[: end + 1]
    return frames


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SipClient:
    """ctrl_tcp client. Events land on ``events`` as dicts; EOF on disconnect."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        reader, self._writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._task = asyncio.create_task(self._read(reader))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        buf = bytearray()
        try:
            while chunk := await reader.read(4096):
                buf.extend(chunk)
                for frame in drain_frames(buf):
                    try:
                        await self.events.put(json.loads(frame))
                    except json.JSONDecodeError:
                        continue
        except (ValueError, OSError):
            pass
        await self.events.put({"type": "EOF"})

    async def command(self, line: str) -> None:
        if self._writer is None:
            raise SipError("sip_not_connected")
        command, _, params = line.strip().partition(" ")
        self._writer.write(encode_ctrl(command, params))
        await self._writer.drain()

    async def wait_type(self, *types: str, timeout: float) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if event.get("type") in (*types, "EOF"):
                    return event

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._writer is not None:
            self._writer.close()


@dataclass
class Baresip:
    directory: Path
    port: int
    process: asyncio.subprocess.Process

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
        shutil.rmtree(self.directory, ignore_errors=True)


async def start_baresip(*, work_dir: Path, pcm: str, user: str, password: str, server: str,
                        did: str) -> Baresip:  # fmt: skip
    binary = shutil.which("baresip")
    if binary is None:
        raise SipError("baresip_not_installed")
    port = _free_port()
    directory = work_dir / f"baresip-{port}"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    _write_private(directory / "config", config_text(port, pcm))
    _write_private(
        directory / "accounts",
        account_line(user=user, password=password, server=server, did=did) + "\n",
    )
    # Output is discarded: the account file holds the SIP password.
    process = await asyncio.create_subprocess_exec(
        binary, "-f", str(directory),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while loop.time() < deadline:
        if process.returncode is not None:
            break
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        w.close()
        return Baresip(directory=directory, port=port, process=process)
    if process.returncode is None:
        process.kill()
    shutil.rmtree(directory, ignore_errors=True)
    raise SipError("baresip_start_failed")
