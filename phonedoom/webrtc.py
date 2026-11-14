"""WebRTC media for the browser caller: aiortc tracks fed from the Doom process."""
import asyncio
import fractions
import logging
import time

from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole

from .doom import RATE, SAMPLES

log = logging.getLogger("webrtc")


class DoomVideo(VideoStreamTrack):
    def __init__(self, doom):
        super().__init__()
        self.doom = doom

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = self.doom.video_frame()
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
        frame = self.doom.audio_frame()
        frame.pts = self._ts
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


class WebRtcMedia:
    def __init__(self, doom, on_dtmf, on_dead, on_audio=None):
        self.doom = doom
        self.on_audio = on_audio
        self.on_dead = on_dead
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._blackhole = MediaBlackhole()
        self._listener = None

    async def start(self, offer_sdp, local_ip=None):
        pc = self.pc

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio" and self.on_audio:
                self._listener = asyncio.create_task(self._listen(track))
            else:
                self._blackhole.addTrack(track)

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("pc %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self.on_dead()

        await pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        pc.addTrack(DoomVideo(self.doom))
        pc.addTrack(DoomAudio(self.doom))
        await self._blackhole.start()
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp

    async def _listen(self, track):
        try:
            while True:
                self.on_audio(await track.recv())
        except Exception:
            pass

    async def negotiate(self, offer_sdp, local_ip=None):
        raise ValueError("re-INVITE is not supported on WebRTC calls")

    async def stop(self):
        if self._listener:
            self._listener.cancel()
        await self._blackhole.stop()
        await self.pc.close()
