Throwaway prototype. Call a SIP.js softphone into native Doom E1M1 and play it with the 12 keypad keys.

## How it works

    browser call.html (SIP.js) --SIP over WebSocket--> phonedoom (Python, aiohttp)
        INVITE  -> Call: spawn native doomphone, wait for its first frame, answer with an aiortc peer
        doomphone: 320x200 BGRA frames on a pipe, 44.1 kHz PCM on a pipe (SDL disk audio driver)
        aiortc: VP8 + Opus encode in-process, DTLS-SRTP/ICE to the browser
        SIP INFO (application/dtmf-relay) -> key policy -> 2-byte key events on doomphone stdin

Ordinary softphones call in too, with no proxy in between:

    Linphone / Zoiper --SIP over UDP or TCP :5060--> phonedoom
        INVITE with a plain RTP offer -> Opus or PCMU audio and VP8 video out over RTP/AVP
        RFC 4733 telephone-events in the audio stream -> same key policy as SIP INFO

No ffmpeg, no Node at runtime. `web/sip.bundle.js` is SIP.js prebuilt for the browser.

## Game data (you supply it)

No WAD is included. `scripts/fetch-shareware-wad.sh` downloads the free shareware `doom1.wad` and checks its MD5, or
supply a copy you own and point `DOOM_WAD` at it. Natively the default is `native/doom1.wad`; in Docker it is `/wad/doom1.wad`.

## Run on Linux (Docker)

    docker build -t phonedoom .
    docker run --rm --network host -v /path/to/wads:/wad:ro phonedoom   # wads dir contains doom1.wad
    # then open http://<host>:8080/ and click Call

Host networking matters: WebRTC media is UDP on ephemeral ports, so a bridged container is
unreachable from a browser outside the host. Inside the container: `docker exec <c> python tests/call_test.py`
places a headless call and prints fps, audio level and whether a key press changed the picture.

## Run natively (macOS or Linux)

    brew install sdl2 sdl2_mixer fluid-synth          # or apt: build-essential libsdl2-dev libsdl2-mixer-dev timgm6mb-soundfont
    make -C native/doomgeneric/doomgeneric -f Makefile.pipe CC=cc
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m phonedoom                    # http://localhost:8080/
    .venv/bin/python tests/call_test.py

Set `SF2` to a soundfont path if music is silent (Linux and Homebrew locations are auto-detected).

## Call from Linphone or Zoiper

Dial `sip:doom@<host>:5060` directly, or add an account whose registrar is `<host>` (REGISTER is accepted, no password).
Turn media encryption off (SRTP and ZRTP get a 488). Keep Opus or PCMU enabled for audio, and enable VP8 for video.
Zoiper's free tier has no video, so use Linphone to see the game. DTMF type RFC 2833 (the default) works, SIP INFO works too.
Use `--sip-port N` to move the port or `0` to turn the listener off. RTP uses UDP ports 10000 to 20000 (`RTP_PORT_MIN`, `RTP_PORT_MAX`).
On a host with several addresses set `PHONEDOOM_IP` to the one phones should send media to. Behind NAT set `PHONEDOOM_PUBLIC_IP`, which is advertised to callers with public addresses while LAN callers still get the LAN address.
`python tests/udp_call_test.py [--tcp] [--opus]` is a headless plain-SIP caller that checks fps, audio and that a held digit moves the player.

## Keys

2 forward, 8 back, 4/6 turn, 1/3 strafe, 5 fire, * use/open, 0 run, 7/9 weapon, # automap.
Modes (call page dropdown, or `DOOM_MODE` for phone callers): hold (auto-repeat while held), pulse (one 160 ms press per tap), latch (tap toggles).

## Notes

`legacy/` holds the earlier Node + ffmpeg + werift and js-dos prototypes. They are not maintained.
Licensing: see LICENSE and THIRD_PARTY_NOTICES.md. DOOM is a trademark of id Software; this project is unaffiliated.
