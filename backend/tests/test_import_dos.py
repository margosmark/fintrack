"""Import hardening (docs/03): stray-quote sanitizer + upload DoS guards (H2/L5/L6).

Part 1 pins the exact behavior of ``_fix_stray_quotes`` — quoted fields,
doubled quotes, stray unpaired quotes — against both hand-captured outputs and
a verbatim copy of the pre-rewrite O(Q*L) implementation as a differential
oracle, so the single-pass O(n) rewrite must be byte-for-byte identical.
Part 2 covers performance, the pre-parse line/quote caps, the 422 parse-error
envelopes (never a bare 500), early Content-Length rejection, and the
human-paced import rate limits.
"""

import random
import time

import pytest
from conftest import latency_budget_blocker
from fastapi.testclient import TestClient

from app.core.ratelimit import IMPORT_UPLOAD_LIMIT, limiter
from app.importers.csv_profile import _fix_stray_quotes

# --- Part 1: characterization of _fix_stray_quotes (pinned pre-rewrite) ------
# Expected outputs were captured from the O(Q*L) implementation before the
# single-pass rewrite; the rewrite must reproduce every one of them exactly.

STRAY_QUOTE_CASES = [
    pytest.param(
        '"RENT PAYMENT",50\n',
        '"RENT PAYMENT",50\n',
        id="plain-quoted-field-unchanged",
    ),
    pytest.param(
        'Date,Desc,Amount\n2026-01-01,"TOYS "R" US",-50\n',
        "Date,Desc,Amount\n2026-01-01,\"TOYS 'R' US\",-50\n",
        id="stray-unpaired-quote-becomes-apostrophe",
    ),
    pytest.param(
        'A,B\n"x ""y"" z",5\n',
        "A,B\n\"x ''y'' z\",5\n",
        id="csv-doubled-quotes-collapsed-to-apostrophes",
    ),
    pytest.param(
        'A,B\n"say ""hi""",5\n',
        "A,B\n\"say ''hi''\",5\n",
        id="doubled-quotes-then-close",
    ),
    pytest.param(
        'A,B\n"unterminated,5\n',
        'A,B\n"unterminated,5\n',
        id="open-quote-never-closed-unchanged",
    ),
    pytest.param(
        'A,B\nstray"quote,5\n',
        'A,B\nstray"quote,5\n',
        id="quote-with-no-pair-unchanged",
    ),
    pytest.param(
        'A,B\nfoo"bar" baz,5\n',
        "A,B\nfoo\"bar' baz,5\n",
        id="quote-closing-into-text-becomes-apostrophe",
    ),
    pytest.param(
        'A,B\r\n"v",5\r\n',
        'A,B\r\n"v",5\r\n',
        id="crlf-line-endings-unchanged",
    ),
    pytest.param(
        'A,B\nval"ue",5\n',
        'A,B\nval"ue",5\n',
        id="closing-quote-before-comma-unchanged",
    ),
    pytest.param(
        'A,B\n"a" ,5\n',
        'A,B\n"a" ,5\n',
        id="closing-quote-before-space-comma-unchanged",
    ),
    pytest.param(
        'A,B\n"x"   \n',
        'A,B\n"x"   \n',
        id="closing-quote-before-trailing-space-unchanged",
    ),
    pytest.param(
        'A,B\n"x"\t,5\n',
        'A,B\n"x"\t,5\n',
        id="closing-quote-before-tab-comma-unchanged",
    ),
    pytest.param(
        'A,B\n"x\ny",5\n',
        'A,B\n"x\ny",5\n',
        id="quote-state-resets-per-line",
    ),
    pytest.param(
        "A,B\n1,2\n",
        "A,B\n1,2\n",
        id="no-quotes-unchanged",
    ),
    pytest.param(
        "",
        "",
        id="empty-input",
    ),
]


@pytest.mark.parametrize("src, expected", STRAY_QUOTE_CASES)
def test_stray_quote_sanitizer_characterization(src: str, expected: str) -> None:
    assert _fix_stray_quotes(src) == expected


def _legacy_fix_stray_quotes(raw_text: str) -> str:
    """Verbatim pre-H2 implementation (O(quotes x line)) kept as the oracle.

    Slow on purpose; only ever called on tiny inputs below.
    """
    lines = raw_text.splitlines(keepends=True)
    fixed = []
    for line in lines:
        chars = list(line)
        in_quote = False
        i = 0
        while i < len(chars):
            ch = chars[i]
            if ch == '"':
                if in_quote:
                    rest = "".join(chars[i + 1 :]).lstrip()
                    if not rest or rest[0] in (",", "\n", "\r"):
                        in_quote = False
                    else:
                        chars[i] = "'"
                else:
                    in_quote = True
            i += 1
        fixed.append("".join(chars))
    return "".join(fixed)


