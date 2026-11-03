"""Voice input: record from the microphone, transcribe, and put the text in the prompt.

Recording (no installs needed on Windows):
  Windows      the built-in MCI recorder in winmm.dll via ctypes (16 kHz mono WAV)
  Linux/mac    arecord, sox (`rec`) or ffmpeg - whichever is installed
  any          config voice.record_command, e.g. "ffmpeg -f dshow -i audio=Microphone -t {seconds} {out}"

Transcription - config voice.engine:
  "llm"            send the audio to a model that accepts audio input (Inkling-Small does), as an
                   OpenAI-style input_audio content part, and ask for a verbatim transcript. Default.
  "transcriptions" an OpenAI-compatible /v1/audio/transcriptions endpoint (e.g. Whisper on vLLM - the
                   kind of service a web UI like Spyglass typically calls; see README for how to find it)
  "command"        any script that prints the transcript: "python my_stt.py {wav}"
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from spy import net

TRANSCRIBE_PROMPT = ("Transcribe this audio exactly, word for word, in the language spoken. Output ONLY the "
                     "transcript text - no quotes, labels, timestamps or commentary. If there is no speech, "
                     "output nothing.")


class VoiceError(Exception):
    pass


# --------------------------------------------------------------- recording ---
class _WinMCIRecorder:
    ALIAS = "spyrec"

    def __init__(self, rate=16000):
        import ctypes
        self.ctypes = ctypes
        self.winmm = ctypes.windll.winmm
        self.rate = rate

    def _mci(self, cmd: str):
        buf = self.ctypes.create_unicode_buffer(512)
        err = self.winmm.mciSendStringW(cmd, buf, 511, None)
        if err:
            ebuf = self.ctypes.create_unicode_buffer(512)
            self.winmm.mciGetErrorStringW(err, ebuf, 511)
            raise VoiceError("Microphone error (%s): %s" % (cmd.split()[0], ebuf.value))
        return buf.value

    def start(self):
        try:
            self._mci("close %s" % self.ALIAS)
        except VoiceError:
            pass
        self._mci("open new type waveaudio alias %s" % self.ALIAS)
        self._mci("set %s time format ms bitspersample 16 channels 1 samplespersec %d bytespersec %d alignment 2"
                  % (self.ALIAS, self.rate, self.rate * 2))
        self._mci("record %s" % self.ALIAS)

    def stop(self, out: Path):
        try:
            self._mci("stop %s" % self.ALIAS)
            self._mci('save %s "%s"' % (self.ALIAS, out))
        finally:
            try:
                self._mci("close %s" % self.ALIAS)
            except VoiceError:
                pass

    def cancel(self):
        try:
            self._mci("close %s" % self.ALIAS)
        except VoiceError:
            pass


class _ProcRecorder:
    def __init__(self, argv: List[str]):
        self.argv = argv
        self.proc = None

    def start(self):
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            raise VoiceError("Could not start recorder %s: %s" % (self.argv[0], e))
        time.sleep(0.2)
        if self.proc.poll() is not None:
            raise VoiceError("Recorder exited: %s" % self.proc.stderr.read().decode("utf-8", "replace")[-300:])

    def stop(self, out: Path):
        if self.proc and self.proc.poll() is None:
            try:
                if os.path.basename(self.argv[0]).startswith("ffmpeg"):
                    self.proc.stdin.write(b"q")   # ffmpeg finishes the file cleanly on 'q'
                    self.proc.stdin.flush()
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def make_recorder(cfg: Dict[str, Any], out: Path, max_seconds: int):
    rate = int(cfg.get("sample_rate", 16000))
    cmd = cfg.get("record_command")
    if cmd:
        import shlex
        argv = [a.replace("{out}", str(out)).replace("{seconds}", str(max_seconds)).replace("{rate}", str(rate))
                for a in shlex.split(cmd, posix=os.name != "nt")]
        return _ProcRecorder(argv)
    if os.name == "nt":
        return _WinMCIRecorder(rate)
    if shutil.which("arecord"):
        return _ProcRecorder(["arecord", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-d", str(max_seconds),
                              str(out)])
    if shutil.which("rec"):
        return _ProcRecorder(["rec", "-q", "-r", str(rate), "-c", "1", "-b", "16", str(out), "trim", "0",
                              str(max_seconds)])
    if shutil.which("ffmpeg"):
        src = ["-f", "avfoundation", "-i", ":0"] if sys.platform == "darwin" else ["-f", "pulse", "-i", "default"]
        return _ProcRecorder(["ffmpeg", "-loglevel", "error", "-y"] + src +
                             ["-t", str(max_seconds), "-ac", "1", "-ar", str(rate), str(out)])
    raise VoiceError("No way to record audio found. Set voice.record_command in config.")


def record(cfg: Dict[str, Any], out_dir: Path, wait_stop: Callable[[float], bool],
           on_tick: Optional[Callable[[float], None]] = None) -> Path:
    """Record until wait_stop(timeout) returns True (e.g. the user pressed Enter) or max_seconds.
    Raises KeyboardInterrupt if the user cancelled."""
    max_s = int(cfg.get("max_seconds", 120))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("voice_%s.wav" % time.strftime("%Y%m%d_%H%M%S"))
    rec = make_recorder(cfg, out, max_s)
    rec.start()
    t0 = time.time()
    try:
        while time.time() - t0 < max_s:
            if wait_stop(0.25):
                break
            if on_tick:
                on_tick(time.time() - t0)
    except KeyboardInterrupt:
        rec.cancel()
        raise
    rec.stop(out)
    if not out.exists() or out.stat().st_size < 1000:
        raise VoiceError("Nothing was recorded (check the microphone / Windows privacy settings for microphone "
                         "access by desktop apps).")
    return out


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


# ------------------------------------------------------------ transcription ---
def transcribe(path: Path, cfg: Dict[str, Any], llm_factory: Callable[[str], Any]) -> str:
    engine = cfg.get("engine", "llm")
    if engine == "command":
        import shlex
        argv = [a.replace("{wav}", str(path)) for a in shlex.split(cfg["command"], posix=os.name != "nt")]
        r = subprocess.run(argv, capture_output=True, timeout=int(cfg.get("timeout", 300)))
        if r.returncode != 0:
            raise VoiceError("Transcription command failed: %s" % r.stderr.decode("utf-8", "replace")[-400:])
        return r.stdout.decode("utf-8", "replace").strip()
    if engine == "transcriptions":
        return _transcriptions_api(path, cfg)
    return _llm_transcribe(path, cfg, llm_factory)


def _llm_transcribe(path: Path, cfg: Dict[str, Any], llm_factory) -> str:
    llm = llm_factory(cfg.get("model_role", "voice"))
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    style = cfg.get("audio_part", "input_audio")
    if style == "audio_url":
        part = {"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64," + b64}}
    else:
        part = {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": TRANSCRIBE_PROMPT}, part]}]
    resp = llm.chat(msgs, temperature=0, max_tokens=int(cfg.get("max_tokens", 4096)))
    text = (resp.content or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text


def _transcriptions_api(path: Path, cfg: Dict[str, Any]) -> str:
    url = cfg.get("url") or ""
    if not url or "<" in url:
        raise VoiceError("Set voice.url to the transcription endpoint, e.g. https://<HOST>/v1/audio/transcriptions")
    boundary = "----spy" + uuid.uuid4().hex
    fields = {"model": cfg.get("model", "whisper-1"), "response_format": "json"}
    if cfg.get("language"):
        fields["language"] = cfg["language"]
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: audio/wav\r\n\r\n"
             % (boundary, Path(path).name)).encode() + Path(path).read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    if cfg.get("api_key") and "<" not in cfg["api_key"]:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    try:
        _, _, raw = net.request(url, cfg, "POST", body, headers, timeout=cfg.get("timeout", 300))
    except net.HTTPError as e:
        raise VoiceError("Transcription request failed: %s" % e)
    txt = raw.decode("utf-8", "replace")
    try:
        data = json.loads(txt)
        return str(data.get("text", "")).strip() if isinstance(data, dict) else txt.strip()
    except ValueError:
        return txt.strip()


class StdinStopper:
    """wait_stop() implementation: True once the user presses Enter."""

    def __init__(self):
        self.ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            sys.stdin.readline()
        except Exception:
            pass
        self.ev.set()

    def __call__(self, timeout: float) -> bool:
        return self.ev.wait(timeout)
