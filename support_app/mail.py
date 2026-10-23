"""Bounded TLS mailbox ingestion and explicit SMTP delivery.

Fetching never marks messages seen. The application must persist the incoming
message and its external_id before treating it as processed. SMTP delivery is
at-least-once: a disconnect after server acceptance has an ambiguous outcome.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
import smtplib
import ssl
import uuid
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, parseaddr
from html.parser import HTMLParser

from .voice import ChannelUnavailable


def _secret(value) -> str:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")


def _header(value, name: str, maximum: int = 998) -> str:
    text = str(value or "")
    if "\r" in text or "\n" in text or "\x00" in text or len(text) > maximum:
        raise ValueError(f"Invalid {name} header.")
    return text


def _address(value, name: str) -> str:
    text = _header(value, name, 320)
    addresses = getaddresses([text])
    if len(addresses) != 1:
        raise ValueError(f"{name} must contain one email address.")
    address = addresses[0][1]
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address):
        raise ValueError(f"{name} must contain a valid email address.")
    return address


class _TextFromHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr", "hr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _message_text(message: EmailMessage, max_chars: int = 16000) -> str:
    """Extract one readable body; attachments and scripts are never executed."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None or part.get_content_disposition() == "attachment":
        return ""
    try:
        body = part.get_content()
    except (LookupError, UnicodeError, KeyError):
        raw = part.get_payload(decode=True) or b""
        body = raw.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return ""
    if part.get_content_type() == "text/html":
        parser = _TextFromHTML()
        parser.feed(body)
        body = "".join(parser.parts)
    body = body.replace("\x00", "").strip()
    return body[:max_chars]


def _ignore_message(message: EmailMessage, own_address: str = "") -> bool:
    auto = str(message.get("Auto-Submitted", "no")).lower().strip()
    precedence = str(message.get("Precedence", "")).lower().strip()
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    suppress = str(message.get("X-Auto-Response-Suppress", "")).lower()
    return (
        auto != "no"
        or precedence in {"bulk", "list", "junk"}
        or bool(message.get("List-Id"))
        or bool(message.get("X-Auto-Customer-Support"))
        or any(item.strip() in {"all", "autoreply", "oof"} for item in suppress.split(","))
        or message.get_content_type() == "multipart/report"
        or str(message.get("Return-Path", "")).strip() == "<>"
        or not sender
        or sender.split("@", 1)[0] in {"mailer-daemon", "postmaster"}
        or (bool(own_address) and sender == own_address.lower())
    )


