"""Central configuration for ALYO.

Every tunable lives here (env prefix ``ALYO_``) so nothing is hard-coded
throughout the application. Runtime-overridable values (retention periods,
privacy toggles) are layered on top by :mod:`config.store`, which persists
authenticated, versioned changes in the database.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALYO_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Runtime ---------------------------------------------------------------
    app_name: str = "J.A.R.V.S"
    app_version: str = "0.1.0"
    environment: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8760
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/alyo.db"

    # Authentication --------------------------------------------------------
    session_ttl_minutes: int = 720
    allow_open_registration: bool = True
    pbkdf2_iterations: int = 310_000
    login_rate_limit: int = 5
    login_rate_window: int = 60
    chat_rate_limit: int = 30
    chat_rate_window: int = 60

    # Social sign-in (OAuth 2.0 / OIDC) ------------------------------------
    # Empty = provider not configured: the API reports "Integration not
    # configured." with these exact variable names instead of faking a login.
    google_client_id: str = ""
    google_client_secret: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    # Create an account automatically on first social login (else link-only).
    oauth_auto_provision: bool = True
    # Public origin used for the OAuth redirect URI behind a reverse proxy;
    # empty = derive from the incoming request.
    public_base_url: str = ""
    # Endpoint overrides: use an OIDC-compliant server other than the default
    # (Keycloak, Authentik, Azure AD, verification servers). Empty = default.
    google_authorization_url: str = ""
    google_token_url: str = ""
    google_userinfo_url: str = ""
    linkedin_authorization_url: str = ""
    linkedin_token_url: str = ""
    linkedin_userinfo_url: str = ""

    # Brain -----------------------------------------------------------------
    wake_word: str = "hey jarvis"
    llm_provider: str = "local"  # local | openai
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    personality_mode: str = "friendly"
    history_window: int = 12

    # Retention (days) ------------------------------------------------------
    retention_conversations_days: int = 90
    retention_security_events_days: int = 180
    retention_critical_incident_days: int = 365
    retention_audit_days: int = 365
    retention_temp_days: int = 7
    retention_task_history_days: int = 30
    retention_worker_interval_minutes: int = 60

    # Security --------------------------------------------------------------
    auto_containment_enabled: bool = True
    high_security_plan_price: int = 699
    high_security_plan_period: str = "monthly"  # never hard-coded in the UI
    college_security_contact: str = ""
    it_security_contact: str = ""

    # Emergency (physical) --------------------------------------------------
    emergency_region: str = "IN"
    emergency_number: str = "112"

    # Privacy defaults ------------------------------------------------------
    conversation_history_enabled: bool = True
    long_term_memory_enabled: bool = True
    store_voice_recordings: bool = False

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    def resolved_database_url(self) -> str:
        """Anchor relative SQLite paths to the project root.

        Otherwise the database would be created in whatever directory the
        server happens to be started from.
        """
        url = self.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix):
            path = url[len(prefix) :]
            if path and not path.startswith(("/", "\\")) and ":" not in path[:3]:
                absolute = (PROJECT_ROOT / path).as_posix()
                return f"{prefix}{absolute}"
        return url


settings = Settings()
