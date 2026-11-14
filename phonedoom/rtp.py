"""Plain RTP media for ordinary SIP softphones (Linphone, Zoiper).

Out: Opus or PCMU audio and VP8 video, encoded with aiortc's codecs.
In: RFC 4733 telephone-events on the audio port. RTCP PLI and FIR force a keyframe.
"""
import asyncio
import fractions
import logging
import os
import random
import time
from dataclasses import dataclass, field

from aiortc.codecs import get_encoder
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtp import RtpPacket, is_rtcp

from .doom import RATE, SAMPLES

log = logging.getLogger("rtp")
PORT_MIN = int(os.environ.get("RTP_PORT_MIN", 10000))
PORT_MAX = int(os.environ.get("RTP_PORT_MAX", 20000))
IDLE_TIMEOUT = 60
TONES = "0123456789*#ABCD"
FPS = 30
KEYFRAME_EVERY = 2 * FPS


class UnsupportedMedia(Exception):
    pass


@dataclass
class Section:
    kind: str
    port: int
    proto: str
    ip: str
    pts: list
    maps: dict = field(default_factory=dict)  # payload type -> (name lower, clock rate)
    rtcp_mux: bool = False


def parse_offer(sdp):
    session_ip = ""
    sections = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith("c=") and not sections:
            session_ip = line.split()[-1]
        elif line.startswith("m="):
            kind, port, proto, *pts = line[2:].split()
            sections.append(Section(kind, int(port), proto, session_ip, [int(p) for p in pts if p.isdigit()]))
        elif sections and line.startswith("c="):
            sections[-1].ip = line.split()[-1]
        elif sections and line.startswith("a=rtpmap:"):
            pt, _, enc = line[9:].partition(" ")
            parts = enc.strip().split("/")
            sections[-1].maps[int(pt)] = (parts[0].lower(), int(parts[1]) if len(parts) > 1 else 8000)
        elif sections and line.strip() == "a=rtcp-mux":
            sections[-1].rtcp_mux = True
    return sections


def pick_audio(sec):
    by_name = {}
    for pt in sec.pts:
        name, rate = sec.maps.get(pt, ({0: "pcmu", 8: "pcma"}.get(pt, ""), 8000))
        by_name.setdefault(name, (pt, rate))
    dtmf = next((pt for pt, (n, r) in sec.maps.items() if n == "telephone-event" and r == 8000), None)
    if dtmf is None:
        dtmf = next((pt for pt, (n, r) in sec.maps.items() if n == "telephone-event"), None)
    if "opus" in by_name:
        pt, _ = by_name["opus"]
        return RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, channels=2, payloadType=pt), 960, dtmf
    if "pcmu" in by_name:
        return RTCRtpCodecParameters(mimeType="audio/PCMU", clockRate=8000, channels=1, payloadType=by_name["pcmu"][0]), 160, dtmf
    raise UnsupportedMedia("no Opus or PCMU audio offered")


def pick_video(sec):
    for pt, (name, rate) in sec.maps.items():
        if name == "vp8" and pt in sec.pts:
            return RTCRtpCodecParameters(mimeType="video/VP8", clockRate=90000, payloadType=pt)
    return None


