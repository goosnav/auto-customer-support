"""Configuration is operator-owned, never writable by the language model."""

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    business_name: str = "Your business"
    greeting: str = "Hello. I'm the automated support assistant. How can I help?"
    data_dir: Path = Path("data")
    knowledge_dir: Path = Path("knowledge")
    admin_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    allowed_origins: str = ""
    owner_email: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    llm_timeout_seconds: float = Field(35, ge=1, le=120)
    followup_hours: int = Field(24, ge=1, le=720)
    max_message_chars: int = Field(6000, ge=500, le=20000)
    rate_limit_per_minute: int = Field(30, ge=1, le=300)
    max_conversation_messages: int = Field(200, ge=10, le=2000)
    # The public API never accepts an identity_verified flag.
    orders_file: str = ""
    orders_api_url: str = ""
    orders_api_token: str = ""

    stt_enabled: bool = False
    whisper_model: str = "small.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    tts_url: str = ""
    tts_voice: str = "af_heart"
    voice_max_seconds: int = Field(45, ge=5, le=120)
    voice_max_bytes: int = Field(8 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    voice_timeout_seconds: float = Field(60, ge=1, le=120)

    phone_enabled: bool = False
    twilio_auth_token: str = ""
    phone_turn_timeout_seconds: float = Field(8, ge=1, le=10)

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_batch_size: int = 30
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_security: str = "ssl"
    email_delivery_enabled: bool = False
    email_auto_send: bool = False
    worker_interval_seconds: int = Field(30, ge=5, le=3600)
    worker_enabled: bool = True

    @model_validator(mode="after")
    def validate_settings(self):
        if self.admin_token and len(self.admin_token) < 32:
            raise ValueError("ADMIN_TOKEN must contain at least 32 characters. Run support-desk init.")
        for key in ("business_name", "owner_email", "smtp_from"):
            if any(c in getattr(self, key) for c in ("\r", "\n")):
                raise ValueError(f"{key} must not contain line breaks")
        for key in ("ollama_url", "tts_url", "orders_api_url", "public_base_url"):
            value = getattr(self, key)
            if value:
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError(f"{key} must be an http(s) URL without embedded credentials")
        if self.phone_enabled and (not self.twilio_auth_token or not self.public_base_url.startswith("https://")):
            raise ValueError("Phone support requires TWILIO_AUTH_TOKEN and an HTTPS PUBLIC_BASE_URL")
        if self.email_auto_send and not self.email_delivery_enabled:
            raise ValueError("EMAIL_AUTO_SEND requires EMAIL_DELIVERY_ENABLED=true")
        if self.smtp_security not in {"ssl", "starttls"}:
            raise ValueError("SMTP_SECURITY must be ssl or starttls")
        return self

    @property
    def origins(self) -> list[str]:
        return [x.strip().rstrip("/") for x in self.allowed_origins.split(",") if x.strip()]
