"""Headless plain-SIP softphone, the way Linphone or Zoiper calls in.

Sends an INVITE over UDP or TCP with a plain RTP offer, decodes the audio and VP8 video it gets back,
sends RFC 4733 digits, and checks the picture changes.
"""
import argparse
import asyncio
import random
import secrets
import socket
import struct
import time

import numpy as np
from aiortc.codecs.g711 import PcmuDecoder
from aiortc.codecs.opus import OpusDecoder
from aiortc.codecs.vpx import Vp8Decoder, vp8_depayload
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtp import RtpPacket


class Media(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.packets.append((time.monotonic(), data, addr))


def parse(text):
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), v.strip())
    return lines[0], headers, body


def answer_ports(sdp):
    out, kind = {}, None
    for line in sdp.splitlines():
        if line.startswith("m="):
            kind, port = line[2:].split()[:2]
            out[kind] = int(port)
    return out


def offer_sdp(ip, aport, vport, opus, video=True):
    audio = "m=audio %d RTP/AVP %s 0 101\r\n" % (aport, "111" if opus else "")
    audio = audio.replace("AVP  ", "AVP ")
    rtpmap = "a=rtpmap:111 opus/48000/2\r\n" if opus else ""
    return ("v=0\r\no=test 1 1 IN IP4 %s\r\ns=t\r\nc=IN IP4 %s\r\nt=0 0\r\n" % (ip, ip) + audio + rtpmap +
            "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=sendrecv\r\n"
            + ("m=video %d RTP/AVP 96\r\na=rtpmap:96 VP8/90000\r\na=sendrecv\r\n" % vport if video else ""))


def rms_of(frames):
    return [float(np.sqrt(np.mean(f.to_ndarray().astype(np.float64) ** 2))) for f in frames]


