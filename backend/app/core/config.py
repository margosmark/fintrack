"""Application configuration via pydantic-settings (docs/01 §Configuration).

Env keys mirror .env.example exactly; every key has a dev fallback so the API
can boot without a .env present — except jwt_secret, which fails fast on its
insecure default (security review #3).
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack"
    # The UNPRIVILEGED role the request path connects as (docs/55). Empty (the
    # default) keeps today's behaviour — requests share DATABASE_URL's schema
    # owner and the database-level tenant policies are inert. Set it to the
    # provisioned app role (backend/scripts/provision_rls_role.py) to arm the
    # second lock; startup then VERIFIES the role is genuinely constrained
    # (app.core.rls.verify_request_role) and refuses to boot otherwise, so a
    # misconfiguration fails loudly instead of silently enforcing nothing.
    app_database_url: str | None = None
    # Optional independent password for the app role; empty = reuse
    # DATABASE_URL's. (scripts/provision_rls_role.py reads both.)
    app_role_password: str = ""
    jwt_secret: str = "dev-secret-change-me"  # noqa: S105 -- sentinel rejected at boot by _reject_default_secret below
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # --- JWT signing-key separation (t_d3176af6) ------------------------------
    # Server-signed tokens (docs/01, docs/38) are RS256 with a `kid` header,
    # split across TWO key families so one compromised key cannot mint every
    # kind of token:
    #   - ACCESS  signs `type=access` bearer tokens (short-lived session).
    #   - SESSION signs `type=challenge` (2FA step-up) AND `type=oidc_pending`
    #     (the OIDC hand-off blob) — i.e. the two families that ESCALATE into a
    #     brand-new session. An access-key leak therefore cannot forge a
    #     long-lived session; a session-key leak cannot mint bearer tokens.
    # `jwt_secret` no longer signs ANY JWT (it is retained only as the legacy
    # fallback for `crypto.py`'s Fernet key — see `app_encryption_key` below —
    # and for the webhook HMAC fallback in `services/notify_delivery.py`).
    #
    # Each family is provisioned from a PEM key FILE (the production path —
    # `scripts/generate_jwt_keys.py` writes them). When the paths are left
    # empty the app generates an EPHEMERAL in-process RSA keypair per family:
    # zero-config for dev/tests, but tokens then do not survive a restart or a
    # second worker, so a deployment MUST set the four paths. Never commit a
    # private key; keep the PEMs beside the deployed `.env` (gitignored).
    jwt_access_private_key_path: str = ""
    jwt_access_public_key_path: str = ""
    jwt_access_kid: str = "access-1"
    jwt_session_private_key_path: str = ""
    jwt_session_public_key_path: str = ""
    jwt_session_kid: str = "session-1"

    # --- Dedicated secrets-encryption key (t_d3176af6) ------------------------
    # Fernet key (urlsafe-b64, 32 raw bytes) that encrypts user secrets at rest
    # (`totp_factors.secret`/`pending_secret`, `users.llm_api_key`). Empty falls
    # back to the legacy sha256(jwt_secret) derivation so an existing install
    # keeps decrypting its stored ciphertext with no migration. SETTING THIS is
    # the fix: once `APP_ENCRYPTION_KEY` is configured, rotating `JWT_SECRET`
    # no longer orphans every stored TOTP seed / LLM key. Back it up WITH the
    # database — losing it makes those ciphertexts unreadable (same note as the
    # OFX hash key). Decrypt additionally tries `APP_ENCRYPTION_KEY_FALLBACKS`
    # (comma-separated old keys) so a rotation never strands ciphertext; new
    # writes always use the primary key.
    app_encryption_key: str = ""
    app_encryption_key_fallbacks: str = ""
    cors_origins: str = "http://localhost:5173"
    # M87 (docs/42): `/docs`, `/redoc` and `/openapi.json` are an API map, so
    # they are authenticated-only by default — 401 `{detail, code}` for a
    # caller with no credential, 200 for a session access token or a live MCP
    # token. Set true ONLY for local dev (`.env.example` does), where an
    # unauthenticated Swagger UI is the whole point of running the stack.
    api_docs_public: bool = False
    # Host-header allowlist for `TrustedHostMiddleware` (docs/42). Empty string
    # = DISABLED, which is the default and is the safe-for-upgrades choice: a
    # wrong allowlist returns 400 to every request including the healthcheck
    # behind the proxy, so it must never be enabled implicitly. Per docs/42
    # the listener is published to the whole LAN, so a deployment that wants the
    # header checked sets `ALLOWED_HOSTS=api.example.com,localhost`.
    # Note this is defence-in-depth only — no route builds a URL from the Host
    # header except the OIDC/app-origin redirects, which derive from settings.
    allowed_hosts: str = ""

    llm_provider: str = "ollama"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "qwen2.5:7b-instruct"
    reporting_currency: str = "CAD"
    rate_limit_enabled: bool = True
    # --- M137 P1-1 (docs/33 §Server-first computing, docs/08 §Report routes)
    # The `/reports/*` routes no UI surface has ever called: /networth-snapshot
    # (the last of M134 §3.6.2's orphans — /dividends, /cashflow-sankey and
    # /total-return left the set with M160-R2, t_85311c34, which re-pointed
    # their panels at the routes). Default OFF, so a deployment that never
    # sets the key keeps answering and nothing silently 404s. Set
    # SERVER_REPORTS_DEPRECATED=1 and each one answers 410
    # `report_route_deprecated` with a body naming what survived. Report routes
    # the UI consumes are never gated.
    server_reports_deprecated: bool = False

    # Demo experience (w8-contract): pre-seeded account reachable in one click.
    seed_demo_account: bool = True
    demo_password: str = "demo-password"  # noqa: S105 -- public demo login, printed on the Login page
    # Shared secret for MCP server → backend internal endpoints.
    mcp_internal_secret: str = ""
    # Cookie security (security review M2): Secure is the default because
    # production is HTTPS-only; browsers treat http://localhost as a secure
    # origin, so plain-HTTP local dev keeps working. Set false only for
    # LAN-IP HTTP access.
    cookie_secure: bool = True
    # Allow USER-supplied llm_base_url targets on RFC1918/link-local networks
    # (LAN Ollama). Default false (security review H1): user-supplied LLM
    # endpoints must be loopback or public addresses. The operator-configured
    # LLM_BASE_URL is trusted and exempt.
    llm_allow_private_hosts: bool = False

    # --- M59 notification delivery (docs/37) — all credentials/key material
    # live HERE, never in the database. Empty SMTP host = the email channel
    # cannot be enabled (409 channel_not_configured), which is the supported
    # "no SMTP infra on this install" state.
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password: str = ""
    notify_smtp_starttls: bool = True
    notify_smtp_from: str = ""
    # HMAC master key for webhook bodies; empty falls back to jwt_secret, which
    # means rotating JWT_SECRET also invalidates issued webhook keys.
    notify_webhook_secret: str = ""
    notify_app_base_url: str = "http://localhost:5173"
    # SSRF policy for user-supplied webhook targets: loopback is always allowed;
    # RFC1918/link-local/etc. needs this operator opt-in.
    notify_webhook_allow_private: bool = False
    notify_worker_enabled: bool = True
    notify_worker_interval_seconds: int = 60

    # --- M150 FX rate-book refresh (docs/07 §Scheduled refresh) ----------------
    # In-process worker that keeps the BoC cache current so the ≤7-day
    # carry-forward is only ever used for BoC's publication calendar (weekends,
    # holidays) — never for a book that simply stopped being fetched. The pair
    # list is the static `CURATED_FX_BASES`, so the tick's egress carries no
    # tenant data; it is skipped outright while any user has privacy_mode on
    # (docs/11). Tests force it off (conftest) so no background tick can fetch.
    fx_refresh_enabled: bool = True
    fx_refresh_interval_seconds: int = 21600

    # --- M173 benchmark series refresh (docs/07 §Benchmark series) ------------
    # In-process worker that keeps `benchmark_prices` current for the four
    # vs-Benchmark picks (docs/28), so the panel's comparison is a live series
    # rather than a one-time import. The cache is SHARED with no `user_id`, so
    # the tick is skipped outright while any tenant has privacy_mode on
    # (docs/11) — the M150 FX rule, for the same reason. Tests force it off
    # (conftest) so no background tick can fetch behind a fixture.
    benchmark_refresh_enabled: bool = True
    benchmark_refresh_interval_seconds: int = 21600

    # M184 dividend-history refresh worker (docs/07 + PERFORMANCE_READ_PATH_PLAN.md).
    # Persistent pending rows are recovered after restart; tests disable the
    # worker to keep fixtures deterministic.
    dividend_refresh_enabled: bool = True

    # --- M187 scheduled price refresh (docs/07 §Scheduled price refresh) ------
    # In-process worker that ADVANCES today's closes on a fixed wall-clock
    # schedule, because the request fan-out stops at a close already dated
    # today (docs/07 §Market-hours optimization) — without it a security's bar
    # freezes at whatever the first market-hours page load of the day saw. The
    # opt-in lives at the seam (`ensure_recent_price(intraday_update=True)`,
    # only this worker passes it), a manual price is never overwritten (docs/07
    # §Manual price overrides), and only a privacy-mode OWNER's securities are
    # skipped (docs/11 — `prices` is per-user, unlike the shared FX/benchmark
    # caches whose workers skip the whole tick). Ticks are HH:MM wall-clock
    # times in America/New_York, NOT an interval: the schedule is anchored to
    # the session — ≥4 per trading day, one after the open and one after the
    # 16:00 close. Tests force it off (conftest) so no background tick can
    # fetch behind a fixture.
    price_refresh_enabled: bool = True
    price_refresh_times: str = "09:35,12:00,15:00,16:15"

    # --- M74 auth hardening (docs/38) ------------------------------------
    # TOTP issuer label shown in authenticator apps.
    totp_issuer: str = "fin_tracker"
    # Public origin of the SPA (scheme + host[:port], NO trailing slash).
    # Pins the WebAuthn rpID (its hostname) AND the expected origin AND the
    # base the OIDC callback redirects back to. Deliberately NOT derived from
    # notify_app_base_url — that key is unset in the deployed .env and would
    # silently pin rpID to `localhost`.
    app_origin: str = "http://localhost:5173"
    # Optional OIDC SSO — off by default; every route except /auth/oidc/config
    # answers 404 oidc_disabled while false.
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Empty derives {app_origin}/api/v1/auth/oidc/callback.
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid email profile"

    @model_validator(mode="after")
    def _reject_default_secret(self) -> "Settings":
        """Fail fast on public/placeholder secrets (docs/29 F8, security review #3).

        A forgotten JWT_SECRET must never silently sign tokens with a value
        that is committed to the repository; short secrets are brute-forceable.
        """
        known_defaults = {"dev-secret-change-me", "change-me-long-random-string"}
        if self.jwt_secret in known_defaults:
            raise ValueError(
                "JWT_SECRET is still a public default; set a long random "
                "JWT_SECRET in .env before starting the API"
            )
        if len(self.jwt_secret) < 32:
            raise ValueError(
                "JWT_SECRET is too short (must be at least 32 characters); "
                "set a long random JWT_SECRET in .env before starting the API"
            )
        for family in ("jwt_access", "jwt_session"):
            priv = getattr(self, f"{family}_private_key_path")
            pub = getattr(self, f"{family}_public_key_path")
            if bool(priv) != bool(pub):
                raise ValueError(
                    f"{family.upper()}_PRIVATE_KEY_PATH and {family.upper()}_PUBLIC_KEY_PATH "
                    "must be set together (a signing key needs both halves); "
                    "leave both empty to use an ephemeral dev/test keypair"
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        """Host-header allowlist; empty means the check stays OFF.

        `TrustedHostMiddleware` is only mounted when this is non-empty, so the
        default deployment behaves exactly as before. Starlette special-cases
        `allowed_hosts=["*"]`, which is equivalent to not mounting the
        middleware at all.
        """
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
