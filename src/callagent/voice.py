"""Ears and a mouth for the demo page: whisper.cpp to listen, the macOS ``say`` command to talk.

Both run on this machine. This is a turn-by-turn demo: the contact speaks, then the agent
does. A production voice agent streams audio both ways and can be interrupted, which this
does not attempt.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from callagent.playbook import ROOT

WHISPER_MODEL = ROOT / "models" / "ggml-base.en.bin"
_NOISE = ("[", "(")  # [BLANK_AUDIO], (wind blowing) and similar


def available() -> dict[str, bool]:
    return {
        "listen": bool(shutil.which("whisper-cli") and shutil.which("ffmpeg"))
        and WHISPER_MODEL.exists(),
        "speak": bool(shutil.which("say")),
    }


def transcribe(recording: bytes, model: Path = WHISPER_MODEL) -> str:
    """Speech in any format ffmpeg reads (the browser sends WebM or MP4) to text."""
    with tempfile.TemporaryDirectory() as folder:
        source, wav = Path(folder) / "in", Path(folder) / "in.wav"
        source.write_bytes(recording)
        convert = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source)]
        subprocess.run([*convert, "-ar", "16000", "-ac", "1", str(wav)], check=True)
        subprocess.run(
            ["whisper-cli", "-m", str(model), "-oj", "-np", str(wav)],
            check=True,
            capture_output=True,
        )
        result = json.loads(Path(f"{wav}.json").read_text())
    pieces = [item["text"].strip() for item in result.get("transcription", [])]
    return " ".join(piece for piece in pieces if piece and not piece.startswith(_NOISE))


def speak(text: str, voice: str = "Samantha") -> bytes:
    """Text to a WAV recording of it being said."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "line.wav"
        command = ["say", "-v", voice, "--file-format=WAVE", "--data-format=LEI16@22050"]
        subprocess.run([*command, "-o", str(path), text], check=True)
        return path.read_bytes()
