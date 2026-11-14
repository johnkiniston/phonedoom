"""One phone call: a native Doom process whose frames and audio feed aiortc tracks."""
import asyncio
import fractions
import glob
import logging
import os
import secrets
import time
from pathlib import Path

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole

log = logging.getLogger("call")
ROOT = Path(__file__).resolve().parent.parent
DOOM_BIN = Path(os.environ.get("DOOM_BIN", ROOT / "native/doomgeneric/doomgeneric/doomphone"))
WAD = Path(os.environ.get("DOOM_WAD", ROOT / "native/doom1.wad"))
RUN_DIR = Path(os.environ.get("DOOM_RUN_DIR", ROOT / "native/run"))
W, H = 320, 200
FRAME_BYTES = W * H * 4
RATE, SAMPLES = 44100, 882  # 20 ms of Doom's 44.1 kHz stereo audio
CHUNK = SAMPLES * 4

# doomkeys.h codes
KEY = {"1": 0xA0, "2": 0xAD, "3": 0xA1, "4": 0xAC, "5": 0xA3, "6": 0xAE, "8": 0xAF, "*": 0xA2, "0": 0xB6, "#": 9}
LATCHABLE = set("12345680")


def find_soundfont():
    env = os.environ.get("SF2")
    if env:
        return env
    patterns = ["/usr/share/sounds/sf2/*.sf2", "/usr/share/soundfonts/*.sf2", "/opt/homebrew/Cellar/fluid-synth/*/share/fluid-synth/sf2/*.sf2"]
    for p in patterns:
        found = sorted(glob.glob(p))
        if found:
            return found[0]
    return ""


class DoomProcess:
    def __init__(self):
        self.latest = None
        self.ready = asyncio.Event()
        self.audio = bytearray()
        self.proc = None
        self._tasks = []

    async def start(self):
        vr, vw = os.pipe()
        ar, aw = os.pipe()
        env = {**os.environ, "SDL_AUDIODRIVER": "disk", "SDL_DISKAUDIOFILE": f"/dev/fd/{aw}",
               "SDL_SOUNDFONTS": find_soundfont(), "DOOM_VIDEO_FD": str(vw)}
        self.proc = await asyncio.create_subprocess_exec(
            str(DOOM_BIN), "-iwad", str(WAD), "-skill", "3", "-warp", "1", "1",
            cwd=RUN_DIR, env=env, pass_fds=(vw, aw),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        os.close(vw)
        os.close(aw)
        loop = asyncio.get_running_loop()
        self._vreader = asyncio.StreamReader(limit=FRAME_BYTES * 2)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self._vreader), os.fdopen(vr, "rb", 0))
        self._areader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self._areader), os.fdopen(ar, "rb", 0))
        self._tasks = [asyncio.create_task(self._pump_video()), asyncio.create_task(self._pump_audio())]

    async def _pump_video(self):
        try:
            while True:
                self.latest = await self._vreader.readexactly(FRAME_BYTES)
                self.ready.set()
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass

    async def _pump_audio(self):
        cap = RATE * 4 // 4  # keep at most ~250 ms so latency stays low
        try:
            while True:
                chunk = await self._areader.read(65536)
                if not chunk:
                    return
                self.audio += chunk
                if len(self.audio) > cap:
                    del self.audio[: len(self.audio) - cap]
        except asyncio.CancelledError:
            pass

    def take_audio(self):
        if len(self.audio) < CHUNK:
            return bytes(CHUNK)
        out = bytes(self.audio[:CHUNK])
        del self.audio[:CHUNK]
        return out

    def key(self, code, pressed):
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.write(bytes([1 if pressed else 0, code]))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


class DoomVideo(VideoStreamTrack):
    def __init__(self, doom):
        super().__init__()
        self.doom = doom

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        data = self.doom.latest or bytes(FRAME_BYTES)
        frame = av.VideoFrame.from_ndarray(np.frombuffer(data, np.uint8).reshape(H, W, 4), format="bgra")
        frame.pts, frame.time_base = pts, time_base
        return frame


