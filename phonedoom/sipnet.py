"""SIP over UDP and TCP, for softphones that do not speak WebSocket."""
import asyncio
import ipaddress
import logging
import os
import socket
from functools import lru_cache

from .sipua import Link

log = logging.getLogger("sipnet")


@lru_cache(maxsize=64)
def local_ip_for(peer_ip):
    if os.environ.get("PHONEDOOM_IP"):
        return os.environ["PHONEDOOM_IP"]
    public = os.environ.get("PHONEDOOM_PUBLIC_IP")
    if public and not ipaddress.ip_address(peer_ip).is_private:
        return public
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((peer_ip, 9))
        return s.getsockname()[0]


class UdpSip(asyncio.DatagramProtocol):
    def __init__(self, ua, port):
        self.ua, self.port = ua, port
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        text = data.decode("utf-8", "replace")
        if not text.strip():
            return

        async def send(reply):
            self.transport.sendto(reply.encode(), addr)

        self.ua.spawn(text, send, None, Link("udp", addr, local_ip_for(addr[0]), self.port))


def _content_length(head):
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() in (b"content-length", b"l"):
            return int(v.strip() or 0)
    return 0


def tcp_handler(ua):
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")[:2]
        local = writer.get_extra_info("sockname")[:2]
        link = Link("tcp", peer, local_ip_for(peer[0]) if os.environ.get("PHONEDOOM_PUBLIC_IP") else local[0], local[1])

        async def send(reply):
            writer.write(reply.encode())

        buf = b""
        try:
            while data := await reader.read(65536):
                buf += data
                while True:
                    buf = buf.lstrip(b"\r\n")
                    end = buf.find(b"\r\n\r\n")
                    if end < 0:
                        break
                    size = end + 4 + _content_length(buf[:end])
                    if len(buf) < size:
                        break
                    ua.spawn(buf[:size].decode("utf-8", "replace"), send, None, link)
                    buf = buf[size:]
        except (ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    return handle


async def start(ua, host, port):
    loop = asyncio.get_running_loop()
    udp, _ = await loop.create_datagram_endpoint(lambda: UdpSip(ua, port), local_addr=(host, port))
    tcp = await asyncio.start_server(tcp_handler(ua), host, port)
    log.info("SIP on udp/tcp %s:%s", host, port)
    return udp, tcp
