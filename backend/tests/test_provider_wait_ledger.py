"""M175 — every provider wait in `app/` is CLASSIFIED, and the class cannot regrow.

The M152/M153/M155/M161 fixes were piecemeal: each one closed the seam the
report happened to name, and each one's docs section ended with "still the same
class, deliberately NOT changed here". M175 is the exhaustive pass — and this
file is its GUARD, so the fifth member of the class cannot appear quietly the
way the first four did.

The rule (docs/01 §Sessions and the event loop): **a `Session` is never held
across a provider wait.** Every path that reads from the database and then
waits on a provider is a candidate, so the guard works by ENUMERATION, not by
pattern-matching intent:

* walk `app/` with an AST and collect every call that is either a direct
  network primitive (`httpx`/`requests`/`yfinance`/`smtplib`/`urllib`/`socket`),
  a call to one of this app's provider seams (`PROVIDER_SEAM_LEAVES`), or a
  dynamic Protocol dispatch (`DYNAMIC_PROVIDER_METHODS` for the bank-sync
  provider, `LLM_PROTOCOL_METHODS` for the LLM provider) — matched on the
  METHOD, never on the receiver's variable name, because the receiver differs
  per call site (`provider`, `resolved.provider`, a return value);
* key each one by the function that contains it;
* require that key to appear in `LEDGER` below, with one of three answers:
  `release` (the release is inside that very function), `release@<keys>` (a
  caller or a callee it delegates to does it — named), `no-session: …`, or
  `exception: …` (a documented, pinned reason to hold the transaction).

A NEW provider call anywhere in `app/` therefore fails this test until someone
decides which of those it is. The ledger is also checked the OTHER way: an
entry that no longer matches any code path is stale and fails too, so the table
stays a description of the tree rather than a historical record.

The full prose ledger — what each `exception:` costs and why — is docs/07
§Provider-wait ledger (M175); this file is its executable form.

Scope: `backend/app` only. `backend/mcp_server/` is an HTTP client of the API
(it opens no `Session`), and `scripts/` holds operator entry points whose
sessions the ledger classifies by the SERVICE they call — a script that grows
its own session across a wait is caught the next time someone reads this file,
not by an AST over a directory nothing else scans.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: CLIENT LIBRARIES ONLY: what "a provider wait" is made of at the bottom.
NETWORK_ROOTS = frozenset(
    {"httpx", "requests", "yfinance", "yf", "smtplib", "urllib", "socket", "aiohttp"}
)

#: The app's provider seams — the functions that perform or wrap a provider
#: fetch. A call to one of these from anywhere in `app/` is a place a `Session`
#: can be held across a network wait, so every call site is classified below.
PROVIDER_SEAM_LEAVES = frozenset(
    {
        "fetch_history",
        "fetch_history_candidates",
        "fetch_latest_close",
        "fetch_benchmark_history",
        "fetch_fundamentals",
        "fetch_fund_holdings",
        "fetch_issuer_holdings",
        "fetch_vanguard_holdings",
        "fetch_fund_composition",
        "fetch_dividend_history",
        "lookup_symbol",
        "resolve_symbol",
        "probe_quote",
        "probe_provider",
        "reverify_provider_row",
        "fetch_valet_series",
        "latest_rate",
        "get_rates_between",
        "get_rate_or_404",
        "refresh_pair",
        "ensure_recent_price",
        "ensure_fundamentals",
        "ensure_sub_fund_composition",
        "import_fund_composition",
        "backfill_range",
        "backfill_symbol",
        "backfill_all",
        "deep_backfill_all",
        "backfill_security",
        "repair_split_window",
        "repair_symbol",
        "send",
        "send_email",
        "send_webhook",
        "send_invite_email",
        "post_chat_completions",
        "exchange_code",
        "verify_id_token",
        "authorize_url",
        # The LLM column-mapping seam (t_4eb1fcbc) — it wraps `provider.chat`,
        # and the route that reaches it is where the request session releases.
        "suggest_column_mapping",
    }
)

# Provider instances are registry-resolved Protocols, so dispatch is dynamic.
# Matched on the METHOD ALONE (see `visit_Call`): the receiver is a local name
# that differs per call site — `provider`, `resolved.provider`, a return value
# — so a receiver check is exactly how a wait escapes this guard.
DYNAMIC_PROVIDER_METHODS = frozenset(
    {"list_institutions", "create_agreement", "pull_delta", "revoke"}
)

#: The LLM Protocol (`app/llm/base.py`) is a second dynamic set. `chat` is the
#: app's longest egress wait (60 s, retried once by the batch worker), and
#: until t_4eb1fcbc it was in NONE of the sets above, so every `provider.chat(...)`
#: call site was invisible here — the guard's "a fifth member of the class
#: cannot appear quietly" promise did not hold for the LLM Protocol.
LLM_PROTOCOL_METHODS = frozenset({"chat"})

#: The classification of every provider wait in `app/`, keyed by
#: `<path>::<qualified function>`. See the module docstring for the forms.
LEDGER: dict[str, str] = {
    # --- request routes -----------------------------------------------------
    "app/api/accounts.py::_build_public": "release@app/services/fx.py::latest_rate",
    "app/api/auth_oidc.py::oidc_callback": (
        "no-session: the code exchange and the ID-token verification precede the "
        "route's first DB query, so no transaction is open at the wait"
    ),
    "app/api/auth_oidc.py::oidc_login": (
        "no-session: the route takes no DB dependency (discovery + the consent redirect)"
    ),
    "app/api/fx.py::get_fx": (
        "release@app/services/fx.py::latest_rate,app/services/fx.py::get_rates_between"
    ),
    "app/api/fx.py::get_fx_board": (
        "no-session: cache-only reads (allow_fetch=False with every call)"
    ),
    "app/api/imports.py::suggest_profile.core": "release",
    "app/api/portfolio.py::_run_ensure_fundamentals": (
        "release@app/services/market_data.py::ensure_fundamentals"
    ),
    "app/api/portfolio.py::_run_ensure_recent_price": (
        "release@app/services/market_data.py::ensure_recent_price"
    ),
    "app/api/securities.py::_run_backfill": "release",
    "app/api/securities.py::_run_deepen": (
        "release@app/services/market_data.py::ensure_sub_fund_composition"
    ),
    "app/api/securities.py::check_quote": "release",
    "app/api/securities.py::import_holdings": "release",
    "app/api/securities.py::lookup_symbol": "release",
    "app/api/securities.py::refresh_security": "release",
    "app/api/securities.py::repair_security_split": (
        "release@app/services/split_repair.py::repair_split_window"
    ),
    "app/api/securities.py::security_overview": (
        "release@app/services/market_data.py::ensure_recent_price,"
        "app/services/market_data.py::ensure_fundamentals"
    ),
    "app/api/sharing.py::create_invite": "release",
    "app/api/sync.py::institutions": "release@app/services/sync/service.py::list_institutions",
    "app/api/sync.py::create_agreement": "release@app/services/sync/service.py::create_agreement",
    "app/api/sync.py::revoke": "release@app/services/sync/service.py::revoke",
    "app/api/trades.py::_resolve_security": (
        "exception: the investment-CSV staging path reaches the lookup with a "
        "FLUSHED, uncommitted `import_batches` row (and the rows already planned) on "
        "the session, so a release here would commit half an import the user never "
        "committed — pinned by "
        "tests/test_m175_provider_wait_sessions.py::"
        "test_the_import_staging_keeps_its_transaction_across_its_lookups"
    ),
    # The LLM probe route: the auth read parks the request session for the
    # whole 60 s `chat()` unless it is released first (t_4eb1fcbc).
    "app/api/users.py::test_llm_connection": "release",
    # The plain-English assistant (docs/57): `ask` reads its context, builds
    # the prompt, then releases before the up-to-60 s `chat()` — the proposal
    # it persists afterwards is written from locals, nothing is pending.
    "app/services/assistant.py::ask": "release",
    # --- operator CLI -------------------------------------------------------
    "app/cli.py::cmd_verify": (
        "no-session: the CLI holds no session — `provider_registry` and the provider "
        "fetchers open and close their own"
    ),
    "app/cli.py::cmd_reverify": "no-session: same as `cmd_verify` — the sweep holds no session",
    # --- egress clients -----------------------------------------------------
    "app/llm/base.py::post_chat_completions": (
        "no-session: pure egress, no database session in scope"
    ),
    "app/llm/egress.py::_resolve": "no-session: DNS pre-flight for an LLM/webhook target",
    "app/llm/ollama.py::OllamaProvider.chat": (
        "no-session: provider adapter, delegates to post_chat_completions"
    ),
    "app/llm/openai_compatible.py::OpenAiCompatibleProvider.chat": (
        "no-session: provider adapter, delegates to post_chat_completions"
    ),
    # The LLM mapping helper holds no session; the route that reaches it
    # releases the request session first (app/api/imports.py::suggest_profile).
    "app/services/column_mapping.py::_llm_suggest": (
        "no-session: the helper takes no session of its own — the caller "
        "releases before calling it "
        "(app/api/imports.py::suggest_profile.core), pinned by "
        "tests/test_llm_session_release.py"
    ),
    "app/services/notify_delivery.py::send": "no-session: channel dispatch switch",
    "app/services/notify_delivery.py::_dispatch": "release",
    "app/services/notify_delivery.py::send_email": "no-session: pure SMTP client",
    "app/services/notify_delivery.py::send_invite_email": "no-session: pure SMTP client",
    "app/services/notify_delivery.py::send_webhook": "no-session: pure HTTP client",
    "app/services/sync/service.py::create_agreement": "release",
    "app/services/sync/service.py::list_institutions": "release",
    "app/services/sync/service.py::pull": "release",
    "app/services/sync/service.py::revoke": "release",
    "app/services/oidc.py::_client": "no-session: httpx client factory",
    "app/services/oidc.py::_token_request": "no-session: pure HTTP",
    "app/services/fx.py::fetch_valet_series": "no-session: pure HTTP client (the Valet seam)",
    "app/services/dividend_refresh.py::refresh_one": (
        "no-session: worker's own short-lived sessions close before fetch"
    ),
    # --- workers / sweeps ---------------------------------------------------
    "app/services/benchmark_refresh.py::run_once": (
        "release@app/services/benchmarks.py::backfill_symbol"
    ),
    "app/services/price_refresh.py::run_once": (
        "release@app/services/market_data.py::ensure_recent_price"
    ),
    "app/services/benchmarks.py::backfill_all": (
        "release@app/services/benchmarks.py::backfill_symbol"
    ),
    "app/services/benchmarks.py::backfill_symbol": "release",
    "app/services/benchmarks.py::_close_in_reporting_currency": (
        "no-session: reads the rate cache-only (allow_fetch=False)"
    ),
    # The batch worker releases after its reads and before the 60 s chat,
    # which the loop below retries once (docs/07 §Provider-wait ledger).
    "app/services/categorization.py::_process_one_batch": "release",
    "app/services/deep_backfill.py::backfill_security": "release",
    "app/services/deep_backfill.py::deep_backfill_all": (
        "release@app/services/deep_backfill.py::backfill_security"
    ),
    "app/services/fund_enrichment.py::_run_enrichment": "release",
    "app/services/fx_refresh.py::run_once": "release",
    "app/services/fx.py::refresh_pair": "release@app/services/fx_refresh.py::run_once",
    "app/services/repair_report.py::scan": (
        "no-session: phases 1 and 3 open their own sessions, which CLOSE before the "
        "provider phase and reopen after it (M152's scan split)"
    ),
    "app/services/repair_report.py::_probe_rows": (
        "no-session: phase 2, with no session in existence"
    ),
    "app/services/repair_report.py::_collect_series": (
        "no-session: phase 2, with no session in existence"
    ),
    "app/services/split_repair.py::repair_split_window": "release",
    "app/services/symbol_repair.py::repair_symbol": (
        "exception: repoints the symbol and drops the stale closes in ONE transaction "
        "that scripts/repair_symbol.py rolls back for a dry run (pinned in "
        "tests/test_m155_price_session.py)"
    ),
    "app/tax/acb.py::_backfill_span": (
        "exception: `rebuild_user_pools` NEVER commits (docs/02), so the rate backfill "
        "rides the caller's transaction — which is why `fx.get_rates_between` releases "
        "on its commit=True side only (pinned in tests/test_m175_provider_wait_sessions.py)"
    ),
    # --- the provider seams themselves --------------------------------------
    "app/services/fx.py::latest_rate": "release",
    "app/services/fx.py::get_rates_between": "release",
    "app/services/fx.py::get_rate_or_404": "release@app/services/fx.py::latest_rate",
    "app/services/market_data.py::ensure_recent_price": "release",
    "app/services/market_data.py::ensure_fundamentals": "release",
    "app/services/market_data.py::ensure_sub_fund_composition": "release",
    "app/services/market_data.py::import_fund_composition": (
        "release@app/services/fund_enrichment.py::_run_enrichment,"
        "app/api/securities.py::import_holdings"
    ),
    "app/services/market_data.py::backfill_range": (
        "release@app/api/securities.py::_run_backfill,app/api/securities.py::refresh_security"
    ),
    "app/services/market_data.py::fetch_latest_close": "no-session: yfinance seam",
    "app/services/market_data.py::fetch_fund_composition": (
        "no-session: the chain composes the fetchers and takes no session"
    ),
    "app/services/market_data.py::fetch_dividend_history": (
        "no-session: yfinance dividend seam; the persistent refresh worker closes its "
        "short-lived session before this call"
    ),
    "app/services/market_data.py::fetch_issuer_holdings": "no-session: provider I/O helper",
    "app/services/market_data.py::lookup_symbol": (
        "no-session: yfinance seam — its callers own the release decision"
    ),
    "app/services/market_data.py::probe_provider": (
        "no-session: the fetchers it drives open their own sessions"
    ),
    "app/services/market_data.py::reverify_provider_row": "no-session: delegates to probe_provider",
    "app/services/market_data.py::_ticker": "no-session: yfinance client factory",
    "app/services/market_data.py::_http_search_json": "no-session: provider I/O helper",
    "app/services/market_data.py::_ishares_product_table": "no-session: provider I/O helper",
    "app/services/market_data.py::_fetch_vanguard_us": "no-session: provider I/O helper",
    "app/services/market_data.py::_vanguard_ca_catalogue": "no-session: provider I/O helper",
    "app/services/market_data.py::_vanguard_ca_holdings": "no-session: provider I/O helper",
}


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: dict[str, list[tuple[str, int]]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        parts: list[str] = []
        target: ast.AST = node.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        name = ".".join(reversed(parts))
        if (
            # Matched on the last segment only: `provider.chat(...)`,
            # `resolved.provider.chat(...)` and a bare `chat(...)` are the same
            # wait, and a receiver-name check is what let every `chat` call site
            # pass this guard unclassified (t_4eb1fcbc).
            name.split(".")[0] in NETWORK_ROOTS
            or name.split(".")[-1] in PROVIDER_SEAM_LEAVES
            or name.split(".")[-1] in DYNAMIC_PROVIDER_METHODS
            or name.split(".")[-1] in LLM_PROTOCOL_METHODS
        ):
            key = ".".join(self.stack) or "<module>"
            self.hits.setdefault(key, []).append((name, node.lineno))
        self.generic_visit(node)


def _wait_sites() -> dict[str, list[tuple[str, int]]]:
    """`{"app/<path>.py::<qualname>": [(callee, lineno), …]}` for the whole app."""
    sites: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(APP.rglob("*.py")):
        collector = _Collector()
        collector.visit(ast.parse(path.read_text(), filename=str(path)))
        relative = path.relative_to(APP.parent).as_posix()
        for qualname, hits in collector.hits.items():
            sites[f"{relative}::{qualname}"] = sorted(hits, key=lambda item: item[1])
    return sites


class _Finder(ast.NodeVisitor):
    """Finds the node whose dotted class/function stack equals a ledger qualname."""

    def __init__(self, qualname: str) -> None:
        self.qualname = qualname
        self.stack: list[str] = []
        self.found: ast.FunctionDef | ast.AsyncFunctionDef | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        if ".".join(self.stack) == self.qualname:
            self.found = node
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def _function_source(key: str) -> str:
    """The source of the function `key` names — the segment the ledger classifies."""
    path_part, qualname = key.split("::")
    text = (APP.parent / path_part).read_text()
    finder = _Finder(qualname)
    finder.visit(ast.parse(text, filename=path_part))
    if finder.found is None:
        raise AssertionError(f"the ledger names {key}, which is not a function in the tree")
    return ast.get_source_segment(text, finder.found) or ""


def test_every_provider_wait_in_app_is_classified() -> None:
    """A new provider call must be classified before it can ship.

    The failure message is the whole point of the guard: it names the seam, the
    file and the decision to make.
    """
    unclassified = sorted(set(_wait_sites()) - set(LEDGER))
    assert not unclassified, (
        "these provider waits appeared in app/ without a classification — decide "
        "whether a Session can be held across them (release it with "
        "`release_before_provider_wait`, docs/07 §Provider-wait ledger) and record the "
        f"answer in this file's LEDGER: {unclassified}"
    )


def test_the_ledger_has_no_stale_entries() -> None:
    """The ledger describes THIS tree — a renamed/moved seam must be re-pointed."""
    stale = sorted(set(LEDGER) - set(_wait_sites()))
    assert not stale, (
        "these LEDGER entries match no provider wait any more (renamed, deleted, or "
        f"the seam changed shape) — re-point or drop them: {stale}"
    )


def test_every_entry_that_claims_a_release_really_makes_one() -> None:
    """`release` means the release is INSIDE that function — checked, not trusted."""
    missing = [
        key
        for key, verdict in LEDGER.items()
        if verdict == "release"
        and "release_before_provider_wait(" not in _function_source(key)
        and not (
            key.startswith("app/services/sync/service.py::")
            and "db.close()" in _function_source(key)
        )
    ]
    assert not missing, (
        "these functions are classified as releasing their session before the provider "
        f"wait, but the call is not in them: {missing}"
    )


def test_sync_provider_dispatch_follows_request_session_close() -> None:
    """Dynamic sync Protocol calls must follow, not precede, db.close()."""
    dispatch = {
        "create_agreement": "provider.create_agreement(",
        "pull": "provider.pull_delta(",
        "revoke": "provider.revoke(",
        "list_institutions": "provider.list_institutions(",
    }
    for name, call in dispatch.items():
        source = _function_source(f"app/services/sync/service.py::{name}")
        assert source.index("db.close()") < source.index(call), name


def test_every_release_at_pointer_names_a_releasing_seam() -> None:
    """A `release@<key>` pointer must lead to a function that really releases."""
    broken: list[str] = []
    for key, verdict in LEDGER.items():
        if not verdict.startswith("release@") or verdict == "release":
            continue
        for target in verdict[len("release@") :].split(","):
            if LEDGER.get(target) != "release":
                broken.append(f"{key} → {target}")
    assert not broken, (
        "these entries say the release lives somewhere else, but the named seam is not "
        f"itself classified as a self-release: {broken}"
    )


def test_every_classification_uses_a_known_form() -> None:
    """Three forms only, so the ledger stays machine-readable."""
    bad = {
        key: verdict
        for key, verdict in LEDGER.items()
        if verdict != "release"
        and not verdict.startswith("release@")
        and not verdict.startswith("no-session:")
        and not verdict.startswith("exception:")
    }
    assert not bad, f"unclassifiable ledger entries: {bad}"
