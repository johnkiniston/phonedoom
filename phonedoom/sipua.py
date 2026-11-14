"""Minimal SIP user agent server (UAS) over WebSocket transport.

Answers INVITE by creating a Call, routes in-dialog INFO and BYE by Call-ID.
A real proxy such as OpenSIPS can sit in front and forward doom@ to this UA.
"""
import asyncio
import logging
import secrets

from .call import Call

log = logging.getLogger("sip")
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}


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


def _response(req, code, reason, tag, body="", contact=None):
    headers = []
    for v in req.get_all("via"):
        headers.append(("Via", v))
    headers.append(("From", req.get("from")))
    to = req.get("to")
    if "tag=" not in to:
        to += ";tag=" + tag
    headers.append(("To", to))
    headers.append(("Call-ID", req.get("call-id")))
    headers.append(("CSeq", req.get("cseq")))
    if contact:
        headers.append(("Contact", contact))
    if body:
        headers.append(("Content-Type", "application/sdp"))
    headers.append(("Content-Length", str(len(body.encode()))))
    lines = [f"SIP/2.0 {code} {reason}"] + [f"{k}: {v}" for k, v in headers]
    return "\r\n".join(lines) + "\r\n\r\n" + body


class UserAgent:
    def __init__(self, contact="<sip:doom@localhost;transport=ws>"):
        self.contact = contact
        self.calls = {}  # call-id -> (Call, tag, owner)

    async def handle(self, text, send, owner):
        if not text.strip():
            return
        msg = Message(text)
        if msg.start.startswith("SIP/2.0"):
            return
        if msg.method == "INVITE":
            await self._invite(msg, send, owner)
            return
        entry = self.calls.get(msg.get("call-id"))
        if msg.method == "ACK":
            return
        if msg.method == "OPTIONS":
            await send(_response(msg, 200, "OK", "opt"))
            return
        if not entry:
            await send(_response(msg, 481, "Call Does Not Exist", "x"))
            return
        call, tag, _ = entry
        if msg.method == "INFO":
            call.info(msg.get("content-type") or "", msg.body)
            await send(_response(msg, 200, "OK", tag))
        elif msg.method in ("BYE", "CANCEL"):
            await send(_response(msg, 200, "OK", tag))
            await call.stop()
        else:
            await send(_response(msg, 405, "Method Not Allowed", tag))

    async def _invite(self, msg, send, owner):
        call_id = msg.get("call-id")
        tag = "doom" + secrets.token_hex(3)
        await send(_response(msg, 100, "Trying", tag))
        call = Call(on_end=lambda: self.calls.pop(call_id, None))
        try:
            sdp = await call.start(msg.body)
        except Exception:
            log.exception("call setup failed")
            await call.stop()
            await send(_response(msg, 500, "Server Error", tag))
            return
        self.calls[call_id] = (call, tag, owner)
        await send(_response(msg, 200, "OK", tag, body=sdp, contact=self.contact))

    async def drop_owner(self, owner):
        for call, _, o in list(self.calls.values()):
            if o is owner:
                await call.stop()
