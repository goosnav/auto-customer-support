# Voice and phone setup

There are two distinct voice paths in this MVP:

| Path | What runs locally | Interaction | External dependency |
|---|---|---|---|
| Browser microphone | Transcription, support logic, knowledge, tickets, speech generation | Record one turn, release/send, hear a response | Initial model downloads; no inference API required |
| Telephone number | Support logic, knowledge, tickets | Twilio speech gathering and spoken replies | Twilio number, calls, hosted speech, public HTTPS callback |

The browser implementation is **push-to-talk**, not a full-duplex phone agent. Telephone speech in the included Twilio adapter is **cloud processed**. These are working integration paths, not claims of measured production voice quality or latency.

## 1. Browser transcription

In the activated project virtual environment:

```sh
pip install -e '.[voice]'
```

Set these values in your local `.env`, then restart `support-desk serve`:

```dotenv
STT_ENABLED=true
WHISPER_MODEL=small.en
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
VOICE_MAX_SECONDS=45
VOICE_MAX_BYTES=8388608
VOICE_TIMEOUT_SECONDS=60
```

**Download the model explicitly before enabling customer voice.** The running server uses cached/local files only and refuses a missing or incomplete model; customer requests cannot initiate a model download. Run this once using the same operating-system user as the server:

```sh
python -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8')"
```

For an offline deployment, copy a complete CTranslate2 model directory to the server and set `WHISPER_MODEL` to that directory. Include `model.bin`, `config.json`, `tokenizer.json`, and `preprocessor_config.json`. The adapter checks for all four files before constructing the model with `local_files_only=True`; this also prevents the library's missing-tokenizer fallback from fetching a separate tokenizer. The preload command above may use the network, but a public transcription request does not.