class MailAdapter:
    def __init__(self, settings):
        self.settings = settings
        # Includes ignored loops, oversized items, and non-readable messages for
        # the worker to persist as handled without creating a customer response.
        self.ignored_external_ids: list[str] = []
        self._ignored: set[str] = set()

    def status(self) -> dict:
        return {
            "imap_configured": bool(getattr(self.settings, "imap_host", "") and getattr(self.settings, "imap_username", "")),
            "smtp_configured": bool(getattr(self.settings, "smtp_host", "") and getattr(self.settings, "smtp_from", "")),
            "delivery_enabled": bool(getattr(self.settings, "email_delivery_enabled", False)),
            "connectivity_checked": False,
        }

    def fetch_inbound(self, known_external_ids: set[str] | None = None) -> list[dict]:
        host = str(getattr(self.settings, "imap_host", "") or "")
        username = str(getattr(self.settings, "imap_username", "") or "")
        if not host or not username:
            raise ChannelUnavailable("Configure IMAP_HOST and IMAP_USERNAME before polling email.")
        known = (known_external_ids or set()) | self._ignored
        mailbox = str(getattr(self.settings, "imap_mailbox", "INBOX"))
        own_address = parseaddr(str(getattr(self.settings, "smtp_from", "") or ""))[1]
        account_id = hashlib.sha256(f"{host}:{username}:{mailbox}".encode()).hexdigest()[:20]
        max_bytes = max(1024, min(int(getattr(self.settings, "email_max_bytes", 2 * 1024 * 1024)), 10 * 1024 * 1024))
        limit = max(1, min(int(getattr(self.settings, "imap_batch_size", 30)), 100))
        results = []
        self.ignored_external_ids = []
        try:
            with imaplib.IMAP4_SSL(
                host, int(getattr(self.settings, "imap_port", 993)),
                ssl_context=ssl.create_default_context(), timeout=20,
            ) as client:
                client.login(username, _secret(getattr(self.settings, "imap_password", "")))
                status, _ = client.select(mailbox, readonly=True)
                if status != "OK":
                    raise ChannelUnavailable("The configured email mailbox could not be selected.")
                _, validity_data = client.response("UIDVALIDITY")
                if not validity_data or not validity_data[0]:
                    raise ChannelUnavailable("The mailbox did not provide UIDVALIDITY; ingestion was stopped.")
                validity = validity_data[0].decode("ascii")
                if not validity.isdigit():
                    raise ChannelUnavailable("The mailbox returned invalid UIDVALIDITY.")
                status, uid_data = client.uid("search", None, "UNSEEN")
                if status != "OK":
                    raise ChannelUnavailable("Email search failed.")
                uids = (uid_data[0] or b"").split() if uid_data else []
                # Newest-first makes incoming messages visible promptly. A caller
                # should pass persisted handled IDs to drain older unread mail.
                scanned = 0
                for uid in reversed(uids):
                    uid_text = uid.decode("ascii")
                    if not uid_text.isdigit():
                        continue
                    external_id = f"imap:{account_id}:{validity}:{uid_text}"
                    if external_id in known:
                        continue
                    scanned += 1
                    if scanned > limit * 5 or len(results) >= limit:
                        break
                    status, size_data = client.uid("fetch", uid, "(RFC822.SIZE)")
                    if status != "OK":
                        continue
                    size_meta = b" ".join(part for part in size_data if isinstance(part, bytes))
                    size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", size_meta, re.I)
                    # Refuse messages whose size cannot be bounded before fetching.
                    if not size_match or int(size_match.group(1)) > max_bytes:
                        self.ignored_external_ids.append(external_id)
                        continue
                    status, chunks = client.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue
                    raw = b"".join(item[1] for item in chunks if isinstance(item, tuple) and isinstance(item[1], bytes))
                    if not raw or len(raw) > max_bytes:
                        self.ignored_external_ids.append(external_id)
                        continue
                    message = BytesParser(policy=policy.default).parsebytes(raw)
                    if _ignore_message(message, own_address):
                        self.ignored_external_ids.append(external_id)
                        continue
                    name, sender = parseaddr(str(message.get("From", "")))
                    try:
                        sender = _address(sender, "From")
                    except ValueError:
                        self.ignored_external_ids.append(external_id)
                        continue
                    body = _message_text(message)
                    if not body:
                        self.ignored_external_ids.append(external_id)
                        continue
                    message_id = str(message.get("Message-ID", "")).strip()[:998]
                    in_reply_to = str(message.get("In-Reply-To", "")).strip()[:998]
                    references = str(message.get("References", "")).strip()[:4000]
                    ids = re.findall(r"<[^<>\s]+>", references)
                    thread_id = (ids[0] if ids else in_reply_to or message_id or external_id)
                    results.append({
                        "external_id": external_id, "thread_id": thread_id,
                        "from_email": sender.lower(), "from_name": name[:200],
                        "subject": str(message.get("Subject", "(no subject)"))[:250],
                        "text": body, "message_id": message_id,
                        "in_reply_to": in_reply_to, "references": references,
                        "reply_to": str(message.get("Reply-To", ""))[:998],
                        "uid": uid_text, "uidvalidity": validity, "mailbox": mailbox,
                    })
        except ChannelUnavailable:
            raise
        except (imaplib.IMAP4.error, OSError, UnicodeError, ValueError) as exc:
            raise ChannelUnavailable("Email polling failed; check the IMAP connection and credentials.") from exc
        self._ignored.update(self.ignored_external_ids)
        # This cache is only an efficiency optimization. Durable handled IDs
        # belong to the application store, which can consume ignored_external_ids.
        if len(self._ignored) > 10000:
            self._ignored = set(self.ignored_external_ids)
        return results

    def send(self, message: dict) -> dict:
        """Explicitly send one persisted outbox item; never called during ingest."""
        if not getattr(self.settings, "email_delivery_enabled", False):
            raise ValueError("Email delivery is disabled; the reply remains in the outbox.")
        host = str(getattr(self.settings, "smtp_host", "") or "")
        sender = str(getattr(self.settings, "smtp_from", "") or "")
        if not host or not sender:
            raise ValueError("Configure SMTP_HOST and SMTP_FROM before sending email.")
        sender = _address(sender, "From")
        recipient = _address(message.get("to_address", ""), "To")
        subject = _header(message.get("subject", "Support follow-up"), "Subject", 250)
        body = str(message.get("body", ""))
        if not body.strip() or len(body) > 50000 or "\x00" in body:
            raise ValueError("Email body is empty or too large.")
        security = str(getattr(self.settings, "smtp_security", "ssl"))
        if security not in {"ssl", "starttls"}:
            raise ValueError("SMTP_SECURITY must be ssl or starttls; plaintext SMTP is disabled.")
        mail = EmailMessage(policy=policy.SMTP)
        mail["From"] = sender
        mail["To"] = recipient
        mail["Subject"] = subject
        mail["Date"] = formatdate(usegmt=True)
        identifier = str(message.get("id") or uuid.uuid4())
        message_id = str(message.get("message_id") or f"<{uuid.uuid5(uuid.NAMESPACE_URL, 'auto-customer-support:' + identifier)}@{sender.rsplit('@', 1)[1]}>")
        message_id = _header(message_id, "Message-ID")
        if not re.fullmatch(r"<[^<>\s]+>", message_id):
            raise ValueError("Invalid Message-ID header.")
        mail["Message-ID"] = message_id
        mail["Auto-Submitted"] = "auto-replied"
        mail["X-Auto-Response-Suppress"] = "All"
        mail["X-Auto-Customer-Support"] = "1"
        for key, field in (("In-Reply-To", "in_reply_to"), ("References", "references")):
            if message.get(field):
                value = _header(message[field], key, 4000)
                if not re.fullmatch(r"(?:<[^<>\s]+>\s*)+", value):
                    raise ValueError(f"Invalid {key} message identifier.")
                mail[key] = value
        mail.set_content(body)
        context = ssl.create_default_context()
        port = int(getattr(self.settings, "smtp_port", 465))
        try:
            if security == "ssl":
                connection = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
            else:
                connection = smtplib.SMTP(host, port, timeout=20)
            with connection as client:
                if security == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                username = str(getattr(self.settings, "smtp_username", "") or "")
                if username:
                    client.login(username, _secret(getattr(self.settings, "smtp_password", "")))
                refused = client.send_message(mail, from_addr=sender, to_addrs=[recipient])
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            # The server explicitly rejected the attempt. The outbox can classify
            # this as failed, rather than ambiguous acceptance.
            raise
        except ChannelUnavailable:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            raise ChannelUnavailable("SMTP delivery failed or its outcome is unknown. Review before retrying.") from exc
        return {"message_id": message_id, "accepted": True}
