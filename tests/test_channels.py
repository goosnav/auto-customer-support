import asyncio
import io
import smtplib
import sys
import threading
import wave
from email.message import EmailMessage
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import FastAPI

from support_app import mail as mail_module
from support_app import voice as voice_module
from support_app.mail import MailAdapter, _ignore_message, _message_text
from support_app.phone import ROUTE, build_phone_router
from support_app.voice import ChannelUnavailable, VoiceService, _tts_endpoint


def settings(**overrides):
    data = dict(
        stt_enabled=False, voice_max_bytes=8192, voice_max_seconds=45,
        tts_url="", tts_voice="af_heart", phone_enabled=False,
        twilio_auth_token="secret", public_base_url="https://support.example.com",
        business_name="Example & Company", greeting="How can I help?",
        imap_host="imap.example.com", imap_port=993, imap_username="support@example.com",
        imap_password="password", imap_mailbox="INBOX", imap_batch_size=30,
        smtp_host="smtp.example.com", smtp_port=465, smtp_from="support@example.com",
        smtp_username="support@example.com", smtp_password="password",
        smtp_security="ssl", email_delivery_enabled=False,
    )
    return SimpleNamespace(**(data | overrides))


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_local_voice_is_explicit_and_bounds_input():
    with pytest.raises(ChannelUnavailable, match="disabled"):
        await VoiceService(settings()).transcribe(b"audio")
    service = VoiceService(settings(stt_enabled=True, voice_max_bytes=4))
    with pytest.raises(ValueError, match="limit"):
        await service.transcribe(b"too long")
    with pytest.raises(ValueError, match="format"):
        await service.transcribe(b"123", ".exe")


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
async def test_transcription_keeps_busy_slot_until_abandoned_native_work_finishes(monkeypatch, abandon):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release_worker = threading.Event()
    service = VoiceService(settings(stt_enabled=True, voice_timeout_seconds=0.04 if abandon == "timeout" else 5))
    invocations = []

    def blocked_decode(audio):
        invocations.append(audio)
        loop.call_soon_threadsafe(started.set)
        assert release_worker.wait(timeout=3), "Test did not release its fake decoder"
        return "Completed local transcription"

    monkeypatch.setattr(service, "_transcribe", blocked_decode)
    request = asyncio.create_task(service.transcribe(b"audio"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        worker = service._transcription_task
        with pytest.raises(ChannelUnavailable, match="busy"):
            await service.transcribe(b"second upload")
        if abandon == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            with pytest.raises(ChannelUnavailable, match="still warming up or processing"):
                await request
        assert worker is not None and not worker.done()
        assert service._transcription_gate.locked()
        with pytest.raises(ChannelUnavailable, match="busy"):
            await service.transcribe(b"third upload")
        assert invocations == [b"audio"]
    finally:
        release_worker.set()
        await asyncio.gather(request, return_exceptions=True)
        if service._transcription_task is not None:
            await asyncio.wait_for(asyncio.shield(service._transcription_task), timeout=1)
    await asyncio.sleep(0)
    assert not service._transcription_gate.locked()
    monkeypatch.setattr(service, "_transcribe", lambda audio: "A new recording works")
    assert await service.transcribe(b"new") == "A new recording works"


@pytest.mark.asyncio
async def test_failed_native_transcription_releases_capacity(monkeypatch):
    service = VoiceService(settings(stt_enabled=True))

    def invalid_recording(audio):
        raise ValueError("The recording could not be decoded")

    monkeypatch.setattr(service, "_transcribe", invalid_recording)
    with pytest.raises(ValueError, match="decoded"):
        await service.transcribe(b"broken")
    assert not service._transcription_gate.locked()
    monkeypatch.setattr(service, "_transcribe", lambda audio: "Valid recording")
    assert await service.transcribe(b"valid") == "Valid recording"


def test_model_loading_is_local_only_and_requires_complete_tokenizer_assets(monkeypatch, tmp_path):
    package = ModuleType("faster_whisper")
    utility = ModuleType("faster_whisper.utils")
    package.WhisperModel = Mock(return_value=object())
    utility.download_model = Mock(return_value=str(tmp_path))
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utility)
    for filename in ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json"):
        (tmp_path / filename).write_text("test model fixture")
    service = VoiceService(settings(stt_enabled=True, whisper_model="small.en"))
    assert service._load_model() is package.WhisperModel.return_value
    utility.download_model.assert_called_once_with("small.en", local_files_only=True)
    package.WhisperModel.assert_called_once_with(str(tmp_path), device="cpu", compute_type="int8", local_files_only=True)
    package.WhisperModel.reset_mock()
    (tmp_path / "tokenizer.json").unlink()
    with pytest.raises(ChannelUnavailable, match="Preload"):
        service._load_model()
    package.WhisperModel.assert_not_called()


@pytest.mark.parametrize("url", ["https://external.example.com", "http://169.254.169.254", "http://user:pw@localhost:8880", "file:///tmp/voice", "http://localhost:8880?url=evil"])
def test_tts_cannot_be_redirected_to_public_or_metadata_services(url):
    with pytest.raises(ChannelUnavailable):
        _tts_endpoint(url)


@pytest.mark.asyncio
async def test_synthesis_requires_valid_wav_and_uses_configured_endpoint(monkeypatch):
    audio = wav_bytes()
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    monkeypatch.setattr(voice_module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    result = await VoiceService(settings(tts_url="http://kokoro:8880")).synthesize("Your ticket is ready.")
    assert result == audio
    assert str(requests[0].url) == "http://kokoro:8880/v1/audio/speech"
    assert b'"response_format":"wav"' in requests[0].content


@pytest.mark.asyncio
async def test_synthesis_rejects_html_backend_errors(monkeypatch):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>not audio</html>"))
    monkeypatch.setattr(voice_module.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    with pytest.raises(ChannelUnavailable, match="WAV"):
        await VoiceService(settings(tts_url="http://127.0.0.1:8880")).synthesize("Hello")


def phone_client(config, service):
    app = FastAPI()
    app.include_router(build_phone_router(config, service))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_phone_disabled_does_not_call_support():
    service = SimpleNamespace(phone_turn=AsyncMock())
    async with phone_client(settings(), service) as client:
        response = await client.post(ROUTE, data={"CallSid": "CA" + "1" * 32})
    assert response.status_code == 503
    service.phone_turn.assert_not_called()


@pytest.mark.asyncio
async def test_phone_requires_signature_and_escapes_reply():
    RequestValidator = pytest.importorskip("twilio.request_validator").RequestValidator
    config = settings(phone_enabled=True)
    service = SimpleNamespace(phone_turn=AsyncMock(return_value={"reply": "An <answer> & a ticket."}))
    client = phone_client(config, service)
    path = ROUTE + "?turn=1"
    params = {"CallSid": "CA" + "1" * 32, "SpeechResult": "Where is my order?", "From": "+15551234567"}
    assert (await client.post(path, data=params)).status_code == 403
    signature = RequestValidator(config.twilio_auth_token).compute_signature(config.public_base_url + path, params)
    response = await client.post(path, data=params, headers={"X-Twilio-Signature": signature, "X-Forwarded-Host": "attacker.example"})
    assert response.status_code == 200
    xml = ElementTree.fromstring(response.content)
    assert xml.find("Gather/Say").text == "An <answer> & a ticket."
    assert xml.find("Gather/answer") is None
    assert xml.find("Gather").get("action") == config.public_base_url + ROUTE + "?turn=2"
    service.phone_turn.assert_awaited_once_with(call_sid=params["CallSid"], speech=params["SpeechResult"], caller=params["From"], event_key=params["CallSid"] + ":1")
    # A signature for another turn is not valid for a replayed/modified URL.
    assert (await client.post(ROUTE + "?turn=2", data=params, headers={"X-Twilio-Signature": signature})).status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_phone_initial_greeting_does_not_make_an_llm_call():
    RequestValidator = pytest.importorskip("twilio.request_validator").RequestValidator
    config = settings(phone_enabled=True)
    service = SimpleNamespace(phone_turn=AsyncMock())
    params = {"CallSid": "CA" + "2" * 32}
    signature = RequestValidator(config.twilio_auth_token).compute_signature(config.public_base_url + ROUTE, params)
    async with phone_client(config, service) as client:
        response = await client.post(ROUTE, data=params, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200
    assert "automated support assistant" in response.text
    service.phone_turn.assert_not_called()


def customer_email(**headers):
    message = EmailMessage()
    message["From"] = "Customer <customer@example.org>"
    message["To"] = "support@example.com"
    message["Subject"] = "Order question"
    message["Message-ID"] = "<customer-message@example.org>"
    for key, value in headers.items():
        message[key] = value
    message.set_content("I ordered four days ago. When will it ship?")
    return message


@pytest.mark.parametrize("headers", [
    {"Auto-Submitted": "auto-replied"}, {"Precedence": "bulk"},
    {"List-Id": "newsletter.example.org"}, {"Return-Path": "<>"},
    {"X-Auto-Customer-Support": "1"}, {"X-Auto-Response-Suppress": "All"},
])
def test_mail_suppresses_auto_reply_loops(headers):
    assert _ignore_message(customer_email(**headers))
    assert not _ignore_message(customer_email())


def test_html_mail_is_text_only_and_never_includes_script():
    message = EmailMessage()
    message.set_content("<p>Order 42</p><script>steal()</script><style>bad</style><p>Where is it?</p>", subtype="html")
    assert "Order 42" in _message_text(message)
    assert "Where is it?" in _message_text(message)
    assert "steal" not in _message_text(message)
    assert "<p>" not in _message_text(message)


def test_imap_uses_peek_and_uidvalidity_dedupe(monkeypatch):
    raw = customer_email(**{"References": "<original@example.org>", "Reply-To": "attacker@example.org"}).as_bytes()
    calls = []

    class FakeIMAP:
        validity = b"123"
        def __init__(self, *args, **kwargs):
            assert kwargs["ssl_context"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): pass
        def select(self, mailbox, readonly):
            assert readonly is True
            return "OK", [b"1"]
        def response(self, name):
            return name, [self.validity]
        def uid(self, command, *args):
            calls.append((command, args))
            if command == "search": return "OK", [b"7"]
            if args[1] == "(RFC822.SIZE)": return "OK", [f"1 (UID 7 RFC822.SIZE {len(raw)})".encode()]
            assert args[1] == "(BODY.PEEK[])"
            return "OK", [(b"1 (BODY[])", raw)]

    monkeypatch.setattr(mail_module.imaplib, "IMAP4_SSL", FakeIMAP)
    adapter = MailAdapter(settings())
    first = adapter.fetch_inbound()
    assert len(first) == 1
    assert first[0]["from_email"] == "customer@example.org"
    assert first[0]["thread_id"] == "<original@example.org>"
    assert adapter.fetch_inbound({first[0]["external_id"]}) == []
    FakeIMAP.validity = b"124"
    assert adapter.fetch_inbound({first[0]["external_id"]})[0]["external_id"] != first[0]["external_id"]
    assert not any(command == "store" for command, _ in calls)


def test_smtp_sending_is_explicit_and_preserves_outbox_threading(monkeypatch):
    delivered = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs): assert kwargs.get("context")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): pass
        def send_message(self, message, from_addr, to_addrs):
            delivered.append((message, from_addr, to_addrs))
            return {}

    monkeypatch.setattr(mail_module.smtplib, "SMTP_SSL", FakeSMTP)
    item = {"id": "outbox-1", "message_id": "<outbox-1@example.com>", "to_address": "customer@example.org", "subject": "Your answer", "body": "It ships tomorrow.", "in_reply_to": "<customer-message@example.org>", "references": "<original@example.org> <customer-message@example.org>"}
    with pytest.raises(ValueError, match="disabled"):
        MailAdapter(settings()).send(item)
    assert delivered == []
    result = MailAdapter(settings(email_delivery_enabled=True)).send(item)
    assert result["message_id"] == item["message_id"]
    sent, sender, recipients = delivered[0]
    assert sent["Message-ID"] == item["message_id"]
    assert sent["In-Reply-To"] == item["in_reply_to"]
    assert sent["Auto-Submitted"] == "auto-replied"
    assert sender == "support@example.com" and recipients == ["customer@example.org"]
    with pytest.raises(ValueError, match="header"):
        MailAdapter(settings(email_delivery_enabled=True)).send(item | {"subject": "Hello\r\nBcc: thief@example.org"})
    assert len(delivered) == 1


def test_explicit_smtp_rejection_is_distinct_from_unknown_delivery(monkeypatch):
    class FakeSMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): raise smtplib.SMTPAuthenticationError(535, b"No")

    monkeypatch.setattr(mail_module.smtplib, "SMTP_SSL", FakeSMTP)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        MailAdapter(settings(email_delivery_enabled=True)).send({"to_address": "customer@example.org", "subject": "Test", "body": "Hello"})
