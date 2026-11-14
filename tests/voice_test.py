"""Voice command accuracy with macOS `say` speech pushed through a phone-quality (8 kHz PCMU) path.

Synthetic speech is easier than real callers, so treat this as a floor check, not a promise.
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiortc.codecs.g711 import PcmuDecoder, PcmuEncoder
from aiortc.jitterbuffer import JitterFrame

from phonedoom import voice

VOICES = ["Samantha", "Alex", "Daniel", "Karen", "Fred"]
NEGATIVE = ["hello there how are you", "what time is it", "the quick brown fox", "turn it up", "one two three"]


def speech_frames(text, who, tmp):
    path = Path(tmp) / "s.aiff"
    subprocess.run(["say", "-v", who, "-o", str(path), text], check=True, capture_output=True)
    pcm = []
    with av.open(str(path)) as c:
        rs = av.AudioResampler(format="s16", layout="mono", rate=8000, frame_size=160)
        for f in c.decode(audio=0):
            pcm += list(rs.resample(f))
    pcm += [av.AudioFrame.from_ndarray(np.zeros((1, 160), np.int16), format="s16", layout="mono") for _ in range(40)]
    enc, dec, out, ts = PcmuEncoder(), PcmuDecoder(), [], 0
    for f in pcm:
        f.sample_rate = 8000
        f.pts = ts
        f.time_base = av.AudioFrame.time_base if False else __import__("fractions").Fraction(1, 8000)
        ts += 160
        for payload in enc.encode(f)[0]:
            out += dec.decode(JitterFrame(payload, ts))
    return out


async def heard(frames, model):
    got = []
    v = voice.Voice(model, got.append)
    for f in frames:
        v.hear(f)
    await asyncio.sleep(1.5)
    v.close()
    return got


async def main():
    model = voice.load_model()
    assert model, "no model"
    ok = total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for phrase in voice.PHRASES:
            row = []
            for who in VOICES:
                got = await heard(speech_frames(phrase, who, tmp), model)
                good = got == [phrase]
                ok += good
                total += 1
                row.append("ok" if good else f"{got or '-'}")
            print(f"{phrase:14} " + "  ".join(f"{w}:{r}" for w, r in zip(VOICES, row)))
        false = 0
        for text in NEGATIVE:
            got = await heard(speech_frames(text, "Samantha", tmp), model)
            false += bool(got)
            print(f"not a command: {text!r} -> {got or 'nothing'}")
    print(f"recognized {ok}/{total}, false triggers {false}/{len(NEGATIVE)}")


asyncio.run(main())
