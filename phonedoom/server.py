import argparse
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from . import sipnet
from .doom import DOOM_BIN, WAD
from .sipua import UserAgent

WEB = Path(__file__).resolve().parent.parent / "web"


async def sip_ws(request):
    ws = web.WebSocketResponse(protocols=("sip",))
    await ws.prepare(request)
    ua = request.app["ua"]
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await ua.handle(msg.data, ws.send_str, ws)
    await ua.drop_owner(ws)
    return ws


def build_app(sip_port=0, host="0.0.0.0"):
    app = web.Application()
    app["ua"] = UserAgent()

    async def start_sip(app):
        app["sip"] = await sipnet.start(app["ua"], host, sip_port)

    async def stop_sip(app):
        udp, tcp = app["sip"]
        udp.close()
        tcp.close()

    if sip_port:
        app.on_startup.append(start_sip)
        app.on_cleanup.append(stop_sip)
    app.router.add_get("/sip", sip_ws)
    app.router.add_get("/", lambda r: web.FileResponse(WEB / "call.html"))
    app.router.add_static("/", WEB)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--sip-port", type=int, default=5060, help="UDP and TCP port for softphones, 0 to disable")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    for what, path in (('Doom binary', DOOM_BIN), ('IWAD', WAD)):
        if not path.exists():
            raise SystemExit(f'{what} not found at {path}. See README (build the binary; supply your own doom1.wad via DOOM_WAD).')
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s")
    web.run_app(build_app(args.sip_port, args.host), host=args.host, port=args.port, print=lambda s: print(f"The Doom Phone on http://{args.host}:{args.port}/"))
