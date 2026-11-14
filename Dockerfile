FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libsdl2-dev libsdl2-mixer-dev timgm6mb-soundfont \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY native native
RUN make -C native/doomgeneric/doomgeneric -f Makefile.pipe clean \
 && make -C native/doomgeneric/doomgeneric -f Makefile.pipe CC=gcc -j"$(nproc)"

COPY phonedoom phonedoom
COPY web web
COPY tests tests

ENV DOOM_WAD=/wad/doom1.wad
ENV SF2=/usr/share/sounds/sf2/TimGM6mb.sf2
EXPOSE 8080
CMD ["python", "-m", "phonedoom"]
