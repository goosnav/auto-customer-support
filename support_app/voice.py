"""Local, bounded, turn-based speech adapters. No browser cloud speech fallback."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import ipaddress
import threading
import wave
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


class ChannelUnavailable(RuntimeError):
    """An optional channel is not configured or could not complete its operation."""


def _tts_endpoint(value: str) -> str:
    """Only administrator-configured local/LAN services; never customer URLs."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ChannelUnavailable("TTS_URL must be a local HTTP(S) service URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ChannelUnavailable("TTS_URL must not contain credentials, a query, or a fragment.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Single-label hostnames allow Docker Compose service discovery. This is
        # trusted process configuration, never a URL supplied by a public request.
        local = host in {"localhost", "host.docker.internal"} or "." not in host
    else:
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("fc00::/7"),
        )
        local = address.is_loopback or any(address in net for net in private_ranges)
    if not local:
        raise ChannelUnavailable("TTS_URL must point to localhost, a private LAN IP, or a Docker service.")
    path = parsed.path.rstrip("/")
    if path in {"", "/v1"}:
        path = "/v1/audio/speech"
    elif path != "/v1/audio/speech":
        raise ChannelUnavailable("TTS_URL must be the server root, /v1, or /v1/audio/speech.")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class VoiceService:
    def __init__(self, settings):
        self.settings = settings
        self._model = None
        self._model_lock = threading.Lock()
        self._transcription_gate = asyncio.Lock()
        self._transcription_task: asyncio.Task[str] | None = None

    def status(self) -> dict:
        """Configuration visibility, not a claim of downloaded models/connectivity."""
        enabled = bool(getattr(self.settings, "stt_enabled", False))
        tts_url = str(getattr(self.settings, "tts_url", "") or "")
        return {
            "mode": "push_to_talk",
            "local": True,
            "stt": {
                "enabled": enabled,
                "configured": enabled,
                "installed": importlib.util.find_spec("faster_whisper") is not None,
                "model": getattr(self.settings, "whisper_model", "small.en"),
                "provider": "faster-whisper",
                "busy": self._transcription_gate.locked(),
                "local_files_only": True,
            },
            "tts": {"configured": bool(tts_url), "provider": "Kokoro-compatible local service"},
            "connectivity_checked": False,
        }

    async def transcribe(self, audio: bytes, suffix: str = ".webm") -> str:
        if not getattr(self.settings, "stt_enabled", False):
            raise ChannelUnavailable("Local transcription is disabled. Enable STT_ENABLED and install the voice extra.")
        maximum = int(getattr(self.settings, "voice_max_bytes", 8 * 1024 * 1024))
        if not audio or len(audio) > maximum:
            raise ValueError("The recording is empty or exceeds the configured upload limit.")
        if suffix.lower() not in {".webm", ".wav", ".mp4", ".m4a", ".ogg", ".mp3", ".flac"}:
            raise ValueError("Unsupported recording format.")
        # Reject before decode: serializing only model inference would still let
        # concurrent requests allocate many decoded buffers and executor jobs.
        if self._transcription_gate.locked():
            raise ChannelUnavailable("Local transcription is busy. Please use text or try again shortly.")
        await self._transcription_gate.acquire()
        task = asyncio.create_task(asyncio.to_thread(self._transcribe, audio), name="local-transcription")
        self._transcription_task = task
        task.add_done_callback(self._transcription_finished)
        timeout = min(120.0, max(0.01, float(getattr(self.settings, "voice_timeout_seconds", 60))))
        try:
            # Cancelling a request cannot stop a native decoder/inference thread.
            # Shield it so the gate remains held until that thread really exits.
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError as exc:
            raise ChannelUnavailable(
                "Local transcription is still warming up or processing. Please use text; voice will be available when it finishes."
            ) from exc

    def _transcription_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            # Event-loop shutdown can cancel the internal task while its native
            # thread still runs. Fail closed; do not admit another transcription.
            return
        # Retrieve exceptions even when the caller disconnected or timed out.
        task.exception()
        if self._transcription_task is task:
            self._transcription_task = None
            self._transcription_gate.release()

    def _load_model(self):
        """Resolve complete preinstalled assets without an inference-time download."""
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError as exc:
            raise ChannelUnavailable("Install the voice extra: pip install -e '.[voice]'.") from exc
        try:
            configured = str(getattr(self.settings, "whisper_model", "small.en"))
            model_path = Path(configured).expanduser()
            if not model_path.is_dir():
                model_path = Path(download_model(configured, local_files_only=True))
            required = ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json")
            if any(not (model_path / name).is_file() for name in required):
                raise ValueError("The local model is missing required model/tokenizer assets.")
            return WhisperModel(
                str(model_path),
                device=getattr(self.settings, "whisper_device", "cpu"),
                compute_type=getattr(self.settings, "whisper_compute_type", "int8"),
                local_files_only=True,
            )
        except Exception as exc:
            raise ChannelUnavailable(
                "The local speech model is unavailable. Preload its complete files using docs/voice.md before enabling voice."
            ) from exc

    def _transcribe(self, audio: bytes) -> str:
        try:
            import av
            import numpy as np
        except ImportError as exc:
            raise ChannelUnavailable("Install the voice extra: pip install -e '.[voice]'.") from exc

        seconds = float(getattr(self.settings, "voice_max_seconds", 45))
        max_samples = int(seconds * 16000)
        frames = []
        samples = 0
        try:
            with av.open(io.BytesIO(audio)) as container:
                if not container.streams.audio:
                    raise ValueError("The recording does not contain audio.")
                if container.duration is not None and container.duration / av.time_base > seconds + 0.1:
                    raise ValueError(f"Recordings must be at most {seconds:g} seconds.")
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                for decoded in container.decode(audio=0):
                    for frame in resampler.resample(decoded):
                        data = frame.to_ndarray().flatten()
                        samples += len(data)
                        if samples > max_samples:
                            raise ValueError(f"Recordings must be at most {seconds:g} seconds.")
                        frames.append(data)
                for frame in resampler.resample(None):
                    data = frame.to_ndarray().flatten()
                    samples += len(data)
                    if samples > max_samples:
                        raise ValueError(f"Recordings must be at most {seconds:g} seconds.")
                    frames.append(data)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("The recording could not be decoded as audio.") from exc
        if not frames:
            raise ValueError("The recording contains no audio samples.")
        signal = np.concatenate(frames).astype(np.float32) / 32768.0
        # Serialize loading/inference so concurrent calls do not duplicate model
        # memory or call a shared decoder concurrently. Loading remains lazy.
        with self._model_lock:
            try:
                if self._model is None:
                    self._model = self._load_model()
                segments, _ = self._model.transcribe(
                    signal, beam_size=1, language="en", vad_filter=True,
                    condition_on_previous_text=False,
                )
                text = " ".join(segment.text.strip() for segment in segments).strip()
            except ChannelUnavailable:
                raise
            except Exception as exc:
                raise ChannelUnavailable("Local transcription failed; check the model installation and device settings.") from exc
        if not text:
            raise ValueError("No speech was detected. Please try a shorter, clearer recording.")
        return text[:8000]

    async def synthesize(self, text: str) -> bytes:
        if not text.strip() or len(text) > 2000:
            raise ValueError("Speech text must contain between 1 and 2000 characters.")
        url = str(getattr(self.settings, "tts_url", "") or "")
        if not url:
            raise ChannelUnavailable("Local speech output is not configured. Set TTS_URL for a local Kokoro service.")
        endpoint = _tts_endpoint(url)
        chunks = []
        size = 0
        max_bytes = int(getattr(self.settings, "voice_max_bytes", 8 * 1024 * 1024))
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
                async with client.stream(
                    "POST", endpoint,
                    json={"model": "kokoro", "input": text,
                          "voice": getattr(self.settings, "tts_voice", "af_heart"),
                          "response_format": "wav", "speed": 1.0},
                ) as response:
                    response.raise_for_status()
                    if int(response.headers.get("content-length", "0")) > max_bytes:
                        raise ChannelUnavailable("The speech service returned too much audio.")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ChannelUnavailable("The speech service returned too much audio.")
                        chunks.append(chunk)
        except (httpx.HTTPError, ValueError) as exc:
            raise ChannelUnavailable("The local speech service is unavailable or returned an invalid response.") from exc
        audio = b"".join(chunks)
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
                # The input-recording limit does not constrain a spoken answer;
                # bound output separately to three minutes and valid PCM channels.
                if wav.getnchannels() not in {1, 2} or wav.getsampwidth() not in {1, 2, 3, 4}:
                    raise ValueError("Unsupported PCM format")
                if not 0 < duration <= 180:
                    raise ValueError("Invalid output duration")
                expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
                if len(wav.readframes(wav.getnframes())) != expected:
                    raise ValueError("Truncated audio")
        except (wave.Error, EOFError, ZeroDivisionError, ValueError) as exc:
            raise ChannelUnavailable("The local speech service did not return a valid PCM WAV file.") from exc
        return audio
