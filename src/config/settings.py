"""Application settings and configuration management."""

import re
from typing import Container

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# ALIASED ON PURPOSE, and the collision is not hypothetical: the class directly
# below is also called SettingsError. An unqualified import would shadow one or
# the other depending on import order, in the one module where the difference
# between them IS the subject.
from pydantic_settings.exceptions import SettingsError as SourceError
from typing import Optional


class SettingsError(Exception):
    """Settings failed to load. Carries field NAMES only, never their values."""


#: The one error contract this module publishes. Spelled once because it is the
#: string every redactor below must agree on and a test asserts.
_PREFIX = "settings failed to load:"

#: The field name in a `pydantic_settings` source-layer message. Never trusted
#: on its own — see `_redact_source`.
_SOURCE_FIELD = re.compile(r'field "([^"]+)"')


def _redact_opaque(exc: ValueError) -> str:
    """The tail rung: a load failure this module cannot describe field-by-field.

    WHY A DEFAULT-DENY RUNG RATHER THAN A THIRD NAMED CLASS. The two clauses
    above enumerate the two failures we know how to describe, and enumeration is
    the wrong altitude for a boundary whose whole job is that nothing gets past
    it. Measured: a single non-UTF-8 byte in `.env` — a latin-1 character in a
    password, a pasted smart quote — raises ``UnicodeDecodeError`` from
    ``DotEnvSettingsSource.__init__``. That is source CONSTRUCTION, which runs
    before the source is ever called, so pydantic-settings' own
    ``except Exception -> SettingsError`` funnel never sees it either, and
    neither named class matches.

    It is the same shape as the ``.doc`` leak and strictly worse on both axes:
    the payload is ``UnicodeDecodeError.object``, the ENTIRE .env file, and it
    fires on the shipped 42-field configuration at import — no subclass and no
    complex field required. Measured on a 163-byte fixture: 163 bytes carried,
    three of three synthetic credentials present, and ZERO of them in ``str()``
    or the rendered traceback. A message-only redactor is blind to it.

    ``ValueError`` IS THE RIGHT WIDTH, not ``Exception``. It is the library's
    own altitude — ``SettingsError`` subclasses it and ``sources/base.py``
    catches it — and every credential-carrying escape found here is one.
    ``except Exception`` would also swallow genuine programming errors (a typo
    in a property, a bad import) and report them with no traceback, because the
    raise happens outside the handler; ``TypeError`` and ``AttributeError``
    still propagate normally and keep their tracebacks.

    The emitted class name is CODE-derived and therefore safe by the same
    argument that lets `_redact` emit pydantic's ``type``: it is a fixed
    vocabulary that no user-supplied value can enter.
    """
    return f"{_PREFIX}\n  <boundary>: {type(exc).__name__}"


def _redact_source(exc: SourceError, known_fields: Container[str]) -> str:
    """Render a source-layer failure as a field name and a fixed error token.

    WHY THE NAME IS CHECKED AGAINST THE MODEL rather than simply extracted.
    Reading anything out of a third-party exception message is exactly the
    discipline `_redact` refuses for `msg`, and the refusal is justified here
    too: `pydantic_settings` has a source that formats its message as
    ``f'Parsing error encountered for {field_name}: {e}'`` — interpolating the
    underlying exception, whose text may quote its input. That source is the CLI
    one and this project does not use it, but "the message happens to be safe in
    the sources we happen to use" is a property of the installed version, not of
    the library.

    So the extracted token is emitted ONLY if it is a field this model actually
    declares. The output is then provably one of two things: a declared field
    name, or ``<unknown>``. No value can reach it, whatever a future release
    puts in the message.
    """
    match = _SOURCE_FIELD.search(str(exc))
    name = match.group(1) if match else None
    field = name if name in known_fields else "<unknown>"
    return f"{_PREFIX}\n  {field}: source_error"


def _redact(exc: ValidationError) -> str:
    """Render a ValidationError as field names and error types, no values.

    Built from ``loc`` and ``type`` ONLY. Deliberately not from ``msg``: some
    pydantic messages interpolate the offending input, and the whole point here
    is that no code path can put a value in this string. ``type`` ("missing",
    "int_parsing") is a fixed vocabulary and is safe.
    """
    lines = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        lines.append(f"  {field}: {err.get('type', 'invalid')}")
    return _PREFIX + "\n" + "\n".join(lines)