@pytest.mark.parametrize("seed", range(10))
def test_stray_quote_sanitizer_matches_legacy_oracle(seed: int) -> None:
    """Differential test: new single-pass sanitizer == old O(Q*L) sanitizer."""
    rng = random.Random(seed)  # noqa: S311 -- seeded test fixture data
    alphabet = ['"', ",", " ", "\t", "\n", "\r", "\x0b", "a", "B"]
    for _ in range(25):
        src = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        assert _fix_stray_quotes(src) == _legacy_fix_stray_quotes(src), repr(src)


# --- Part 2: performance (H2) ------------------------------------------------


def test_stray_quote_sanitizer_200k_quotes_well_under_two_seconds() -> None:
    """Audit H2: 160k quotes took ~60 s pre-rewrite; must now be near-instant.

    `len(result) == 200_000` is the functional claim and runs unconditionally;
    the 2 s budget is the wall-clock claim. This one is pure Python, so it is
    CPU-share sensitive rather than DB-bound — the same reason, through the
    same helper: a scheduler slice is not a regression (docs/34, t_4980b7ed).
    """
    payload = '"' * 200_000
    start = time.perf_counter()
    result = _fix_stray_quotes(payload)
    elapsed = time.perf_counter() - start
    print(f"\n[stray-quote timing] 200,000-quote single line: {elapsed:.3f}s")
    assert len(result) == 200_000

    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "2s 200k-quote budget not measurable on this box: "
            f"{blocker}; measured {elapsed:.3f}s for 200,000 quotes"
            " (length assertion above enforced regardless)"
        )
    assert elapsed < 2.0, f"200k-quote line took {elapsed:.3f}s; expected well under 2s"


def test_stray_quote_sanitizer_quote_space_lookahead_well_under_two_seconds() -> None:
    """Exercises the whitespace look-ahead scan (quote + space x100k)."""
    payload = '" ' * 100_000
    start = time.perf_counter()
    result = _fix_stray_quotes(payload)
    elapsed = time.perf_counter() - start
    print(f"[stray-quote timing] 100k quote+space pairs: {elapsed:.3f}s")
    assert len(result) == 200_000

    blocker = latency_budget_blocker()
    if blocker is not None:
        pytest.skip(
            "2s lookahead-scan budget not measurable on this box: "
            f"{blocker}; measured {elapsed:.3f}s for 100k quote+space pairs"
            " (length assertion above enforced regardless)"
        )
    assert elapsed < 2.0, f"quote/space line took {elapsed:.3f}s; expected well under 2s"


# --- Part 2: upload validation order + 422 envelopes (H2/L5/L6) --------------

EMAIL = "dos@example.com"
PASSWORD = "correct-horse-battery"

PROFILE = {
    "columns": {"date": "Date", "raw_description": "Description", "amount_native": "Amount"},
    "date_format": "%Y-%m-%d",
    "credits_positive": True,
}

UPLOAD_URL = "/api/v1/imports/upload"
SUGGEST_URL = "/api/v1/imports/suggest-profile"
TRADES_IMPORT_URL = "/api/v1/trades/import"


