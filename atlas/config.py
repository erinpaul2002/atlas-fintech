"""Env → typed settings. A missing secret crashes here, at import, naming the variable."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # required — no defaults, ever. ARCHITECTURE.md §11
    telegram_bot_token: str
    gemini_api_key: str
    fallback_llm_api_key: str
    mongodb_uri: str
    token_encryption_key: str
    sec_user_agent: str
    finnhub_api_key: str
    google_client_id: str
    google_client_secret: str
    # Local development sets PUBLIC_BASE_URL. Render supplies its public hostname
    # automatically, so production does not need a circular pre-deploy URL value.
    public_base_url: str = ""
    render_external_hostname: str = ""

    # Optional preview access control. If all three are empty, the bot remains open.
    allowed_telegram_users: str = ""
    allowed_chat_ids: str = ""
    tester_passcode: str = ""

    # tunables
    # Free tier is per-model: gemini-2.5-flash gives 20 requests/day (≈10 turns), flash-lite
    # gives 500. Quota, not quality, picks this until billing is on — then GEMINI_MODEL=
    # gemini-3.5-flash in the env is the whole upgrade.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Set empty (or equal to GEMINI_MODEL) to disable the multimodal failover tier.
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    transcription_api_key: str = ""
    transcription_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    transcription_model: str = "whisper-large-v3-turbo"
    fallback_model: str = "@cf/meta/llama-4-scout-17b-16e-instruct"
    fallback_url: str = "https://api.groq.com/openai/v1/chat/completions"
    embed_model: str = "gemini-embedding-001"
    mongodb_db: str = "atlas"
    port: int = 8080
    log_level: str = "INFO"

    @field_validator("allowed_telegram_users", "allowed_chat_ids")
    @classmethod
    def validate_telegram_ids(cls, value: str) -> str:
        for item in value.split(","):
            item = item.strip()
            if item:
                int(item)
        return value

    @property
    def allowed_telegram_user_ids(self) -> set[int]:
        return {int(item.strip()) for item in self.allowed_telegram_users.split(",") if item.strip()}

    @property
    def allowed_chat_id_values(self) -> set[int]:
        return {int(item.strip()) for item in self.allowed_chat_ids.split(",") if item.strip()}

    @property
    def access_control_enabled(self) -> bool:
        return bool(
            self.allowed_telegram_user_ids
            or self.allowed_chat_id_values
            or self.tester_passcode
        )

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.effective_public_base_url}/oauth/google/callback"

    @property
    def effective_public_base_url(self) -> str:
        if self.public_base_url.strip():
            return self.public_base_url.rstrip("/")
        if self.render_external_hostname.strip():
            return f"https://{self.render_external_hostname.strip().strip('/')}"
        raise ValueError("Set PUBLIC_BASE_URL or RENDER_EXTERNAL_HOSTNAME")


settings = Settings()
