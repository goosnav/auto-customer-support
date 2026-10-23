"""Opt-in Twilio phone adapter. Gather/Say speech is hosted by Twilio."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.datastructures import FormData

logger = logging.getLogger(__name__)
ROUTE = "/api/phone/twilio"


def _secret(value) -> str:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")


def _twiml(reply: str, action: str | None) -> Response:
    root = Element("Response")
    if action:
        target = SubElement(root, "Gather", {
            "input": "speech", "action": action, "method": "POST",
            "language": "en-US", "speechTimeout": "2", "timeout": "6",
            "actionOnEmptyResult": "true",
        })
    else:
        target = root
    SubElement(target, "Say", {"language": "en-US"}).text = reply
    if action is None:
        SubElement(root, "Hangup")
    return Response(tostring(root, encoding="utf-8", xml_declaration=True), media_type="application/xml")


def build_phone_router(settings, service) -> APIRouter:
    router = APIRouter()

    @router.post(ROUTE)
    async def phone(
        request: Request, turn: int = Query(0, ge=0, le=100),
        empty: int = Query(0, ge=0, le=3),
    ):
        token = _secret(getattr(settings, "twilio_auth_token", ""))
        if not getattr(settings, "phone_enabled", False) or not token:
            raise HTTPException(503, "Phone support is not enabled.")
        base = str(getattr(settings, "public_base_url", "") or "").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise HTTPException(503, "Phone support requires a configured public HTTPS URL.")
        try:
            from twilio.request_validator import RequestValidator
        except ImportError as exc:
            raise HTTPException(503, "Install the phone extra to enable Twilio support.") from exc
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Phone callbacks must be form encoded.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 64 * 1024:
                raise HTTPException(413, "Phone callback is too large.")
        try:
            params = FormData(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=100))
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "Invalid phone callback form.") from exc
        canonical_url = base + ROUTE
        if request.url.query:
            canonical_url += "?" + request.url.query
        signature = request.headers.get("X-Twilio-Signature", "")
        if not signature or not RequestValidator(token).validate(canonical_url, params, signature):
            raise HTTPException(403, "Invalid phone callback signature.")
        call_sid = str(params.get("CallSid", ""))
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
            raise HTTPException(400, "Missing or invalid call identifier.")
        speech = str(params.get("SpeechResult", "")).strip()
        if len(speech) > 8000:
            raise HTTPException(413, "Speech transcript is too large.")
        if turn >= 100:
            return _twiml("This support call has reached its time limit. Please continue through website chat.", None)
        next_action = base + ROUTE + f"?turn={turn + 1}"
        if not speech:
            if empty >= 2:
                return _twiml("I could not hear a question. Please call again or use website chat when you are ready.", None)
            if turn == 0:
                name = str(getattr(settings, "business_name", "the business"))
                greeting = getattr(settings, "greeting", "")
                reply = f"Hello, you have reached {name}. I am an automated support assistant. "
                reply += str(greeting or "How can I help you?")
            else:
                reply = "I did not hear a question. Please tell me what you need help with."
            return _twiml(reply[:2000], next_action + f"&empty={empty + (turn > 0)}")
        try:
            result = await asyncio.wait_for(
                service.phone_turn(
                    call_sid=call_sid, speech=speech,
                    caller=str(params.get("From", ""))[:100],
                    event_key=f"{call_sid}:{turn}",
                ),
                # The support service owns its shorter model deadline and needs
                # time to persist a fallback ticket after that deadline expires.
                timeout=float(getattr(settings, "phone_turn_timeout_seconds", 8)) + 3,
            )
            reply = result.get("reply", "")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Missing support reply")
        except Exception as exc:
            logger.warning("Phone support turn failed (%s)", type(exc).__name__)
            return _twiml(
                "I could not complete that request right now. Please use website chat or contact the business directly.",
                None,
            )
        return _twiml(reply[:2500], next_action)

    return router
