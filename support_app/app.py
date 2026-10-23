"""Authenticated operator console and capability-token customer API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .knowledge import KnowledgeBase, MAX_DOCUMENT_BYTES
from .phone import build_phone_router
from .service import SupportService, valid_email
from .store import Store
from .voice import ChannelUnavailable, VoiceService

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PROJECT = Path(__file__).resolve().parent.parent


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=254)

    @field_validator("email")
    @classmethod
    def check_email(cls, value):
        if value is not None:
            value = value.strip().lower()
            if not valid_email(value):
                raise ValueError("Enter a valid email address")
        return value


class CustomerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20000)
    ordered_days_ago: int | None = Field(None, ge=0, le=3650)
    client_message_id: str | None = Field(None, pattern=r"^[a-zA-Z0-9_-]{8,100}$")


class SimulateMessage(CustomerMessage):
    conversation_id: str | None = None
    customer_email: str = ""
    customer_name: str = ""

    @field_validator("customer_email")
    @classmethod
    def check_email(cls, value):
        if not valid_email(value):
            raise ValueError("Enter a valid email address")
        return value.lower()


class OwnerReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=12000)
    resolve: bool = False


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "waiting_customer", "resolved"]


class RateLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.buckets = OrderedDict()

    def accept(self, key):
        stamp = time.monotonic()
        bucket = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        while bucket and bucket[0] <= stamp - 60:
            bucket.popleft()
        allowed = len(bucket) < self.limit
        if allowed:
            bucket.append(stamp)
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)
        return allowed


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if not settings.admin_token:
        raise RuntimeError("ADMIN_TOKEN is missing. Run `support-desk init`, then `support-desk serve`.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.data_dir.chmod(0o700)
    except OSError:
        pass
    store = Store(settings.data_dir / "support.sqlite3")
    store.recover_interrupted_deliveries()
    knowledge = KnowledgeBase(settings.knowledge_dir)
    service = SupportService(settings, store, knowledge)
    voice = VoiceService(settings)
    audio_cache = OrderedDict()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    worker_health = {"last_run": None, "last_error": None}

    async def worker_loop():
        while True:
            try:
                await service.worker_once()
                worker_health.update(last_run=time.time(), last_error=None)
            except Exception as exc:
                worker_health["last_error"] = type(exc).__name__ + ": check local worker/mail settings"
                logger.warning("Support worker failed (%s)", type(exc).__name__)
            await asyncio.sleep(settings.worker_interval_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker_loop()) if settings.worker_enabled else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Auto Customer Support", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.knowledge, app.state.service, app.state.voice = store, knowledge, service, voice
    app.state.settings = settings
    if settings.origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            # Do not trust caller-supplied X-Forwarded-For. Configure a trusted reverse proxy explicitly.
            peer = request.client.host if request.client else "unknown"
            supplied = request.headers.get("Authorization", "")
            trusted_admin = path.startswith("/api/admin/") and secrets.compare_digest(supplied, "Bearer " + settings.admin_token)
            if not trusted_admin and not limiter.accept(peer):
                return JSONResponse({"detail": "Too many requests. Please wait a minute."}, 429,
                                    headers={"Retry-After": "60"})
            maximum = settings.voice_max_bytes + 256 * 1024 if path.endswith("/voice") else 2 * 1024 * 1024
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    return JSONResponse({"detail": "Request is too large."}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        ancestors = "'self' " + " ".join(settings.origins) if path == "/widget" else "'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
            f"frame-ancestors {ancestors}; form-action 'self'"
        )
        if path.startswith("/api/") or path in {"/", "/widget"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Not found."}, 404)

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    def bearer(authorization):
        return authorization[7:] if authorization and authorization.startswith("Bearer ") else ""

    async def admin(authorization: Annotated[str | None, Header()] = None):
        if not secrets.compare_digest(bearer(authorization), settings.admin_token):
            raise HTTPException(401, "Enter the admin token from your local .env file.", headers={"WWW-Authenticate": "Bearer"})

    async def customer(conversation_id: str, authorization: Annotated[str | None, Header()] = None):
        if not store.authorize(conversation_id, bearer(authorization)):
            raise HTTPException(401, "This conversation requires its private access token.")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/widget")
    async def widget():
        return FileResponse(STATIC / "widget.html")

    @app.get("/widget.js")
    async def widget_loader():
        return FileResponse(STATIC / "widget.js", media_type="text/javascript")

    @app.get("/api/config")
    async def public_config():
        status = voice.status()
        return {"business_name": settings.business_name, "greeting": settings.greeting,
                "voice_enabled": status["stt"]["enabled"] and status["stt"]["installed"]}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(contact: Contact):
        result = store.create_conversation(name=contact.name or "", email=contact.email or "")
        return {"id": result["id"], "token": result["token"]}

    @app.patch("/api/conversations/{conversation_id}", dependencies=[Depends(customer)])
    async def update_contact(conversation_id: str, contact: Contact):
        result = store.update_contact(conversation_id, name=contact.name, email=contact.email)
        return {"id": result["id"], "name": result["customer_name"], "email": result["customer_email"]}

    @app.get("/api/conversations/{conversation_id}/messages", dependencies=[Depends(customer)])
    async def public_messages(conversation_id: str):
        return {"messages": store.messages(conversation_id)}

    @app.post("/api/conversations/{conversation_id}/messages", dependencies=[Depends(customer)])
    async def public_message(conversation_id: str, message: CustomerMessage):
        key = f"web:{conversation_id}:{message.client_message_id}" if message.client_message_id else None
        return await service.respond(conversation_id, message.message, ordered_days_ago=message.ordered_days_ago, event_key=key)

    def cache_audio(data):
        stamp = time.monotonic()
        for key in list(audio_cache):
            if audio_cache[key][0] < stamp:
                del audio_cache[key]
        key = secrets.token_urlsafe(32)
        audio_cache[key] = (stamp + 300, data)
        # Short-lived bearer URL: at most 24 clips and 32 MiB in RAM, never on disk.
        while len(audio_cache) > 24 or sum(len(v[1]) for v in audio_cache.values()) > 32 * 1024 * 1024:
            audio_cache.popitem(last=False)
        return "/api/audio/" + key

    @app.post("/api/conversations/{conversation_id}/voice", dependencies=[Depends(customer)])
    async def public_voice(conversation_id: str, audio: Annotated[UploadFile, File()]):
        try:
            raw = await audio.read(settings.voice_max_bytes + 1)
            if len(raw) > settings.voice_max_bytes:
                raise HTTPException(413, "Recording is too large.")
            transcript = await voice.transcribe(raw, Path(audio.filename or "recording.webm").suffix.lower())
        except ChannelUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            await audio.close()
        result = await service.respond(conversation_id, transcript)
        result.update(transcript=transcript, audio_url=None)
        try:
            spoken = await voice.synthesize(result["reply"][:2000])
            result["audio_url"] = cache_audio(spoken)
        except (ChannelUnavailable, ValueError):
            result["audio_notice"] = "Your answer is available as text. Local speech output is not available."
        return result

    @app.get("/api/audio/{audio_token}")
    async def read_audio(audio_token: str):
        cached = audio_cache.get(audio_token)
        if not cached or cached[0] < time.monotonic():
            audio_cache.pop(audio_token, None)
            raise HTTPException(404, "Audio expired.")
        return Response(cached[1], media_type="audio/wav")

    @app.get("/api/admin/status", dependencies=[Depends(admin)])
    async def admin_status():
        model_available = False
        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                response = await client.get(settings.ollama_url.rstrip("/") + "/api/tags")
                response.raise_for_status()
                names = {m.get("name", "") for m in response.json().get("models", [])}
                model_available = settings.ollama_model in names or settings.ollama_model + ":latest" in names
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        documents = knowledge.list_documents()
        return {"business_name": settings.business_name,
                "model": {"provider": "ollama", "name": settings.ollama_model, "available": model_available},
                "voice": voice.status(),
                "channels": {"web": True, "email": bool(settings.imap_host), "phone": settings.phone_enabled},
                "counts": {**store.counts(), "documents_approved": sum(x["status"] == "approved" for x in documents)},
                "email_delivery_enabled": settings.email_delivery_enabled, "email_auto_send": settings.email_auto_send,
                "mail": service.mail.status(), "worker": {"enabled": settings.worker_enabled, **worker_health},
                "public_base_url": settings.public_base_url, "allowed_origins": settings.origins}

    @app.get("/api/admin/conversations", dependencies=[Depends(admin)])
    async def conversations():
        return {"items": store.conversations()}

    @app.get("/api/admin/conversations/{conversation_id}", dependencies=[Depends(admin)])
    async def conversation(conversation_id: str):
        item = store.conversation(conversation_id)
        if item is None:
            raise HTTPException(404, "Conversation not found.")
        return {**item, "messages": store.messages(conversation_id), "ticket": store.active_ticket(conversation_id)}

    @app.get("/api/admin/tickets", dependencies=[Depends(admin)])
    async def tickets():
        return {"items": store.tickets()}

    @app.post("/api/admin/tickets/{ticket_id}/reply", dependencies=[Depends(admin)])
    async def ticket_reply(ticket_id: str, payload: OwnerReply):
        if not payload.message.strip():
            raise ValueError("Reply cannot be blank")
        return store.owner_reply(ticket_id, payload.message.strip(), payload.resolve, service.mail_domain)

    @app.patch("/api/admin/tickets/{ticket_id}", dependencies=[Depends(admin)])
    async def ticket_update(ticket_id: str, payload: TicketUpdate):
        return store.update_ticket(ticket_id, payload.status)

    @app.get("/api/admin/knowledge", dependencies=[Depends(admin)])
    async def documents():
        return {"items": knowledge.list_documents()}

    @app.post("/api/admin/knowledge/upload", dependencies=[Depends(admin)])
    async def upload_knowledge(files: Annotated[list[UploadFile], File()]):
        if not 1 <= len(files) <= 20:
            raise ValueError("Upload between 1 and 20 Markdown files at a time.")
        validated = []
        try:
            for upload in files:
                name = upload.filename or ""
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,100}\.md", name) or ".." in name:
                    raise ValueError("Use a simple .md filename without directories or special characters.")
                raw = await upload.read(MAX_DOCUMENT_BYTES + 1)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise ValueError("Each Markdown file must be 128 KiB or smaller.")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("Knowledge files must be UTF-8 Markdown.") from exc
                if not text.strip() or "\x00" in text:
                    raise ValueError("Knowledge files must contain readable Markdown text.")
                validated.append((name, text))
            for name, text in validated:
                target = knowledge.inbox / (secrets.token_hex(4) + "-" + name)
                with target.open("x", encoding="utf-8") as stream:
                    stream.write(text)
            result = knowledge.ingest()
            store.audit("knowledge_upload", str(len(validated)))
            return result
        finally:
            for upload in files:
                await upload.close()

    @app.post("/api/admin/knowledge/ingest", dependencies=[Depends(admin)])
    async def ingest():
        return knowledge.ingest()

    @app.get("/api/admin/knowledge/{document_id}", dependencies=[Depends(admin)])
    async def document(document_id: str):
        return knowledge.read_document(document_id)

    @app.post("/api/admin/knowledge/{document_id}/approve", dependencies=[Depends(admin)])
    async def approve(document_id: str):
        result = knowledge.approve(document_id)
        store.audit("knowledge_approved", document_id)
        return result

    @app.post("/api/admin/knowledge/{document_id}/archive", dependencies=[Depends(admin)])
    async def archive(document_id: str):
        result = knowledge.archive(document_id)
        store.audit("knowledge_archived", document_id)
        return result

    @app.get("/api/admin/outbox", dependencies=[Depends(admin)])
    async def outbox():
        return {"items": store.outbox()}

    @app.post("/api/admin/outbox/{outbox_id}/send", dependencies=[Depends(admin)])
    async def send(outbox_id: str):
        return await service.send_outbox(outbox_id)

    @app.post("/api/admin/simulate", dependencies=[Depends(admin)])
    async def simulate(payload: SimulateMessage):
        if payload.conversation_id:
            cid = payload.conversation_id
            conv = store.conversation(cid)
            if not conv or conv["channel"] != "simulation":
                raise ValueError("The simulator can only continue its own test conversations.")
        else:
            cid = store.create_conversation(channel="simulation", name=payload.customer_name, email=payload.customer_email)["id"]
        key = f"sim:{cid}:{payload.client_message_id}" if payload.client_message_id else None
        return await service.respond(cid, payload.message, ordered_days_ago=payload.ordered_days_ago, event_key=key)

    @app.get("/api/admin/onboarding-prompt", dependencies=[Depends(admin)])
    async def onboarding_prompt():
        path = PROJECT / "prompts" / "onboarding.md"
        if not path.exists():
            raise HTTPException(404, "See prompts/onboarding.md in the repository.")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    app.include_router(build_phone_router(settings, service))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
