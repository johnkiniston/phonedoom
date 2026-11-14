"""Minimal SIP user agent server (UAS) over WebSocket, UDP or TCP.

Answers INVITE by creating a Call, routes in-dialog INFO and BYE by Call-ID.
A real proxy such as OpenSIPS can sit in front and forward doom@ to this UA.
"""
import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from .call import Call
from .rtp import UnsupportedMedia

log = logging.getLogger("sip")
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, REGISTER"


@dataclass(frozen=True)
class Link:
    """Where a request came from and which of our addresses it reached."""
    transport: str = "ws"
    peer: tuple | None = None
    local_ip: str = "localhost"
    local_port: int = 0

    @property
    def contact(self):
        if self.transport == "ws":
            return "<sip:doom@localhost;transport=ws>"
        return f"<sip:doom@{self.local_ip}:{self.local_port};transport={self.transport}>"


def _norm(name):
    n = name.lower()
    return COMPACT.get(n, n)


class Message:
    def __init__(self, text):
        head, _, self.body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.start = lines[0]
        self.headers = []
        for line in lines[1:]:
            k, _, v = line.partition(":")
            self.headers.append((k.strip(), v.strip()))
        self.method = self.start.split(" ")[0]

    def get(self, name):
        for k, v in self.headers:
            if _norm(k) == name:
                return v
        return None

    def get_all(self, name):
        return [v for k, v in self.headers if _norm(k) == name]


def _stamp(via, peer):
    """Fill received and rport on the top Via so the reply finds the sender behind NAT."""
    ip, port = peer
    parts = [p for p in via.split(";") if not p.strip().lower().startswith("received=")]
    out = [parts[0]]
    for p in parts[1:]:
        out.append(f"rport={port}" if p.strip().lower() == "rport" else p)
    out.append(f"received={ip}")
    return ";".join(out)


def _response(req, code, reason, tag, body="", content_type="application/sdp", link=Link(), extra=()):
    vias = req.get_all("via")
    if link.peer and vias:
        vias[0] = _stamp(vias[0], link.peer)
    headers = [("Via", v) for v in vias]
    if req.method == "INVITE":
        headers += [("Record-Route", v) for v in req.get_all("record-route")]
    headers.append(("From", req.get("from")))
    to = req.get("to")
    if "tag=" not in to and code > 100:
        to += ";tag=" + tag
    headers += [("To", to), ("Call-ID", req.get("call-id")), ("CSeq", req.get("cseq"))]
    if code < 300 and req.method in ("INVITE", "OPTIONS"):
        headers.append(("Allow", ALLOW))
    if req.method == "INVITE" and 200 <= code < 300:
        headers.append(("Contact", link.contact))
    headers += list(extra)
    if body:
        headers.append(("Content-Type", content_type))
    headers.append(("Content-Length", str(len(body.encode()))))
    lines = [f"SIP/2.0 {code} {reason}"] + [f"{k}: {v}" for k, v in headers]
    return "\r\n".join(lines) + "\r\n\r\n" + body


@dataclass
class Entry:
    call: Call
    tag: str
    owner: object
    ok: str = ""
    cseq: str = ""
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    retransmit: asyncio.Task | None = None


