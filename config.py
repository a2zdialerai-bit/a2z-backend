from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = (os.getenv(key) or "").strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_csv(key: str, default: str = "") -> List[str]:
    raw = (os.getenv(key) or default).strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("ENV", "local").strip() or "local"
    base_url: str = (os.getenv("BASE_URL", "http://127.0.0.1:8000").strip()).rstrip("/")
    frontend_url: str = (os.getenv("FRONTEND_URL", "http://localhost:3000").strip()).rstrip("/")

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///database.db").strip()

    jwt_secret: str = os.getenv("JWT_SECRET", "CHANGE_ME_USE_A_LONG_RANDOM_64+_CHARS").strip()
    jwt_alg: str = os.getenv("JWT_ALG", "HS256").strip()
    access_token_expire_minutes: int = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 43200)

    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    twilio_tts_voice: str = os.getenv("TWILIO_TTS_VOICE", "Polly.Joanna").strip()
    twilio_tts_language: str = os.getenv("TWILIO_TTS_LANGUAGE", "en-US").strip()
    twilio_gather_timeout: int = _env_int("TWILIO_GATHER_TIMEOUT", 6)
    twilio_speech_timeout: str = os.getenv("TWILIO_SPEECH_TIMEOUT", "auto").strip()
    twilio_speech_model: str = os.getenv("TWILIO_SPEECH_MODEL", "phone_call").strip()
    twilio_enhanced_speech: bool = _env_bool("TWILIO_ENHANCED_SPEECH", True)
    twilio_gather_barge_in: bool = _env_bool("TWILIO_GATHER_BARGE_IN", True)

    enable_sms_confirmation: bool = _env_bool("ENABLE_SMS_CONFIRMATION", True)
    twilio_sms_from_number: str = os.getenv("TWILIO_SMS_FROM_NUMBER", "").strip()

    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/New_York").strip()
    call_window_start: str = os.getenv("CALL_WINDOW_START", "09:00").strip()
    call_window_end: str = os.getenv("CALL_WINDOW_END", "19:00").strip()
    call_days: List[str] = None  # type: ignore[assignment]
    global_dnc_enabled: bool = _env_bool("GLOBAL_DNC_ENABLED", False)

    script_strict_mode: bool = _env_bool("SCRIPT_STRICT_MODE", False)
    min_speech_alnum: int = _env_int("MIN_SPEECH_ALNUM", 3)
    min_speech_confidence: float = _env_float("MIN_SPEECH_CONFIDENCE", 0.30)

    openai_phrasing_enabled: bool = _env_bool("OPENAI_PHRASING_ENABLED", True)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
    openai_rewrite_cache_max: int = _env_int("OPENAI_REWRITE_CACHE_MAX", 800)

    voice_mode_default: str = os.getenv("VOICE_MODE_DEFAULT", "realtime").strip()

    openai_realtime_enabled: bool = _env_bool("OPENAI_REALTIME_ENABLED", True)
    openai_realtime_model: str = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview").strip()
    openai_realtime_voice: str = os.getenv("OPENAI_REALTIME_VOICE", "alloy").strip()
    openai_realtime_temperature: float = _env_float("OPENAI_REALTIME_TEMPERATURE", 0.4)

    voice_clone_enabled: bool = _env_bool("VOICE_CLONE_ENABLED", True)
    voice_clone_provider: str = os.getenv("VOICE_CLONE_PROVIDER", "elevenlabs").strip()

    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "").strip()
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    elevenlabs_model_id: str = os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5").strip()

    stt_provider: str = os.getenv("STT_PROVIDER", "deepgram").strip()
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "").strip()

    appointment_mode_default: str = os.getenv("APPOINTMENT_MODE_DEFAULT", "google").strip()

    google_calendar_enabled: bool = _env_bool("GOOGLE_CALENDAR_ENABLED", True)
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    google_redirect_uri: str = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:8000/integrations/google/callback",
    ).strip()
    google_scopes: str = os.getenv(
        "GOOGLE_SCOPES",
        "https://www.googleapis.com/auth/calendar.events",
    ).strip()

    calendly_enabled: bool = _env_bool("CALENDLY_ENABLED", True)
    calendly_link_default: str = os.getenv(
        "CALENDLY_LINK_DEFAULT",
        "https://calendly.com/YOUR_HANDLE/15min",
    ).strip()
    calendly_personal_access_token: str = os.getenv("CALENDLY_PERSONAL_ACCESS_TOKEN", "").strip()
    calendly_webhook_signing_key: str = os.getenv("CALENDLY_WEBHOOK_SIGNING_KEY", "").strip()

    email_enabled: bool = _env_bool("EMAIL_ENABLED", False)
    email_provider: str = os.getenv("EMAIL_PROVIDER", "sendgrid").strip()
    sendgrid_api_key: str = os.getenv("SENDGRID_API_KEY", "").strip()
    email_from: str = os.getenv("EMAIL_FROM", "appointments@a2zdialer.com").strip()

    stripe_enabled: bool = _env_bool("STRIPE_ENABLED", False)
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "").strip()
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    stripe_price_id: str = os.getenv("STRIPE_PRICE_ID", "").strip()

    log_level: str = os.getenv("LOG_LEVEL", "info").strip()

    autopilot_enabled: bool = _env_bool("AUTOPILOT_ENABLED", True)
    autopilot_tick_seconds: int = _env_int("AUTOPILOT_TICK_SECONDS", 10)
    autopilot_limit_per_campaign: int = _env_int("AUTOPILOT_LIMIT_PER_CAMPAIGN", 3)
    worker_max_concurrency: int = _env_int("WORKER_MAX_CONCURRENCY", 5)

    # Marketplace fee — A2Z takes 40%, seller/agent gets 60%
    marketplace_transaction_fee_pct: float = _env_float("MARKETPLACE_TRANSACTION_FEE_PCT", 40.0)

    # Cartesia TTS
    cartesia_model_id: str = os.getenv("CARTESIA_MODEL_ID", "sonic-3").strip()
    cartesia_voice_id_default: str = os.getenv("CARTESIA_VOICE_ID_DEFAULT", "f786b574-daa5-4673-aa0c-cbe3e8534c02").strip()
    cartesia_voice_id_male: str = os.getenv("CARTESIA_VOICE_ID_MALE", "228fca29-3a0a-435c-8728-5cb483251068").strip()

    # Top Agent SMS follow-up
    top_agent_sms_follow_up_enabled: bool = _env_bool("TOP_AGENT_SMS_FOLLOW_UP_ENABLED", True)
    top_agent_sms_delay_minutes: int = _env_int("TOP_AGENT_SMS_DELAY_MINUTES", 10)

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_days", _env_csv("CALL_DAYS", "Mon,Tue,Wed,Thu,Fri,Sat"))

    @property
    def cors_origins(self) -> List[str]:
        origins = _env_csv("CORS_ORIGINS")
        if origins:
            return origins
        return [self.frontend_url]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()