class Port(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.handler(self, data, addr)


@dataclass
class Stream:
    kind: str
    rtp: Port
    rtcp: Port | None
    remote: tuple
    codec: RTCRtpCodecParameters
    ssrc: int = field(default_factory=lambda: random.getrandbits(32))
    seq: int = field(default_factory=lambda: random.getrandbits(16))
    task: asyncio.Task | None = None

    def send(self, payload, ts, marker):
        if not self.remote[1]:
            return
        pkt = RtpPacket(self.codec.payloadType, marker, self.seq, ts & 0xFFFFFFFF, self.ssrc, payload)
        self.seq = (self.seq + 1) & 0xFFFF
        self.rtp.transport.sendto(pkt.serialize(), self.remote)


class RtpMedia:
    def __init__(self, doom, on_dtmf, on_dead):
        self.doom = doom
        self.on_dtmf = on_dtmf
        self.on_dead = on_dead
        self.streams = {}
        self._ports = []
        self._dtmf_pts = set()
        self._current = None
        self._last_rx = time.monotonic()
        self._keyframe = True
        self._tasks = []

    async def _bind(self, want_rtcp):
        loop = asyncio.get_running_loop()
        for _ in range(200):
            port = random.randrange(PORT_MIN, PORT_MAX, 2)
            opened = []
            try:
                for p in (port, port + 1) if want_rtcp else (port,):
                    _, proto = await loop.create_datagram_endpoint(lambda: Port(self._on_packet), local_addr=("0.0.0.0", p))
                    opened.append(proto)
                self._ports += opened
                return port, opened[0], opened[1] if want_rtcp else None
            except OSError:
                for proto in opened:
                    proto.transport.close()
        raise OSError("no free RTP port")

    async def start(self, offer_sdp, local_ip):
        sections = parse_offer(offer_sdp)
        if not any(s.kind == "audio" for s in sections):
            raise UnsupportedMedia("no audio offered")
        lines = ["v=0", f"o=doom {int(time.time())} 1 IN IP4 {local_ip}", "s=phonedoom", f"c=IN IP4 {local_ip}", "t=0 0"]
        spp = 160
        for sec in sections:
            codec = dtmf = None
            plain = sec.proto in ("RTP/AVP", "RTP/AVPF")
            if sec.kind == "audio" and sec.port and "audio" not in self.streams:
                if not plain:
                    raise UnsupportedMedia(f"{sec.proto} is not supported, turn media encryption off")
                codec, spp, dtmf = pick_audio(sec)
            elif sec.kind == "video" and sec.port and plain and "video" not in self.streams:
                codec = pick_video(sec)
            if codec is None:
                lines.append(f"m={sec.kind} 0 {sec.proto} {sec.pts[0] if sec.pts else 0}")
                continue
            port, rtp, rtcp = await self._bind(not sec.rtcp_mux)
            stream = Stream(sec.kind, rtp, rtcp, (sec.ip, sec.port), codec)
            self.streams[sec.kind] = stream
            fmts = [codec.payloadType] + ([dtmf] if dtmf is not None else [])
            lines += [f"m={sec.kind} {port} RTP/AVP {' '.join(map(str, fmts))}",
                      f"a=rtpmap:{codec.payloadType} {codec.mimeType.split('/')[1]}/{codec.clockRate}" + (f"/{codec.channels}" if (codec.channels or 1) > 1 else "")]
            if dtmf is not None:
                self._dtmf_pts.add(dtmf)
                lines += [f"a=rtpmap:{dtmf} telephone-event/{sec.maps[dtmf][1]}", f"a=fmtp:{dtmf} 0-15"]
            if sec.rtcp_mux:
                lines.append("a=rtcp-mux")
            lines.append("a=ptime:20" if sec.kind == "audio" else "a=sendonly")
        for kind, stream in self.streams.items():
            enc = get_encoder(stream.codec)
            run = self._audio_loop if kind == "audio" else self._video_loop
            args = (stream, enc, spp) if kind == "audio" else (stream, enc)
            self._tasks.append(asyncio.create_task(run(*args)))
        self._tasks.append(asyncio.create_task(self._watchdog()))
        log.info("rtp media %s", {k: (s.codec.mimeType, s.remote) for k, s in self.streams.items()})
        return "\r\n".join(lines) + "\r\n"

    def _on_packet(self, port, data, addr):
        self._last_rx = time.monotonic()
        if len(data) < 8:
            return
        if is_rtcp(data):
            self._rtcp(data)
            return
        stream = next((s for s in self.streams.values() if s.rtp is port), None)
        if stream is None:
            return
        if stream.remote != addr:
            stream.remote = addr  # symmetric RTP, follows the phone through NAT
        if stream.kind != "audio":
            return
        try:
            pkt = RtpPacket.parse(data)
        except Exception:
            return
        if pkt.payload_type in self._dtmf_pts and len(pkt.payload) >= 4:
            event, flags = pkt.payload[0], pkt.payload[1]
            if event >= len(TONES):
                return
            first = self._current != (pkt.timestamp, event)
            self._current = (pkt.timestamp, event)
            self.on_dtmf(TONES[event], first, bool(flags & 0x80))

    def _rtcp(self, data):
        pos = 0
        while pos + 4 <= len(data):
            fmt, pt = data[pos] & 0x1F, data[pos + 1]
            if pt == 206 and fmt in (1, 4):
                self._keyframe = True
            pos += 4 * (int.from_bytes(data[pos + 2:pos + 4], "big") + 1)

    async def _audio_loop(self, stream, enc, spp):
        start, n, ts = time.monotonic(), 0, 0
        while True:
            frame = self.doom.audio_frame()
            frame.pts, frame.time_base = n * SAMPLES, fractions.Fraction(1, RATE)
            payloads, _ = enc.encode(frame)
            for payload in payloads:
                stream.send(payload, ts, int(ts == 0))
                ts += spp
            n += 1
            await asyncio.sleep(max(0, start + n * 0.02 - time.monotonic()))

    async def _video_loop(self, stream, enc):
        start, n = time.monotonic(), 0
        await self.doom.ready.wait()
        while True:
            frame = self.doom.video_frame()
            frame.pts, frame.time_base = n * 90000 // FPS, fractions.Fraction(1, 90000)
            force = self._keyframe or n % KEYFRAME_EVERY == 0
            self._keyframe = False
            payloads, _ = enc.encode(frame, force)
            for i, payload in enumerate(payloads):
                stream.send(payload, frame.pts, int(i == len(payloads) - 1))
            n += 1
            await asyncio.sleep(max(0, start + n / FPS - time.monotonic()))

    async def _watchdog(self):
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self._last_rx > IDLE_TIMEOUT:
                log.info("no RTP for %ss, ending call", IDLE_TIMEOUT)
                await self.on_dead()
                return

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for p in self._ports:
            p.transport.close()
