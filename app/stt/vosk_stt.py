"""Speech to text: Telegram OGG/Opus -> 16 kHz mono WAV (ffmpeg) -> Vosk (Persian)."""

import json
import logging
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

log = logging.getLogger("stt")

_model = None
_model_lock = threading.Lock()


@dataclass
class Transcription:
    text: str
    confidence: float  # mean per-word confidence, 0..1; 0.0 when nothing was recognised

    @property
    def usable(self) -> bool:
        return bool(self.text.strip()) and self.confidence >= get_settings().stt_min_confidence


class SttError(RuntimeError):
    pass


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)
            path = get_settings().stt_model_path
            if not Path(path).is_dir():
                raise SttError(f"Vosk model not found at {path}")
            _model = Model(path)
        return _model


def convert_to_wav(src: Path, dst: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SttError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:300]}")


def transcribe_wav(wav_path: Path) -> Transcription:
    from vosk import KaldiRecognizer

    with wave.open(str(wav_path), "rb") as wf:
        rec = KaldiRecognizer(_get_model(), wf.getframerate())
        rec.SetWords(True)
        pieces: list[dict] = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                pieces.append(json.loads(rec.Result()))
        pieces.append(json.loads(rec.FinalResult()))

    words = [w for p in pieces for w in p.get("result", [])]
    text = " ".join(p.get("text", "") for p in pieces).strip()
    confidence = sum(w["conf"] for w in words) / len(words) if words else 0.0
    return Transcription(text=text, confidence=round(confidence, 3))


def transcribe_ogg(ogg_path: Path) -> Transcription:
    """Convert and transcribe one Telegram voice file. Temporary WAV is always removed."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        convert_to_wav(ogg_path, wav)
        return transcribe_wav(wav)
