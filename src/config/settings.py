"""Application settings and configuration management."""

import ipaddress
import re
import uuid
from typing import Container

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ALIASED ON PURPOSE, and the collision is not hypothetical: the class directly
# below is also called SettingsError. An unqualified import would shadow one or
# the other depending on import order, in the one module where the difference
# between them IS the subject.
from pydantic_settings.exceptions import SettingsError as SourceError
from typing import Optional

from src.config.db_url import userinfo


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
    fires on the shipped configuration at import — no subclass and no
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


def parse_ops_user_ids(raw: str) -> tuple[frozenset[str], list[int]]:
    """`OPS_USER_IDS` (comma-separated) as ``(ids, refused)``: each entry
    canonical the way the database spells a user id, so a braced, hyphen-less
    or `urn:uuid:` paste still matches, and the 1-based positions of the
    entries that are not a UUID at all and admit nobody. The one parser: the
    API's startup warning reads ``refused`` from here."""
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    ids, refused = set(), []
    for position, entry in enumerate(entries, start=1):
        try:
            ids.add(str(uuid.UUID(entry)))
        except ValueError:
            refused.append(position)
    return frozenset(ids), refused


class Settings(BaseSettings):
    """Application configuration. NO FIELD IS REQUIRED (#1222): a process needs
    only what it reads, and a field survives only while something reads it
    (`tests/src/test_legacy_settings_gone.py` measures both)."""

    def __init__(self, **kwargs):
        """Load settings, converting any validation failure into SettingsError.

        WHY THIS EXISTS (#775). Fields here are bare-named -- ENCRYPTION_KEY,
        TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN and friends -- so pydantic reads
        whatever the ambient environment holds under those names, from a
        process this project does not control. On a validation failure its
        ValidationError renders ``input_value=`` with a truncated copy of the
        input, which printed part of an unrelated real credential for four
        different operators in one evening. That was a ``missing`` error on a
        REQUIRED sibling of the legacy tier's bot token; no field is required
        since that tier retired, so that exact shape is DORMANT as declared --
        a field failing its own validation still reaches it, and the first
        required field anyone adds re-arms it, which is why the boundary
        stays.

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

        DORMANT AS DECLARED, and measured: zero complex-typed fields, zero
        aliases, none required. The trigger is a REQUIRED list/dict/nested-model field (an
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

    # The TEST HARNESS's database, by component. No deployed root builds a URL
    # from these: the worker and the API take TARGET_DATABASE_URL and the
    # migration runner takes DATABASE_URL, both from the process environment at
    # run time. `unit_of_work.async_database_url` and `test_database_url`
    # below are the readers, and the suite is their caller.
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "storydump"
    DB_USER: str = "storydump_user"
    DB_PASSWORD: Optional[str] = ""
    DB_SSLMODE: Optional[str] = None  # e.g., "require" for Neon
    TEST_DB_NAME: str = "storydump_test"

    # The Telegram fields are prefixed TARGET_ because they pair with
    # TARGET_TELEGRAM_BOT_TOKEN (read by the worker and the API directly), and
    # because this class reads whatever the ambient environment holds under a
    # bare name (see `__init__`). The product runs ONE bot
    # (documentation/operations/telegram-webhook.md).
    #
    # The value Telegram echoes in X-Telegram-Bot-Api-Secret-Token, set when the
    # target webhook is registered. Optional and absent by default, and the
    # absence is load-bearing: verify_secret_token refuses when the expected
    # value is missing, so an unset deployment refuses every delivery at the
    # ingress door rather than accepting every one. Arming the target webhook is
    # therefore two deliberate acts -- set this, then register -- not one.
    TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN: Optional[str] = None
    # The target bot's @username (without the @) — the bot whose webhook
    # points at this API and whose token the worker sends with. Renders the
    # `t.me/<bot>?start=link-…` deep link (`07` §2 `link`). Absent means the
    # link route refuses 503 rather than minting a link to nowhere.
    TARGET_TELEGRAM_BOT_USERNAME: Optional[str] = None
    # S.2 applied to taps (F12, locked 2026-09-09): commands a workspace may
    # EXECUTE from Telegram taps per minute, checked before the flip and
    # debited only for a flip that ran — a repeat tap that is merely answered
    # with its state spends nothing. 120 = two members each clearing a backlog
    # at a tap a second (`05`'s revision rule). The web route keeps S.2's 30.
    TARGET_TAP_ADMISSION_PER_MINUTE: int = 120

    # Meta app registration (deployment-level; one app, many tenants).
    # Per-tenant account selection lives in the target tier's `ig_accounts`
    # and `oauth_credentials`.
    # Meta's signed callbacks verify against INSTAGRAM_APP_SECRET, then this.
    FACEBOOK_APP_SECRET: Optional[str] = None
    INSTAGRAM_APP_ID: Optional[str] = None  # Instagram Login OAuth
    INSTAGRAM_APP_SECRET: Optional[str] = None  # Instagram Login OAuth
    OAUTH_REDIRECT_BASE_URL: Optional[str] = None  # e.g., "https://api.storydump.app"

    # The web front end (#1028). The target API hosts sign-in and the session
    # cookie; the front end is a separate origin that calls it. WEB_APP_URL is
    # the ONE browser origin CORS admits (never "*") and where a finished
    # sign-in lands. Absent = no browser origin is admitted and sign-in lands
    # on the API's own root, which is the fail-closed reading.
    WEB_APP_URL: Optional[str] = None
    # Session cookie scope. None = host-only (the API host, unreadable by the
    # front end's server side); the shared registrable domain (e.g.
    # "storydump.app") lets a same-site front end's SSR read it. Secure is on
    # by default and only a local http dev setup should turn it off.
    SESSION_COOKIE_DOMAIN: Optional[str] = None
    SESSION_COOKIE_SECURE: bool = True
    # A web session's ABSOLUTE lifetime, counted from sign-in
    # (`session_tokens.created_at`). Use slides `expires_at` 30 days out, and
    # without a cap a session used once a month would never end; past this
    # age it is refused as expired however recently it was used, and no slide
    # carries `expires_at` beyond `created_at` + this. 30 days: the cookie's
    # own Max-Age and the "30 days" the Privacy page states.
    # Bounded: 0 would end every session at once, and a huge value overflows
    # the interval it builds.
    SESSION_MAX_AGE_SECONDS: int = Field(30 * 24 * 3600, gt=0, le=365 * 24 * 3600)
    # Sign-up while in beta (092, owner decision 2026-10-02): a NEW Google
    # account creates its user only when `fn_signup_admitted` admits its
    # verified email. True switches that ask off — a local stack's setting,
    # never production's. An existing user signs in either way.
    TARGET_SIGNUP_OPEN: bool = False

    # Which peers may set X-Forwarded-For / X-Forwarded-Proto on our behalf.
    #
    # Comma-separated addresses and/or CIDR networks. This is the set of hosts
    # whose forwarded-for claims the app believes; every other peer is
    # attributed by its real TCP address and its headers are ignored.
    #
    # The default is the private ranges plus 100.64.0.0/10, the shared address
    # space Railway's edge connects from (production's access log shows every
    # request arriving from 100.64.0.x). None of these is routable on the
    # public internet, so a public client can never hold one as its source
    # address and place itself in this set. Narrow it to the concrete edge
    # address if the platform publishes a stable one.
    #
    # Only the edge is trusted, never the CDN behind it. When Railway routes a
    # request through Fastly, the header arrives as "<client>, <Fastly edge>"
    # and the walk stops at the Fastly address: shared by the visitors that
    # edge serves, but never caller-chosen. Skipping it would let anyone who
    # fronts the API with their own Fastly service write the client entry.
    #
    # NEVER set this to "*". The wildcard makes uvicorn take the LEFTMOST
    # X-Forwarded-For entry, which is wholly caller-supplied, so every
    # IP-keyed control in the app (rate limiting, auth-failure alerting)
    # becomes attacker-partitionable. See issue #726.
    TRUSTED_PROXY_HOSTS: str = (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,127.0.0.1,::1,fd00::/8"
    )

    # The addresses of the hop Railway puts between its edge and the app.
    #
    # Railway's edge sees the visitor (its Network Logs' `srcIp`), then the
    # request reaches the app from 100.64.0.x with one more entry after the
    # visitor's: `X-Forwarded-For: <visitor>, 152.233.47.x` (measured
    # 2026-10-06: .66, .67 and .69, from one edge region). When the header
    # ENDS in one of these, `DropEdgeHopMiddleware` removes exactly that one
    # entry, so the walk lands on the visitor rather than on a hop every
    # visitor shares. Never more than one, and never the only entry; a hop
    # outside this range (another region's, say) is kept, so its visitors
    # share that hop's limits rather than anyone choosing their own. Keep it
    # as narrow as the measured hops: a client holding one of these addresses
    # on a path with no hop would have its own entry removed (the middleware's
    # docstring says when that matters). Listing an address here never makes
    # it a trusted peer. "*", a malformed entry or a range broader than /16 (v6: /48) is
    # refused at load.
    EDGE_HOP_HOSTS: str = "152.233.47.0/24"

    @field_validator("EDGE_HOP_HOSTS")
    @classmethod
    def _narrow_hops(cls, value: str) -> str:
        """Refuse "*" and any range broad enough to cover callers at large:
        a removed entry must be a hop, so the list names hops, not networks."""
        for entry in (e.strip() for e in value.split(",")):
            if not entry:
                continue
            if "*" in entry:
                raise ValueError("EDGE_HOP_HOSTS must list addresses, never *")
            net = ipaddress.ip_network(entry, strict=False)
            if net.prefixlen < (16 if net.version == 4 else 48):
                raise ValueError(
                    "EDGE_HOP_HOSTS ranges must be /16 (v6: /48) or narrower"
                )
        return value

    # The largest request body the API reads, in bytes; over it is 413
    # (`app.py::BodySizeLimitMiddleware`). No route takes an upload: the
    # largest body anything reads is one Telegram update, and every other is
    # a small JSON object or a Meta callback form, so 1 MiB is far above
    # every legitimate request.
    API_REQUEST_BODY_MAX_BYTES: int = 1024 * 1024

    # Who may read the API's operating details (`GET /api/v1/ops/health`):
    # comma-separated user ids, the `user` line `storydump whoami` prints. Empty —
    # the default — admits nobody, so the details stay closed until the
    # deployment names them. Not the `operator` token role: a person-bound
    # token of any role passes only if its person is listed here.
    OPS_USER_IDS: str = ""

    # The secret the landing site's server sends with each waitlist signup
    # (`POST /public/waitlist`, `07` §43), the same value as the site's
    # WAITLIST_SITE_SECRET on Vercel. Set, the API refuses a call without it
    # and limits each visitor the site names on their own counter; unset — the
    # default — it keys the limit on the site's address, shared by everyone.
    # Set it on the site first: until then the site sends nothing to match.
    WAITLIST_SITE_SECRET: Optional[str] = None

    @property
    def web_app_origin(self) -> Optional[str]:
        """The front end's origin, normalized (no trailing slash), or None.

        The ONE browser origin CORS admits and the host a finished or failed
        sign-in lands on — one spelling, so the cookie can never land on a
        page whose origin CORS refuses.
        """
        return self.WEB_APP_URL.rstrip("/") if self.WEB_APP_URL else None

    @property
    def waitlist_site_secret(self) -> Optional[str]:
        """`WAITLIST_SITE_SECRET` without surrounding whitespace (the site
        trims its copy too, so a pasted newline cannot refuse every signup),
        or None when unset or blank."""
        return (self.WAITLIST_SITE_SECRET or "").strip() or None

    @property
    def trusted_proxy_hosts(self) -> list[str]:
        """`TRUSTED_PROXY_HOSTS` as the list uvicorn's middleware expects."""
        return [h.strip() for h in self.TRUSTED_PROXY_HOSTS.split(",") if h.strip()]

    @property
    def edge_hop_hosts(self) -> list[str]:
        """`EDGE_HOP_HOSTS` as a list, like `trusted_proxy_hosts`."""
        return [h.strip() for h in self.EDGE_HOP_HOSTS.split(",") if h.strip()]

    @property
    def ops_user_ids(self) -> frozenset[str]:
        """`OPS_USER_IDS` as the canonical ids the database returns."""
        return parse_ops_user_ids(self.OPS_USER_IDS)[0]

    # Google Drive OAuth: the client the workspace grant is minted and
    # refreshed with (the worker warns at boot without both).
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # Security: the Fernet key(s) the stored credentials are encrypted with.
    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB
    ENCRYPTION_KEYS: Optional[str] = (
        None  # Comma-separated Fernet keys (newest first) for key rotation
    )

    # Logging
    LOG_LEVEL: str = "INFO"

    @property
    def test_database_url(self) -> str:
        """Get test database URL."""
        # The user and password encoded (`src.config.db_url`): pasted raw, one
        # carrying `@`, `/` or `%` was misread by libpq.
        auth = userinfo(self.DB_USER, self.DB_PASSWORD)
        url = f"postgresql://{auth}{self.DB_HOST}:{self.DB_PORT}/{self.TEST_DB_NAME}"

        if self.DB_SSLMODE:
            url += f"?sslmode={self.DB_SSLMODE}"
        return url


# Global settings instance
settings = Settings()