async def main(a):
    loop = asyncio.get_running_loop()
    ip = a.local
    _, ap = await loop.create_datagram_endpoint(Media, local_addr=(ip, 0))
    _, vp = await loop.create_datagram_endpoint(Media, local_addr=(ip, 0))
    aport = ap.transport.get_extra_info("sockname")[1]
    vport = vp.transport.get_extra_info("sockname")[1]
    if a.tcp:
        reader, writer = await asyncio.open_connection(a.host, a.port)
        lport = writer.get_extra_info("sockname")[1]
    else:
        sip = Media()
        await loop.create_datagram_endpoint(lambda: sip, local_addr=(ip, 0))
        lport = sip.transport.get_extra_info("sockname")[1]
    proto = "TCP" if a.tcp else "UDP"
    call_id, tag = secrets.token_hex(6), secrets.token_hex(3)

    def request(method, cseq, extra="", body="", to_tag=""):
        text = (f"{method} sip:666@{a.host}:{a.port} SIP/2.0\r\nVia: SIP/2.0/{proto} {ip}:{lport};branch=z9hG4bK{secrets.token_hex(4)};rport\r\n"
                f"Max-Forwards: 70\r\nFrom: <sip:test@{ip}>;tag={tag}\r\nTo: <sip:doom@{ip}>{to_tag}\r\nCall-ID: {call_id}\r\n"
                f"CSeq: {cseq} {method}\r\nContact: <sip:test@{ip}:{lport}>\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}")
        return text.encode()

    def send(data):
        if a.tcp:
            writer.write(data)
        else:
            sip.transport.sendto(data, (a.host, a.port))

    async def recv_sip(timeout=10):
        if a.tcp:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += await asyncio.wait_for(reader.read(65536), timeout)
            head, _, body = buf.partition(b"\r\n\r\n")
            n = int([l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0])
            while len(body) < n:
                body += await asyncio.wait_for(reader.read(65536), timeout)
            return (head + b"\r\n\r\n" + body).decode()
        end = time.monotonic() + timeout
        while True:
            for i, (_, data, _) in enumerate(sip.packets):
                sip.packets.pop(i)
                return data.decode()
            if time.monotonic() > end:
                raise TimeoutError("no SIP reply")
            await asyncio.sleep(0.01)

    t0 = time.monotonic()
    send(request("INVITE", 1, "Content-Type: application/sdp\r\n", offer_sdp(ip, aport, vport, a.opus, not a.late_video)))
    while True:
        start, headers, body = parse(await recv_sip())
        print("<<", start)
        if start.startswith("SIP/2.0 200"):
            break
        assert start.startswith("SIP/2.0 1"), start
    print(f"answered after {time.monotonic() - t0:.2f}s")
    ports = answer_ports(body)
    totag = ";" + headers["to"].split(";", 1)[1]
    send(request("ACK", 1, to_tag=totag))
    audio_codec = next(l for l in body.splitlines() if l.startswith("a=rtpmap") and ("opus" in l or "PCMU" in l))
    print("answer:", audio_codec, ports)

    if a.late_video:
        assert "video" not in ports, ports
        await asyncio.sleep(1)
        send(request("INVITE", 2, "Content-Type: application/sdp\r\n", offer_sdp(ip, aport, vport, a.opus), to_tag=totag))
        while True:
            start, headers, body = parse(await recv_sip())
            print("<< re-INVITE", start)
            if not start.startswith("SIP/2.0 1"):
                break
        ports = answer_ports(body)
        print("after re-INVITE:", ports)
        send(request("ACK", 2, to_tag=totag))

    # let the server learn our RTP source address, then listen
    ap.transport.sendto(struct.pack("!BBHII", 0x80, 0, 0, 0, 1) + bytes(160), (a.host, ports["audio"]))
    await asyncio.sleep(a.measure)

    dec_a = OpusDecoder() if a.opus else PcmuDecoder()
    audio = [(t, p) for t, p, _ in ap.packets if len(p) > 12]
    frames = []
    for _, p in audio:
        pkt = RtpPacket.parse(p)
        frames += dec_a.decode(JitterFrame(pkt.payload, pkt.timestamp))
    level = rms_of(frames)
    print(f"audio packets {len(audio)} in {a.measure}s, rms mean {np.mean(level):.0f} max {np.max(level):.0f}")

    def decode_video(packets):
        dec, parts, pics = Vp8Decoder(), [], []
        for t, p, _ in packets:
            pkt = RtpPacket.parse(p)
            parts.append(vp8_depayload(pkt.payload))
            if pkt.marker:
                for f in dec.decode(JitterFrame(b"".join(parts), pkt.timestamp)):
                    pics.append((t, f.to_ndarray(format="gray")))
                parts = []
        return pics

    pics = decode_video(vp.packets)
    span = pics[-1][0] - pics[0][0]
    print(f"video: {len(pics)} frames decoded, {len(pics) / span:.1f} fps, {pics[0][1].shape[::-1]}")
    pressed_at = time.monotonic()

    # hold digit 2 (forward) as RFC 4733 for 2 seconds
    ts, seq = random.getrandbits(32), 1
    for i in range(100):
        dur = 160 * (i + 1)
        ap.transport.sendto(struct.pack("!BBHII", 0x80, 101, seq, ts, 1) + bytes([2, 10, dur >> 8 & 255, dur & 255]), (a.host, ports["audio"]))
        seq += 1
        await asyncio.sleep(0.02)
    for _ in range(3):
        ap.transport.sendto(struct.pack("!BBHII", 0x80, 0x80 | 101, seq, ts, 1) + bytes([2, 0x8A, 3, 0x20]), (a.host, ports["audio"]))
        seq += 1
    await asyncio.sleep(0.5)
    everything = decode_video(vp.packets)
    before = [f for t, f in everything if t < pressed_at][-1]
    after = [f for t, f in everything if t >= pressed_at]
    change = np.abs(after[-1].astype(int) - before.astype(int)).mean()
    print(f"mean pixel change after holding forward: {change:.1f}")
    if a.out:
        import PIL.Image as Image
        Image.fromarray(before).save(f"{a.out}/before.png")
        Image.fromarray(after[-1]).save(f"{a.out}/after.png")

    send(request("BYE", 3, to_tag=totag))
    start, _, _ = parse(await recv_sip())
    print("<<", start)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1", help="server to call")
    p.add_argument("--local", default="127.0.0.1", help="this machine's address as the server should see it")
    p.add_argument("--port", type=int, default=5060)
    p.add_argument("--tcp", action="store_true")
    p.add_argument("--late-video", action="store_true", help="offer audio only, then add video with a re-INVITE")
    p.add_argument("--opus", action="store_true")
    p.add_argument("--measure", type=float, default=6)
    p.add_argument("--out")
    asyncio.run(main(p.parse_args()))