class DoomAudio(MediaStreamTrack):
    kind = "audio"

    def __init__(self, doom):
        super().__init__()
        self.doom = doom
        self._start = None
        self._ts = 0

    async def recv(self):
        if self._start is None:
            self._start = time.time()
        else:
            self._ts += SAMPLES
            wait = self._start + self._ts / RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        pcm = np.frombuffer(self.doom.take_audio(), np.int16).astype(np.int32)
        pcm = (pcm // 2).astype(np.int16).reshape(1, -1)  # Doom output clips at full scale
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
        frame.sample_rate = RATE
        frame.pts = self._ts
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


class Call:
    def __init__(self, on_end=lambda: None):
        self.id = secrets.token_hex(3)
        self.on_end = on_end
        self.doom = DoomProcess()
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._blackhole = MediaBlackhole()
        self._ended = False
        self.mode = "hold"
        self.weapon = 2
        self._down = {}
        self._latched = set()

    async def start(self, offer_sdp):
        await self.doom.start()
        await asyncio.wait_for(self.doom.ready.wait(), 30)
        pc = self.pc

        @pc.on("track")
        def on_track(track):
            self._blackhole.addTrack(track)

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("[call %s] pc %s", self.id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self.stop()

        await pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        pc.addTrack(DoomVideo(self.doom))
        pc.addTrack(DoomAudio(self.doom))
        await self._blackhole.start()
        await pc.setLocalDescription(await pc.createAnswer())
        log.info("[call %s] started", self.id)
        return pc.localDescription.sdp

    async def stop(self):
        if self._ended:
            return
        self._ended = True
        for h in self._down.values():
            if h:
                h.cancel()
        await self._blackhole.stop()
        await self.doom.stop()
        await self.pc.close()
        log.info("[call %s] ended", self.id)
        self.on_end()

    # ---- DTMF -> keys ----
    def _press(self, code, ms):
        loop = asyncio.get_running_loop()
        if code not in self._down:
            self.doom.key(code, True)
        old = self._down.get(code)
        if old:
            old.cancel()
        self._down[code] = None if ms is None else loop.call_later(ms / 1000, self._release, code)

    def _release(self, code):
        if code not in self._down:
            return
        h = self._down.pop(code)
        if h:
            h.cancel()
        self.doom.key(code, False)

    def _release_all(self):
        for c in list(self._down):
            self._release(c)
        self._latched.clear()

    def _tone(self, tone, dur, sent):
        lag = f"{int(time.time() * 1000) - sent}ms" if sent else "?"
        log.debug("[call %s] tone %s dur=%s mode=%s lag=%s", self.id, tone, dur, self.mode, lag)
        if tone in ("7", "9"):
            self.weapon = self.weapon % 7 + 1 if tone == "9" else (self.weapon + 5) % 7 + 1
            code = 0x30 + self.weapon
            self.doom.key(code, True)
            asyncio.get_running_loop().call_later(0.06, self.doom.key, code, False)
            return
        code = KEY.get(tone)
        if code is None:
            return
        if self.mode == "latch" and tone in LATCHABLE:
            if tone in self._latched:
                self._latched.discard(tone)
                self._release(code)
            else:
                self._latched.add(tone)
                self._press(code, None)
        else:
            self._press(code, min(max(dur, 60), 1000))

    def info(self, content_type, body):
        if "x-doom-mode" in content_type:
            self.mode = body.strip()
            self._release_all()
            log.info("[call %s] mode = %s", self.id, self.mode)
            return
        fields = {}
        for line in body.splitlines():
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
        try:
            self._tone(fields.get("Signal", ""), int(fields.get("Duration", 160)), int(fields.get("Sent", 0)))
        except ValueError:
            pass
