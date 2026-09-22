from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str
    gemini_model: str = "gemini-3.6-flash"
    gemini_tts_model: str = "gemini-3.1-flash-tts-preview"
    vapid_public_key: str
    vapid_private_key: str
    vapid_claims_email: str = "mailto:admin@example.com"
    app_secret: str
    responder_invite_codes: str = "KTF-2026"
    public_base_url: str = "http://localhost:8000"
    mongodb_uri: str
    mongodb_db: str = "saferoad"
    alert_ttl_hours: int = 6
    timezone: str = "Africa/Lagos"

    @property
    def invite_codes(self) -> set[str]:
        return {c.strip().upper() for c in self.responder_invite_codes.split(",") if c.strip()}


settings = Settings()

LANGUAGES = {
    "en": "English",
    "yo": "Yoruba",
    "ha": "Hausa",
    "ig": "Igbo",
    "pcm": "Nigerian Pidgin",
}
