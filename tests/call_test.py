"""Headless SIP+WebRTC caller: calls sip:doom, measures video fps and audio level, presses keys, checks the picture changes."""
import argparse
import asyncio
import secrets
import time

import aiohttp
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription


def parse(text):
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), v.strip())
    return lines[0], headers, body


async def main(url, outdir):
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")
    stats = {"video": 0, "audio_rms": [], "frames": []}
    t0 = {}

    @pc.on("track")
    def on_track(track):
        async def pump():
            while True:
                try:
                    f = await track.recv()
                except Exception:
                    return
                if track.kind == "video":
                    stats["video"] += 1
                    stats["frames"].append((time.time(), f.to_ndarray(format="bgr24")))
                    stats["frames"] = stats["frames"][-120:]
                else:
                    a = f.to_ndarray().astype(np.float32)
                    stats["audio_rms"].append((time.time(), float(np.sqrt((a ** 2).mean()))))
        asyncio.ensure_future(pump())

    await pc.setLocalDescription(await pc.createOffer())
    call_id = secrets.token_hex(6)
    tag = secrets.token_hex(3)
    cseq = [1]

    def req(method, uri, body="", extra=None, to_tag=None, ctype=None):
        h = [f"{method} {uri} SIP/2.0", f"Via: SIP/2.0/WS test.invalid;branch=z9hG4bK{secrets.token_hex(5)}",
             "Max-Forwards: 70", f"From: <sip:tester@localhost>;tag={tag}",
             "To: <sip:doom@localhost>" + (f";tag={to_tag}" if to_tag else ""), f"Call-ID: {call_id}",
             f"CSeq: {cseq[0]} {method}", "Contact: <sip:tester@test.invalid;transport=ws>"]
        h += extra or []
        if body:
            h.append(f"Content-Type: {ctype or 'application/sdp'}")
        h.append(f"Content-Length: {len(body.encode())}")
        return "\r\n".join(h) + "\r\n\r\n" + body

    async with aiohttp.ClientSession() as s, s.ws_connect(url, protocols=("sip",)) as ws:
        await ws.send_str(req("INVITE", "sip:doom@localhost", pc.localDescription.sdp))
        to_tag = None
        t_invite = time.time()
        while True:
            msg = await asyncio.wait_for(ws.receive(), 30)
            start, headers, body = parse(msg.data)
            print("<<", start)
            if start.startswith("SIP/2.0 200"):
                to_tag = headers["to"].split("tag=")[1]
                break
            if start.startswith("SIP/2.0 4") or start.startswith("SIP/2.0 5"):
                raise SystemExit("call rejected")
        print(f"answered after {time.time() - t_invite:.2f}s")
        await pc.setRemoteDescription(RTCSessionDescription(body, "answer"))
        await ws.send_str(req("ACK", "sip:doom@localhost", to_tag=to_tag))

        for _ in range(100):
            if pc.connectionState == "connected":
                break
            await asyncio.sleep(0.1)
        print("pc", pc.connectionState)

        async def tone(sig, ms, repeat=90):
            end = time.time() + ms / 1000
            while time.time() < end:
                cseq[0] += 1
                body = f"Signal={sig}\r\nDuration=140\r\nSent={int(time.time() * 1000)}"
                await ws.send_str(req("INFO", "sip:doom@localhost", body, to_tag=to_tag, ctype="application/dtmf-relay"))
                await asyncio.sleep(repeat / 1000)

        async def drain():
            while True:
                m = await ws.receive()
                if m.type != aiohttp.WSMsgType.TEXT:
                    return
        asyncio.ensure_future(drain())

        await asyncio.sleep(3)
        n0 = stats["video"]
        ta = time.time()
        await asyncio.sleep(6)
        fps = (stats["video"] - n0) / (time.time() - ta)
        rms = [r for t, r in stats["audio_rms"] if t > ta]
        print(f"video fps over 6s: {fps:.1f}   audio rms mean {np.mean(rms):.0f} max {np.max(rms):.0f} (packets {len(rms)})")
        before = stats["frames"][-1][1].copy()
        await tone("2", 1500)
        await asyncio.sleep(0.5)
        after = stats["frames"][-1][1].copy()
        print("mean pixel change after holding forward:", float(np.abs(before.astype(int) - after.astype(int)).mean()))
        n1, tb = stats["video"], time.time()
        await tone("5", 1500)
        rms2 = [r for t, r in stats["audio_rms"] if t > tb]
        print(f"audio rms while firing: mean {np.mean(rms2):.0f} max {np.max(rms2):.0f}")
        await asyncio.sleep(25)
        n2 = stats["video"]
        await asyncio.sleep(5)
        print(f"fps after 30+ s in call: {(stats['video'] - n2) / 5:.1f}")
        if outdir:
            for name, frame in (("before", before), ("after", after)):
                with open(f"{outdir}/{name}.ppm", "wb") as f:
                    f.write(b"P6 %d %d 255\n" % (frame.shape[1], frame.shape[0]) + frame[:, :, ::-1].tobytes())
        cseq[0] += 1
        await ws.send_str(req("BYE", "sip:doom@localhost", to_tag=to_tag))
        await asyncio.sleep(0.5)
    await pc.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8080/sip")
    ap.add_argument("--out")
    a = ap.parse_args()
    asyncio.run(main(a.url, a.out))
