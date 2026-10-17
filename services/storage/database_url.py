import os
import re


ALEMBIC_DATABASE_URL_PLACEHOLDER = "${DATABASE_URL}"


def to_sync_database_url(database_url: str) -> str:
    return re.sub(r"^postgresql\+asyncpg:", "postgresql:", database_url)


def to_async_database_url(database_url: str) -> str:
    sync_url = to_sync_database_url(database_url)
    return re.sub(r"^postgresql:", "postgresql+asyncpg:", sync_url)


def resolve_alembic_database_url(
    configured_url: str | None,
    env_database_url: str | None,
) -> str | None:
    candidate = configured_url
    if not candidate or candidate == ALEMBIC_DATABASE_URL_PLACEHOLDER:
        candidate = env_database_url
    if not candidate:
        return None
    return to_sync_database_url(candidate)

# The PostgreSQL app schema must never be public: Supabase music-cache lives there.
# An absent schema preserves existing local/CI database behavior.
_RESERVED_SCHEMAS = frozenset({
    "public", "auth", "storage", "realtime", "extensions", "vault",
    "pg_catalog", "information_schema", "graphql_public", "neon_auth",
})


def musix_runtime_schema() -> str | None:
    name = (os.getenv("MUSIX_DB_SCHEMA") or "").strip()
    if not name:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) or name in _RESERVED_SCHEMAS or name.startswith("pg_"):
        raise ValueError("MUSIX_DB_SCHEMA must be a safe dedicated schema name, not a Supabase system/public schema.")
    return name
