"""Service configuration. Single Settings class, read from env (and an
optional agentic/backend/.env).

Leaf module: imports nothing from the rest of the service. The provider
API keys llm.py's get_chat_model() needs (GROQ_API_KEY, ANTHROPIC_API_KEY,
...) are deliberately NOT modeled as fields here -- each provider's own
LangChain integration already reads its own conventional env var
automatically (see llm.py's module docstring), so load_dotenv() below
just needs to get them into os.environ, not into this class.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# load_dotenv() with no path walks up from THIS file's location looking
# for a .env -- i.e. agentic/backend/.env, found before it would ever
# reach a repo-root .env. That makes it independent of the process's
# current working directory, which matters here: this service is run
# both as `cd agentic/backend/src && uvicorn main:create_app --factory`
# and as `cd agentic/backend && pytest`.
load_dotenv()

# pydantic-settings' own env_file loading (for the fields below) DOES
# resolve relative to CWD unless given an absolute path -- so, unlike
# load_dotenv() above, this one is computed explicitly.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Checked against the client's X-API-Key header on every route except
    # /health (see dependencies.py). No default -- an unset key must fail
    # closed, never be silently treated as "auth is off."
    service_api_key: str | None = None
