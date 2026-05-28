"""
Központi konfiguráció.

Betölti a környezeti változókat a .env fájlból (fejlesztésnél) vagy a
Railway environment variables-ből (élesben), és egyetlen helyen elérhetővé
teszi őket a kód számára.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Fejlesztésnél a .env fájlt tölti be; Railway-en a változók már a környezetben vannak.
load_dotenv()


def _required(key: str) -> str:
    """Kötelező változó beolvasása — ha hiányzik, érthető hibát dob."""
    value = os.getenv(key)
    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Hiányzó környezeti változó: {key}. "
            f"Ellenőrizd a .env fájlt (vagy a Railway beállításokat)."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Config:
    # ---- Supabase ----
    supabase_url: str
    supabase_service_key: str

    # ---- Discord ----
    discord_bot_token: str
    discord_guild_id: str
    discord_admin_channel_id: str

    # ---- Anthropic ----
    anthropic_api_key: str
    claude_model: str

    # ---- Meta Ads ----
    meta_app_id: str
    meta_app_secret: str
    meta_access_token: str

    # ---- Google Ads ----
    google_ads_developer_token: str
    google_ads_client_id: str
    google_ads_client_secret: str
    google_ads_refresh_token: str
    google_ads_login_customer_id: str

    # ---- ClickUp ----
    clickup_api_token: str
    clickup_team_id: str
    clickup_default_list_id: str

    # ---- Általános ----
    timezone: str
    quiet_hours_start: int
    quiet_hours_end: int
    log_level: str

    @classmethod
    def load(cls) -> "Config":
        """
        Betölti a teljes konfigurációt.

        A Supabase és Discord kulcsok kötelezőek (enélkül a bot el sem indul).
        A többi integráció kulcsai opcionálisak fejlesztés közben — így tudunk
        mock adatokkal dolgozni, mielőtt minden külső API be van kötve.
        """
        return cls(
            # Kötelező az alapműködéshez
            supabase_url=_required("SUPABASE_URL"),
            supabase_service_key=_required("SUPABASE_SERVICE_KEY"),
            discord_bot_token=_required("DISCORD_BOT_TOKEN"),
            discord_guild_id=_required("DISCORD_GUILD_ID"),
            discord_admin_channel_id=_optional("DISCORD_ADMIN_CHANNEL_ID"),

            # Opcionális fejlesztés közben (mock adatok mellett)
            anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
            claude_model=_optional("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),

            meta_app_id=_optional("META_APP_ID"),
            meta_app_secret=_optional("META_APP_SECRET"),
            meta_access_token=_optional("META_ACCESS_TOKEN"),

            google_ads_developer_token=_optional("GOOGLE_ADS_DEVELOPER_TOKEN"),
            google_ads_client_id=_optional("GOOGLE_ADS_CLIENT_ID"),
            google_ads_client_secret=_optional("GOOGLE_ADS_CLIENT_SECRET"),
            google_ads_refresh_token=_optional("GOOGLE_ADS_REFRESH_TOKEN"),
            google_ads_login_customer_id=_optional("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),

            clickup_api_token=_optional("CLICKUP_API_TOKEN"),
            clickup_team_id=_optional("CLICKUP_TEAM_ID"),
            clickup_default_list_id=_optional("CLICKUP_DEFAULT_LIST_ID"),

            timezone=_optional("TIMEZONE", "Europe/Budapest"),
            quiet_hours_start=_int("QUIET_HOURS_START", 18),
            quiet_hours_end=_int("QUIET_HOURS_END", 9),
            log_level=_optional("LOG_LEVEL", "INFO"),
        )


# Egyetlen, az egész alkalmazásban használt konfig példány.
# Lazán töltődik be, hogy a tesztek felül tudják írni a környezetet előbb.
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config
