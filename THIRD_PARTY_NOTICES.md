# Third-party notices

## doomgeneric (native/doomgeneric)
GPL-2.0. Vendored from https://github.com/ozkl/doomgeneric, which is a port of id Software's Doom
source release (GPL) with parts derived from Chocolate Doom. Full text in native/doomgeneric/LICENSE.
Changes in this repository are limited to adding `doomgeneric_pipe.c` and `Makefile.pipe`. The
phonedoom Python code runs the compiled binary as a separate process and talks to it over pipes.
Upstream screenshots were removed from the vendored copy.

## SIP.js (web/sip.bundle.js)
The file is SIP.js 0.21.2 bundled with esbuild. SIP.js is MIT licensed:

MIT License

Copyright (c) 2020 Junction Networks

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Runtime dependencies installed from PyPI or apt, not redistributed here
aiortc (BSD-3-Clause), aiohttp (Apache-2.0), numpy (BSD-3-Clause), SDL2 and SDL2_mixer (zlib),
TimGM6mb soundfont in the Docker image (GPL-2.0, installed from Debian, not copied into this repo).

## Game data
No game data is included. DOOM.WAD and doom1.wad are copyrighted by id Software / ZeniMax. Supply your
own. The shareware doom1.wad is freely redistributable under id's shareware terms, but this repository
does not ship it. DOOM is a trademark of id Software. This project is not affiliated with or endorsed by them.
