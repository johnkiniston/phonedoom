"""The native Doom process: frames and audio come out of pipes, key events go in on stdin."""
import asyncio
import glob
import os
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DOOM_BIN = Path(os.environ.get("DOOM_BIN", ROOT / "native/doomgeneric/doomgeneric/doomphone"))
WAD = Path(os.environ.get("DOOM_WAD", ROOT / "native/doom1.wad"))
RUN_DIR = Path(os.environ.get("DOOM_RUN_DIR", ROOT / "native/run"))
W, H = 320, 200
FRAME_BYTES = W * H * 4
RATE, SAMPLES = 44100, 882  # 20 ms of Doom's 44.1 kHz stereo audio
CHUNK = SAMPLES * 4


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

    def video_frame(self):
        data = self.latest or bytes(FRAME_BYTES)
        return av.VideoFrame.from_ndarray(np.frombuffer(data, np.uint8).reshape(H, W, 4), format="bgra")

    def audio_frame(self):
        pcm = np.frombuffer(self.take_audio(), np.int16).astype(np.int32)
        pcm = (pcm // 2).astype(np.int16).reshape(1, -1)  # Doom output clips at full scale
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
        frame.sample_rate = RATE
        return frame
