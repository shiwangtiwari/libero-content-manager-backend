from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Supabase — required, must be set before deployment
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # Telegram — optional until Telegram bot is created (Step 4)
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # LinkedIn — optional until OAuth2 is completed (Step 5)
    LINKEDIN_CLIENT_ID: Optional[str] = None
    LINKEDIN_CLIENT_SECRET: Optional[str] = None
    LINKEDIN_ACCESS_TOKEN: Optional[str] = None
    LINKEDIN_PERSON_URN: Optional[str] = None

    # Playwright session cookies (JSON strings) — optional until Phase 2
    CLAUDE_COOKIES: Optional[str] = None
    CHATGPT_COOKIES: Optional[str] = None
    GEMINI_COOKIES: Optional[str] = None

    # Anthropic API — for content generation (Phase 2)
    ANTHROPIC_API_KEY: Optional[str] = None

    # GitHub Actions — for triggering content generation workflows
    GITHUB_PAT: Optional[str] = None
    GITHUB_REPO: Optional[str] = None
    RAILWAY_INTERNAL_SECRET: Optional[str] = None
    RAILWAY_CALLBACK_URL: Optional[str] = None

    # App
    PORT: int = 8000

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
