"""Spoken commands. Caller audio goes to a Vosk recognizer limited to a fixed phrase list."""
import asyncio
import json
import logging
import os
import queue
import threading
from pathlib import Path

import av

log = logging.getLogger("voice")
ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("VOSK_MODEL", ROOT / "models/vosk-model-small-en-us-0.15"))
PHRASES = ["forward", "backwards", "strafe left", "strafe right", "turn left", "turn right",
           "shoot", "open door", "map", "next weapon", "run", "stop"]
RATE = 16000
LOCKOUT = 1.0  # seconds of audio ignored after a command
_model = None
_tried = False


def load_model():
    """The shared model, or None when voice control is not installed."""
    global _model, _tried
    if _tried:
        return _model
    _tried = True
    if not MODEL_DIR.exists():
        log.info("voice control off, no model at %s (scripts/fetch-vosk-model.sh)", MODEL_DIR)
        return None
    try:
        import vosk
        vosk.SetLogLevel(-1)
        _model = vosk.Model(str(MODEL_DIR))
        log.info("voice control on, %d phrases", len(PHRASES))
    except Exception as e:
        log.warning("voice control off: %s", e)
    return _model


class Voice:
    """Takes decoded caller audio frames and calls on_phrase(text) on the event loop."""

    def __init__(self, model, on_phrase):
        import vosk
        self._loop = asyncio.get_running_loop()
        self._on_phrase = on_phrase
        self._rec = vosk.KaldiRecognizer(model, RATE, json.dumps(PHRASES + ["[unk]"]))
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        self._queue = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def hear(self, frame):
        for out in self._resampler.resample(frame):
            data = bytes(out.planes[0])[: out.samples * 2]
            try:
                self._queue.put_nowait(data)
            except queue.Full:
                pass  # recognizer is behind, drop audio instead of building latency

    def _run(self):
        rec, heard, mute_until = self._rec, 0, 0
        while (data := self._queue.get()) is not None:
            heard += len(data) // 2
            if heard < mute_until:
                continue  # the tail of a word just acted on would otherwise fire again
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
            else:
                text = json.loads(rec.PartialResult()).get("partial", "")
            text = " ".join(w for w in text.split() if w != "[unk]")
            if text in PHRASES:
                rec.Reset()
                mute_until = heard + int(LOCKOUT * RATE)
                self._loop.call_soon_threadsafe(self._on_phrase, text)

    def close(self):
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