class Settings(BaseSettings):
    """Application configuration."""

    def __init__(self, **kwargs):
        """Load settings, converting any validation failure into SettingsError.

        WHY THIS EXISTS (#775). Fields here are bare-named -- TELEGRAM_BOT_TOKEN
        and friends -- so pydantic reads whatever the ambient environment holds
        under those names, from a process this project does not control. On a
        validation failure its ValidationError renders ``input_value=`` with a
        truncated copy of the input, which printed part of an unrelated real
        credential for four different operators in one evening.

        WHY NOT SecretStr, which is the obvious tool and what the issue first
        suggested: measured, it does not fix this shape. The observed error is
        ``missing`` on a DIFFERENT field, and that error's ``input_value`` is
        the whole RAW input mapping, assembled before field types apply -- so
        the annotation never runs and the value still appears. Pinned by
        tests/src/config/test_settings_never_echo_values.py.

        THE RAISE HAPPENS OUTSIDE THE except BLOCK, and that is the subtle
        half. ``raise ... from exc`` would chain the original ValidationError
        and Python prints __cause__, putting input_value straight back on
        screen under "The above exception was the direct cause" -- a redaction
        that redacts nothing. But ``from None`` is not sufficient either: it
        only sets __suppress_context__, so the original exception, message and
        all, is still hanging off __context__ for any logger, debugger or
        ``repr()`` to reach. Raising after the handler has exited means there
        is no active exception to chain, so __context__ is genuinely None and
        the value is unreachable rather than merely unprinted.

        BOTH EXITS ARE COVERED (#780), because construction can fail in two
        phases that raise unrelated classes. pydantic-settings' SOURCE layer
        resolves raw values from env/dotenv/secrets BEFORE pydantic's
        VALIDATION layer runs, and raises its own ``SettingsError`` -- a
        ``ValueError``, not a ``ValidationError``, so ``except ValidationError``
        structurally cannot see it. Catching one class and calling the boundary
        complete is the mistake this second clause exists to prevent.

        THE SOURCE EXIT LEAKS MORE, NOT LESS, WHICH IS WHY IT IS NOT MERELY
        TIDINESS. Its own message names a field and a source class and quotes no
        value -- but it is chained ``from e``, and for a complex field that
        ``e`` is a ``json.JSONDecodeError`` carrying the ENTIRE undecoded input
        on ``.doc``, untruncated. Measured: a plain ``pytest --showlocals`` on
        the escaping path printed a synthetic credential nine times, against
        zero for the ValidationError path under the same invocation. Severing
        the chain is therefore the load-bearing half here; redacting the message
        alone would accomplish nothing, since the value was never in it.

        DORMANT AS DECLARED, and measured: 42 fields, zero complex-typed, zero
        aliases. The trigger is a REQUIRED list/dict/nested-model field (an
        ``Optional``-wrapped one degrades safely to the redacted validation
        path), which is an ordinary thing to add and carries no warning that it
        opens a credential path -- so the boundary covers it now rather than
        depending on whoever adds the first one noticing.
        """
        error: Optional[str] = None
        try:
            super().__init__(**kwargs)
        except ValidationError as exc:
            error = _redact(exc)
        except SourceError as exc:
            error = _redact_source(exc, type(self).model_fields)
        except ValueError as exc:
            error = _redact_opaque(exc)
        if error is not None:
            raise SettingsError(error)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # Meta Graph API
    META_GRAPH_API_VERSION: str = "v21.0"

    # Database Configuration
    DATABASE_URL: Optional[str] = None  # Full URL (overrides DB_* components if set)
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "storydump"
    DB_USER: str = "storydump_user"
    DB_PASSWORD: Optional[str] = ""
    DB_SSLMODE: Optional[str] = None  # e.g., "require" for Neon
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    TEST_DB_NAME: str = "storydump_test"

    # Telegram Configuration (REQUIRED)
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHANNEL_ID: int
    ADMIN_TELEGRAM_CHAT_ID: int

    # Target tier (the serving system being cut over to). Prefixed because these
    # pair with TARGET_TELEGRAM_BOT_TOKEN -- a DIFFERENT bot from
    # TELEGRAM_BOT_TOKEN above -- and because this class reads whatever the
    # ambient environment holds under a bare name (see the note at the top of
    # this class). An operator setting a bare TELEGRAM_ name gets no signal about
    # which of the two bots it belongs to.
    #
    # The value Telegram echoes in X-Telegram-Bot-Api-Secret-Token, set when the
    # target webhook is registered. Optional and absent by default, and the
    # absence is load-bearing: verify_secret_token refuses when the expected
    # value is missing, so an unset deployment refuses every delivery at the
    # ingress door rather than accepting every one. Arming the target webhook is
    # therefore two deliberate acts -- set this, then register -- not one.
    TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN: Optional[str] = None

    # Web-session credential signing secret (#1015). Deliberately NOT derived
    # from TELEGRAM_BOT_TOKEN: a user with no Telegram identity must not have
    # their session depend on a Telegram secret. Optional and absent by default,
    # and the absence is load-bearing the same way the webhook secret's is --
    # webapp_auth._web_token_key refuses on an unset value, so a deployment that
    # has not configured one mints and accepts nothing rather than signing every
    # credential with the same empty key.
    WEB_TOKEN_SECRET: Optional[str] = None

    # Number of Telegram updates processed concurrently (PTB
    # Application.concurrent_updates). Each concurrent callback runs in its own
    # asyncio Task with its own per-task DB session (see BaseRepository), so this
    # is the dominant multiplier on peak concurrent DB connections from button
    # taps. Keep it comfortably within the connection pool
    # (DB_POOL_SIZE + DB_MAX_OVERFLOW) alongside the background loops; unbounded
    # concurrency would exhaust the pool.
    TELEGRAM_MAX_CONCURRENT_UPDATES: int = 8

    # Outbound Telegram API pacing (PTB AIORateLimiter, wired in
    # TelegramService). The limiter's default buckets mirror Telegram's
    # published budgets (30 msgs/s overall, 20 msgs/min per group); disabling
    # it is the no-redeploy rollback lever — bursts then hit raw RetryAfter
    # walls again. MAX_RETRIES bounds how many residual RetryAfter errors
    # (budget consumed by senders the limiter cannot see, e.g. one-shot OAuth
    # bots on the same token) the limiter absorbs before surfacing the error.
    TELEGRAM_RATE_LIMITER_ENABLED: bool = True
    TELEGRAM_RATE_LIMITER_MAX_RETRIES: int = 3

    # Media Configuration
    MEDIA_DIR: str = "/tmp/media"

    # Backup Configuration
    BACKUP_DIR: str = "/backup/storydump"
    BACKUP_RETENTION_DAYS: int = 30

    # Meta app registration (deployment-level; one app, many tenants).
    # Per-tenant account selection lives in `instagram_accounts` + `api_tokens`.
    FACEBOOK_APP_ID: Optional[str] = None  # Facebook Login OAuth (legacy)
    FACEBOOK_APP_SECRET: Optional[str] = None  # Facebook Login OAuth (legacy)
    INSTAGRAM_APP_ID: Optional[str] = None  # Instagram Login OAuth (preferred)
    INSTAGRAM_APP_SECRET: Optional[str] = None  # Instagram Login OAuth (preferred)
    OAUTH_REDIRECT_BASE_URL: Optional[str] = None  # e.g., "https://api.storydump.app"

    # Which peers may set X-Forwarded-For / X-Forwarded-Proto on our behalf.
    #
    # Comma-separated addresses and/or CIDR networks. This is the set of hosts
    # whose forwarded-for claims the app believes; every other peer is
    # attributed by its real TCP address and its headers are ignored.
    #
    # The default is the private ranges rather than a specific edge address.
    # A public-internet client can never hold an RFC1918 source address, so it
    # can never place itself in this set, and the value needs no per-platform
    # tuning. Narrow it to the concrete edge address if the platform publishes
    # a stable one.
    #
    # NEVER set this to "*". The wildcard makes uvicorn take the LEFTMOST
    # X-Forwarded-For entry, which is wholly caller-supplied, so every
    # IP-keyed control in the app (rate limiting, auth-failure alerting)
    # becomes attacker-partitionable. See issue #726.
    TRUSTED_PROXY_HOSTS: str = (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1,fd00::/8"
    )

    @property
    def trusted_proxy_hosts(self) -> list[str]:
        """`TRUSTED_PROXY_HOSTS` as the list uvicorn's middleware expects."""
        return [h.strip() for h in self.TRUSTED_PROXY_HOSTS.split(",") if h.strip()]

    # Google Drive OAuth (Phase 05 Multi-Tenant)
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    # In Testing mode, Google silently expires refresh tokens after 7 days.
    # Set to 0 after moving to Production mode (refresh tokens don't expire).
    GOOGLE_REFRESH_TOKEN_TTL_DAYS: int = 7

    # Cloudinary Configuration (Phase 2 Only)
    CLOUD_STORAGE_PROVIDER: str = "cloudinary"  # Currently only cloudinary supported
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None
    CLOUD_UPLOAD_RETENTION_HOURS: int = 24  # Delete cloud uploads after this time
    # Bound the Cloudinary upload HTTP call so a stalled upload cannot hold the
    # autopost background task (and its per-item operation lock) open forever.
    CLOUD_UPLOAD_TIMEOUT_SECONDS: int = 120

    # Instagram API Rate Limiting (Phase 2)
    # Fallback daily publishing limit, used ONLY when Meta's
    # content_publishing_limit endpoint is unreachable. The authoritative
    # limit is fetched live per-account (Meta has changed it 25→50→100 and it
    # varies per account); Meta's current documented default is 100 API-published
    # posts per rolling 24h.
    INSTAGRAM_PUBLISH_LIMIT_FALLBACK: int = 100

    # Security (Phase 2 - required for token encryption)
    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB
    ENCRYPTION_KEYS: Optional[str] = (
        None  # Comma-separated Fernet keys (newest first) for key rotation
    )

    # Media Sync (loop cadence is system-wide; per-chat enable lives in chat_settings)
    MEDIA_SYNC_INTERVAL_SECONDS: int = 300  # 5 minutes

    # Logging
    LOG_LEVEL: str = "INFO"

    # AI Caption Generation
    ANTHROPIC_API_KEY: Optional[str] = None
    CAPTION_MODEL: str = "claude-haiku-4-5-20251001"

    @property
    def meta_graph_base(self) -> str:
        """Base URL for Facebook Graph API calls (FB Login flow / legacy)."""
        return f"https://graph.facebook.com/{self.META_GRAPH_API_VERSION}"

    @property
    def meta_ig_graph_base(self) -> str:
        """Base URL for Instagram Graph API calls (IG Login flow).

        Tokens issued by the Instagram Login OAuth flow are valid only against
        graph.instagram.com, not graph.facebook.com. Use this for content
        publishing and read endpoints when the account auth_method is
        'instagram_login'.
        """
        return f"https://graph.instagram.com/{self.META_GRAPH_API_VERSION}"

    @property
    def database_url(self) -> str:
        """Get database URL for SQLAlchemy.

        If DATABASE_URL is set, use it directly (standard for PaaS platforms).
        Otherwise, assemble from individual DB_* components.
        Appends ?sslmode= if DB_SSLMODE is set (required for Neon).
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL

        if self.DB_PASSWORD:
            url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        else:
            url = f"postgresql://{self.DB_USER}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

        if self.DB_SSLMODE:
            url += f"?sslmode={self.DB_SSLMODE}"
        return url

    @property
    def test_database_url(self) -> str:
        """Get test database URL."""
        if self.DB_PASSWORD:
            url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.TEST_DB_NAME}"
        else:
            url = f"postgresql://{self.DB_USER}@{self.DB_HOST}:{self.DB_PORT}/{self.TEST_DB_NAME}"

        if self.DB_SSLMODE:
            url += f"?sslmode={self.DB_SSLMODE}"
        return url


# Global settings instance
settings = Settings()
