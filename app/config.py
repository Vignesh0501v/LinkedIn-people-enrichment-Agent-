"""Runtime configuration, read from the environment.

`python-dotenv` is loaded here for *local, non-Docker* development (a
developer's `.env` next to the repo). Under docker-compose the same
variables arrive as real environment variables via the compose file's
`env_file`/`environment` keys, so `load_dotenv()` finds nothing and the
container's env wins -- which is why the defaults below stay `localhost`-
based: they describe a laptop, and Docker overrides them with service names
(`redis`, `postgres`) instead of this module knowing about containers.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Local non-Docker dev default: a Postgres on the developer's machine (or
# the one docker-compose publishes on 5433 -- remapped off the standard 5432
# to avoid colliding with any other Postgres container already running on
# the host). Set DATABASE_URL to a SQLite URL (e.g. sqlite:///./chat.db) to
# run the API with no database server at all -- app.session's schema is
# backend-agnostic and creates itself.
DEFAULT_DATABASE_URL = "postgresql+psycopg2://postgres:postgres@localhost:5433/linkedin_enrichment"


@dataclass(frozen=True)
class Config:
    redis_url: str
    database_url: str
    groq_api_key: str | None
    tavily_api_key: str | None


def load_config() -> Config:
    return Config(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        groq_api_key=os.environ.get("GROQ_API_KEY"),
        tavily_api_key=os.environ.get("TAVILY_API_KEY"),
    )