`faster-whisper` runs CPU inference and supports NVIDIA CUDA with a compatible CUDA/cuDNN installation. For a supported NVIDIA machine, `WHISPER_DEVICE=cuda` and `WHISPER_COMPUTE_TYPE=float16` are possible choices; verify runtime compatibility using the [upstream installation guide](https://github.com/SYSTRAN/faster-whisper#gpu).

This Python path uses CPU inference on a Mac. A future `whisper.cpp` adapter could exploit native Metal; [whisper.cpp documents Apple Silicon acceleration](https://github.com/ggml-org/whisper.cpp). This repo does not currently implement that adapter.

Microphone access requires localhost or HTTPS and the user's browser permission. Audio is decoded with a duration limit before transcription. No browser cloud speech-recognition API is used as a silent fallback.

One transcription may run at a time per server process, including audio decoding and model warmup. Further voice requests receive a busy response immediately and can use text instead. `VOICE_TIMEOUT_SECONDS` limits how long a request waits (default 60 seconds, maximum 120). A timeout or browser disconnect cannot kill native inference; the server retains the busy slot until that work actually completes, preventing abandoned requests from accumulating decoder/model jobs. A timed-out recording is not later submitted as a hidden support message. If a native runtime hangs permanently, restart the server; text support remains independent.

## 2. Local spoken replies

Run [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) separately. It exposes a local OpenAI-compatible speech endpoint. Example CPU startup:

```sh
docker run --rm -p 127.0.0.1:8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest
```

The command uses the upstream rolling tag for initial setup; pin a reviewed release tag/digest for a maintained installation. Model files and dependencies must be downloaded once. The service also supports native Apple MPS outside Docker; follow upstream's native setup for that configuration.

Set:

```dotenv
TTS_URL=http://127.0.0.1:8880
TTS_VOICE=af_heart
```

The adapter calls `/v1/audio/speech` with `model=kokoro` and `response_format=wav`. It bounds response size, validates PCM WAV output, and refuses redirects. `TTS_URL` accepts localhost, private LAN IPs, `host.docker.internal`, or a single-label Docker service name. The value is trusted operator configuration; customers cannot choose it. In Compose networking, use the service name, for example `http://kokoro:8880`, rather than another container's localhost.

Kokoro's [model card](https://huggingface.co/hexgrad/Kokoro-82M) identifies Apache-2.0 weights. The [inference library](https://github.com/hexgrad/kokoro) supplies 24 kHz audio. The optional sidecar is installed and managed separately from this application's dependencies.

## 3. Connect a real phone number

Install the optional Twilio SDK:

```sh
pip install -e '.[phone]'
```

Run the support server behind your HTTPS reverse proxy or tunnel, and set:

```dotenv
PHONE_ENABLED=true
PUBLIC_BASE_URL=https://support.your-domain.example
TWILIO_AUTH_TOKEN=your-real-twilio-auth-token
PHONE_TURN_TIMEOUT_SECONDS=8
```

Keep the token in `.env` or a secret store; never put it in the knowledge folder or commit it. Configure your Twilio number's incoming Voice webhook as:

```text
POST https://support.your-domain.example/api/phone/twilio
```

Use an origin with no path suffix for `PUBLIC_BASE_URL`. The configured public URL must match the actual URL Twilio signs. The adapter ignores forwarded host headers for signature construction, and validates all callback form parameters with the official SDK. An unsigned request always fails, including in development.

The flow is:

1. Identify the assistant as automated and ask for the question.
2. Twilio posts its transcription to the next numbered turn URL.
3. The shared support service answers or records an escalation ticket.
4. Twilio speaks the response and listens for another question.

Repeated callbacks use the call SID and turn number for application deduplication. Repeated silence ends the call. Slow local answers become a human-review ticket when the support service's time budget expires; if the entire channel fails, the caller is directed to website chat rather than promised an unrecorded follow-up. Caller ID is unverified metadata, not proof of order ownership. The system should collect a usable email address before promising an email reply.

### Cost and privacy boundary

Twilio's `<Gather>` sends speech to its speech-recognition provider; `<Say>` uses hosted speech synthesis. Neither uses the browser's local Whisper/Kokoro adapters. Current pricing and supported speech options are on [Twilio's pricing page](https://www.twilio.com/en-us/voice/pricing/us) and [Gather reference](https://www.twilio.com/docs/voice/twiml/gather). Phone numbers, call minutes, and optional speech features are not free. This repository does not purchase a number or place calls.

Configure an HTTPS gateway, request limits, uptime monitoring, and a separate business fallback before advertising the number. Keep the owner console private; only the intended customer routes and signed phone callback need public access.

## 4. Upgrade path for real-time local phone speech

The next implementation would replace Gather/Say with [bidirectional Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams) or a SIP/PBX gateway:

1. Receive 8 kHz mono μ-law frames over an authenticated WebSocket.
2. Decode/resample, detect voice activity, and transcribe streaming utterances.
3. Run the same support service and stream the grounded answer into TTS.
4. Resample generated audio back to μ-law and stream it to the call.
5. Handle barge-in, cancel queued speech, measure turn latency, and preserve ticket state across disconnects.

Twilio's [WebSocket protocol](https://www.twilio.com/docs/voice/media-streams/websocket-messages) specifies raw μ-law media, `mark` messages, and `clear` for interruption. This upgrade is **not implemented** in the MVP. The carrier still transports the call even when all inference is local.

### NVIDIA options

| Model | Relevant capability | Why it is not the default here |
|---|---|---|
| [PersonaPlex 7B](https://huggingface.co/nvidia/personaplex-7b-v1) | Full-duplex speech-to-speech with role and voice prompts; model card allows commercial use under NVIDIA Open Model License | Separate GPU-oriented Moshi runtime; grounded retrieval, permissions, and ticket workflows still need integration. Code MIT and model-weight license are different. |
| [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | Multilingual transcription; CC-BY-4.0 | Optional future ASR adapter and more model/runtime configuration. |
| [Nemotron ASR Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) | Native cache-aware streaming ASR; NVIDIA Open Model License | Useful for a streaming gateway; default documented deployment is Linux/NVIDIA. |
| [Nemotron 3 VoiceChat](https://build.nvidia.com/nvidia/nemotron-voicechat/modelcard) | Full-duplex 12B speech-to-speech model | At review on 2026-10-23, the model card states early-access evaluation-only terms. A free trial endpoint is not an unrestricted commercial deployment license. |

The conversational support service remains authoritative regardless of the speech runtime. A more natural voice must not invent shipment dates, override policies, or claim that an unsent email was delivered.