class UserAgent:
    def __init__(self):
        self.calls = {}  # call-id -> Entry
        self.pending = {}  # call-id -> True once CANCELed during setup
        self._tasks = set()

    def spawn(self, text, send, owner, link):
        task = asyncio.create_task(self.handle(text, send, owner, link))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, text, send, owner, link=Link()):
        if not text.strip():
            return
        msg = Message(text)
        if msg.start.startswith("SIP/2.0"):
            return
        cid = msg.get("call-id")
        entry = self.calls.get(cid)
        if msg.method == "INVITE":
            await self._invite(msg, send, owner, link)
        elif msg.method == "ACK":
            if entry:
                entry.acked.set()
        elif msg.method == "OPTIONS":
            await send(_response(msg, 200, "OK", "opt", link=link))
        elif msg.method == "REGISTER":
            extra = [("Contact", c + ";expires=3600") for c in msg.get_all("contact") if c != "*"]
            await send(_response(msg, 200, "OK", "reg", link=link, extra=extra))
        elif msg.method == "CANCEL" and cid in self.pending:
            self.pending[cid] = True
            await send(_response(msg, 200, "OK", "x", link=link))
        elif not entry:
            await send(_response(msg, 481, "Call Does Not Exist", "x", link=link))
        elif msg.method == "INFO":
            entry.call.info(msg.get("content-type") or "", msg.body)
            await send(_response(msg, 200, "OK", entry.tag, link=link))
        elif msg.method in ("BYE", "CANCEL"):
            await send(_response(msg, 200, "OK", entry.tag, link=link))
            await entry.call.stop()
        else:
            await send(_response(msg, 405, "Method Not Allowed", entry.tag, link=link))

    async def _invite(self, msg, send, owner, link):
        cid = msg.get("call-id")
        old = self.calls.get(cid)
        if old:
            await self._reinvite(old, msg, send, link)
            return
        if cid in self.pending:
            return
        self.pending[cid] = False
        tag = "doom" + secrets.token_hex(3)
        await send(_response(msg, 100, "Trying", tag, link=link))
        call = Call(on_end=lambda: self._forget(cid))
        try:
            sdp = await call.start(msg.body, link.local_ip)
        except UnsupportedMedia as e:
            log.info("rejecting call: %s", e)
            await call.stop()
            await send(_response(msg, 488, "Not Acceptable Here", tag, link=link, extra=[("Warning", f'399 doom "{e}"')]))
            return
        except Exception:
            log.exception("call setup failed")
            await call.stop()
            await send(_response(msg, 500, "Server Error", tag, link=link))
            return
        finally:
            cancelled = self.pending.pop(cid, False)
        if cancelled:
            await call.stop()
            await send(_response(msg, 487, "Request Terminated", tag, link=link))
            return
        ok = _response(msg, 200, "OK", tag, body=sdp, link=link)
        entry = self.calls[cid] = Entry(call, tag, owner, ok, msg.get("cseq"))
        await send(ok)
        if link.transport == "udp":
            entry.retransmit = asyncio.create_task(self._retransmit(entry, send))

    async def _reinvite(self, entry, msg, send, link):
        cseq = msg.get("cseq")
        if cseq == entry.cseq or not msg.body.strip():  # retransmit, or an offerless refresh
            await send(entry.ok)
            return
        await send(_response(msg, 100, "Trying", entry.tag, link=link))
        try:
            sdp = await entry.call.reinvite(msg.body, link.local_ip)
        except (UnsupportedMedia, ValueError) as e:
            await send(_response(msg, 488, "Not Acceptable Here", entry.tag, link=link, extra=[("Warning", f'399 doom "{e}"')]))
            return
        entry.cseq = cseq
        entry.ok = _response(msg, 200, "OK", entry.tag, body=sdp, link=link)
        await send(entry.ok)

    async def _retransmit(self, entry, send):
        """RFC 3261 section 13.3.1.4. Resend the 200 over UDP until the ACK arrives."""
        delay, waited = 0.5, 0.0
        while waited < 32:
            try:
                await asyncio.wait_for(entry.acked.wait(), delay)
                return
            except asyncio.TimeoutError:
                await send(entry.ok)
                waited += delay
                delay = min(delay * 2, 4)
        log.info("no ACK, ending call")
        await entry.call.stop()

    def _forget(self, cid):
        entry = self.calls.pop(cid, None)
        if entry and entry.retransmit:
            entry.retransmit.cancel()

    async def drop_owner(self, owner):
        for entry in list(self.calls.values()):
            if entry.owner is owner:
                await entry.call.stop()
