"""One phone call: a Doom process, a media transport, and the keypad-to-key policy."""
import asyncio
import logging
import os
import secrets
import time

from .doom import DoomProcess
from .rtp import RtpMedia
from .webrtc import WebRtcMedia

log = logging.getLogger("call")

# doomkeys.h codes
KEY = {"1": 0xA0, "2": 0xAD, "3": 0xA1, "4": 0xAC, "5": 0xA3, "6": 0xAE, "8": 0xAF, "*": 0xA2, "0": 0xB6, "#": 9}
LATCHABLE = set("12345680")


class Call:
    def __init__(self, on_end=lambda: None):
        self.id = secrets.token_hex(3)
        self.on_end = on_end
        self.doom = DoomProcess()
        self.media = None
        self._ended = False
        self.mode = os.environ.get("DOOM_MODE", "hold")
        self.weapon = 2
        self._down = {}
        self._latched = set()

    async def start(self, offer_sdp, local_ip=None):
        await self.doom.start()
        await asyncio.wait_for(self.doom.ready.wait(), 30)
        webrtc = "a=fingerprint" in offer_sdp
        self.media = (WebRtcMedia if webrtc else RtpMedia)(self.doom, self.dtmf, self.stop)
        answer = await self.media.start(offer_sdp, local_ip)
        log.info("[call %s] started (%s)", self.id, "webrtc" if webrtc else "rtp")
        return answer

    async def stop(self):
        if self._ended:
            return
        self._ended = True
        for h in self._down.values():
            if h:
                h.cancel()
        if self.media:
            await self.media.stop()
        await self.doom.stop()
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

    def dtmf(self, tone, first, end):
        """RFC 4733 event packet. The first packet presses, later packets keep a held key down."""
        if first:
            self._tone(tone, 160, 0)
        elif not end and self.mode == "hold" and tone in KEY:
            self._press(KEY[tone], 120)

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
