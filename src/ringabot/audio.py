"""8 kHz S16 mono duplex over one snd-aloop substream (baresip <-> this process)."""

from __future__ import annotations

import asyncio
import shutil

SAMPLE_RATE = 8000
_used: set[int] = set()


class AudioUnavailable(RuntimeError):
    pass


class AlsaLoop:
    def __init__(self) -> None:
        for n in range(1, 8):
            if n not in _used:
                _used.add(n)
                self.substream = n
                break
        else:
            raise AudioUnavailable("alsa_substreams_full")
        self.baresip_device = f"hw:Loopback,0,{self.substream}"
        self._device = f"hw:Loopback,1,{self.substream}"
        self._capture: asyncio.subprocess.Process | None = None
        self._playback: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if shutil.which("arecord") is None or shutil.which("aplay") is None:
            raise AudioUnavailable("alsa-utils is not installed")
        self._capture = await asyncio.create_subprocess_exec(
            "arecord", *self._args(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await self._start_playback()

    def _args(self) -> list[str]:
        return ["-D", self._device, "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
                "-t", "raw", "--period-time=20000", "--buffer-time=80000"]  # fmt: skip

    async def _start_playback(self) -> None:
        self._playback = await asyncio.create_subprocess_exec(
            "aplay", *self._args(), stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip

    async def flush(self) -> None:
        """Drop queued speech (barge-in): restart aplay so its pipe buffer is discarded."""
        old, self._playback = self._playback, None
        if old is not None and old.returncode is None:
            old.kill()
            await old.wait()
        await self._start_playback()

    async def read(self) -> bytes | None:
        if self._capture is None or self._capture.stdout is None:
            return None
        return await self._capture.stdout.read(320) or None

    async def write(self, pcm: bytes) -> None:
        if self._playback is None or self._playback.stdin is None:
            return
        # No drain(): the websocket reader must never wait on playback, or speech_started
        # (barge-in) is read late. flush() discards whatever is still buffered.
        self._playback.stdin.write(pcm)

    async def stop(self) -> None:
        for proc in (self._capture, self._playback):
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    proc.kill()
        _used.discard(self.substream)
