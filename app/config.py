"""Runtime configuration, read from the environment.

`python-dotenv` is loaded here for *local, non-Docker* development (a
developer's `.env` next to the repo). Under docker-compose the same
variables arrive as real environment variables via the compose file's
`env_file`/`environment` keys, so `load_dotenv()` finds nothing and the
container's env wins.

Credentials (DATABASE_URL included) are never hardcoded here -- they must
come from `.env` (see `.env.example`). Set DATABASE_URL to a SQLite URL
(e.g. sqlite:///./chat.db) to run the API with no database server at all --
app.session's schema is backend-agnostic and creates itself.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    redis_url: str
    database_url: str
    groq_api_key: str | None
    tavily_api_key: str | None


def load_config() -> Config:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to .env (see .env.example)."
        )
    return Config(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        database_url=database_url,
        groq_api_key=os.environ.get("GROQ_API_KEY"),
        tavily_api_key=os.environ.get("TAVILY_API_KEY"),
    )