def _register(client: TestClient) -> dict:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(client: TestClient, token: str, profile: dict | None = PROFILE) -> int:
    inst_payload: dict = {"name": "DoS Test Bank"}
    if profile is not None:
        inst_payload["importer_profile"] = profile
    inst = client.post("/api/v1/institutions", json=inst_payload, headers=_headers(token))
    assert inst.status_code == 201, inst.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": "CAD",
            "institution_id": inst.json()["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _upload(client: TestClient, token: str, account_id: int, payload: bytes, name="upload.csv"):
    return client.post(
        UPLOAD_URL,
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": (name, payload, "text/csv")},
    )


def _assert_envelope(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"detail", "code"}, body
    assert body["code"] == code
    assert isinstance(body["detail"], str)


def test_upload_oversize_line_is_422_csv_line_too_long(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    payload = b"Date,Description,Amount\n2026-01-01," + b"A" * 1_100_000 + b",-50\n"
    response = _upload(client, auth["access_token"], account_id, payload)
    _assert_envelope(response, 422, "csv_line_too_long")


def test_upload_too_many_quotes_is_422_csv_too_complex(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    # 1001 quotes on one (short) line: under the 1 MB line cap, over the
    # 1000-quote cap -> rejected before csv ever parses it.
    payload = b"Date,Description,Amount\n" + b'"' * 1_001 + b"\n"
    response = _upload(client, auth["access_token"], account_id, payload)
    _assert_envelope(response, 422, "csv_too_complex")


def test_upload_csv_module_error_is_422_csv_parse_error_not_500(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    # Passes both caps, then csv.Error fires on its 128 KiB field limit.
    payload = b'Date,Description,Amount\n2026-01-01,"' + b"B" * 200_000 + b'",-50\n'
    response = _upload(client, auth["access_token"], account_id, payload)
    _assert_envelope(response, 422, "csv_parse_error")


OFX_BAD_PAYLOADS = [
    pytest.param(
        (
            b"<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>"
            b"<STMTTRN><TRNTYPE>X</TRNTYPE></STMTTRN></BANKTRANLIST>"
            b"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
        ),
        id="ofxparserexception-missing-amount",
    ),
    pytest.param(b"OFXHEADER:100\nOFX\nDSML\n<GARBAGE>", id="ofxparse-valueerror-header"),
]


@pytest.mark.parametrize("payload", OFX_BAD_PAYLOADS)
def test_upload_ofx_parse_failure_is_422_ofx_parse_error_not_500(
    client: TestClient, payload: bytes
) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"], profile=None)
    response = _upload(client, auth["access_token"], account_id, payload, name="bad.ofx")
    _assert_envelope(response, 422, "ofx_parse_error")


def test_upload_oversize_content_length_is_413(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    # 6 MB body: rejected on the Content-Length header (cheapest check, L6
    # partial) before the file bytes are read or parsed.
    response = _upload(client, auth["access_token"], account_id, b"X" * 6_000_000)
    _assert_envelope(response, 413, "file_too_large")


def test_suggest_profile_line_caps_are_422(client: TestClient) -> None:
    auth = _register(client)
    payload = b"Date,Description,Amount\n" + b'"' * 1_001 + b"\n"
    response = client.post(
        SUGGEST_URL,
        headers=_headers(auth["access_token"]),
        files={"file": ("x.csv", payload, "text/csv")},
    )
    _assert_envelope(response, 422, "csv_too_complex")


def test_suggest_profile_small_csv_still_succeeds(client: TestClient) -> None:
    auth = _register(client)
    payload = b"Date,Description,Amount\n2026-01-01,COFFEE BAR,4.50\n"
    response = client.post(
        SUGGEST_URL,
        headers=_headers(auth["access_token"]),
        files={"file": ("x.csv", payload, "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "columns" in body and "reasoning" in body


def test_trades_import_csv_error_is_422_parse_error_not_500(client: TestClient) -> None:
    auth = _register(client)
    account = client.post(
        "/api/v1/accounts",
        json={"name": "Brokerage", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=_headers(auth["access_token"]),
    )
    assert account.status_code == 201, account.text
    header = b"activity_type,effective_date,symbol,quantity,unit_price,net_cash_amount\n"
    payload = header + b'Buy,2026-01-02,VEQT,"' + b"C" * 200_000 + b'",10,100\n'
    response = client.post(
        TRADES_IMPORT_URL,
        headers=_headers(auth["access_token"]),
        data={"account_id": str(account.json()["id"])},
        files={"file": ("trades.csv", payload, "text/csv")},
    )
    _assert_envelope(response, 422, "parse_error")


# --- Part 2: rate limits (L6) ------------------------------------------------


def test_import_upload_rate_limit_engages_when_enabled(client: TestClient) -> None:
    """With the limiter enabled, the (7th) request over 6/minute gets 429."""
    auth = _register(client)
    limit_per_minute = int(IMPORT_UPLOAD_LIMIT.split("/")[0])
    assert limit_per_minute >= 1
    limiter.reset()
    limiter.enabled = True
    try:
        attempts = []
        for _ in range(limit_per_minute + 1):
            response = _upload(
                client,
                auth["access_token"],
                999_999,  # no such account: in-limit requests end in 404
                b"Date,Amount\n",
            )
            attempts.append((response.status_code, response.json()))
        assert all(status == 404 for status, _ in attempts[:limit_per_minute])
        last_status, last_body = attempts[-1]
        assert last_status == 429
        assert last_body == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()
