"""H6 path-jail tests for MCP file_path tools (mcp_server/uploads.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.uploads import (
    CSV_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    STATEMENT_EXTENSIONS,
    open_upload,
)


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip auth-mode vars so importing mcp_server.server below can never hit
    the AuthModeError startup guard or auto-login against a live URL."""
    for var in (
        "FIN_TRACKER_TOKEN",
        "FIN_TRACKER_EMAIL",
        "FIN_TRACKER_PASSWORD",
        "MCP_INTERNAL_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FIN_TRACKER_URL", "http://mcp-tests.invalid")


def _envelope(exc: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the {detail, code} envelope; also proves it is valid JSON."""
    raw = str(exc.value)
    assert "Traceback" not in raw, "tool error must not leak a traceback"
    data = json.loads(raw)
    assert set(data) == {"detail", "code"}
    return data


@pytest.fixture(name="jail")
def jail_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mcp_uploads"
    root.mkdir()
    monkeypatch.setenv("MCP_UPLOAD_DIR", str(root))
    return root


def test_file_inside_jail_opens(jail: Path) -> None:
    target = jail / "trades.csv"
    target.write_bytes(b"date,symbol\n2026-01-02,AAPL\n")
    with open_upload(str(target), CSV_EXTENSIONS) as handle:
        assert handle.read() == b"date,symbol\n2026-01-02,AAPL\n"
        # The multipart filename comes from the validated path.
        assert os.path.basename(handle.name) == "trades.csv"


def test_relative_parent_stay_inside_is_allowed(jail: Path) -> None:
    (jail / "sub").mkdir()
    target = jail / "statement.ofx"
    target.write_bytes(b"OFXHEADER:100\n")
    sneaky_but_inside = str(jail / "sub" / ".." / "statement.ofx")
    with open_upload(sneaky_but_inside, STATEMENT_EXTENSIONS) as handle:
        assert handle.read().startswith(b"OFXHEADER")


def test_parent_escape_rejected(jail: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.csv"
    outside.write_text("password,account\n", encoding="utf-8")
    # Both spellings: ../ traversal and a plain absolute path outside the root.
    for attempt in (
        str(jail / ".." / "secret.csv"),
        str(outside),
        "/etc/passwd",
    ):
        with pytest.raises(ToolError) as excinfo:
            open_upload(attempt, CSV_EXTENSIONS)
        assert _envelope(excinfo)["code"] == "path_not_allowed", attempt


def test_symlink_escape_rejected(jail: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_text("x,y\n1,2\n", encoding="utf-8")
    link = jail / "link.csv"
    os.symlink(outside, link)
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(link), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "path_not_allowed"


def test_symlink_inside_jail_allowed(jail: Path) -> None:
    target = jail / "real.csv"
    target.write_text("a,b\n", encoding="utf-8")
    link = jail / "alias.csv"
    os.symlink(target, link)
    with open_upload(str(link), CSV_EXTENSIONS) as handle:
        assert handle.read() == b"a,b\n"


def test_bad_extension_rejected(jail: Path) -> None:
    (jail / "notes.txt").write_text("not data", encoding="utf-8")
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(jail / "notes.txt"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "unsupported_extension"

    # .ofx is fine for imports_upload but not for the CSV-only tools.
    (jail / "statement.ofx").write_bytes(b"OFXHEADER:100\n")
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(jail / "statement.ofx"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "unsupported_extension"
    with open_upload(str(jail / "statement.ofx"), STATEMENT_EXTENSIONS) as handle:
        assert handle.read().startswith(b"OFXHEADER")

    # Case-insensitive: .CSV is still a CSV.
    (jail / "UPPER.CSV").write_text("a,b\n", encoding="utf-8")
    with open_upload(str(jail / "UPPER.CSV"), CSV_EXTENSIONS) as handle:
        assert handle.read() == b"a,b\n"


def test_oversize_rejected(jail: Path) -> None:
    big = jail / "big.csv"
    big.write_bytes(b"")
    os.truncate(big, MAX_UPLOAD_BYTES + 1)  # sparse — no real 5 MB write
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(big), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "file_too_large"

    # Exactly at the cap is accepted (same boundary as the backend).
    ok = jail / "ok.csv"
    ok.write_bytes(b"")
    os.truncate(ok, MAX_UPLOAD_BYTES)
    with open_upload(str(ok), CSV_EXTENSIONS) as handle:
        assert os.fstat(handle.fileno()).st_size == MAX_UPLOAD_BYTES


def test_missing_file_structured_error(jail: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(jail / "nope.csv"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "file_not_found"


def test_directory_inside_jail_rejected(jail: Path) -> None:
    (jail / "folder.csv").mkdir()
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(jail / "folder.csv"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "not_a_regular_file"


def test_unusable_upload_dir_structured_error(tmp_path: Path, monkeypatch) -> None:
    file_in_the_way = tmp_path / "not-a-dir"
    file_in_the_way.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MCP_UPLOAD_DIR", str(file_in_the_way))
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(tmp_path / "whatever.csv"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "upload_dir_unavailable"
    monkeypatch.setenv("MCP_UPLOAD_DIR", "relative/path")
    with pytest.raises(ToolError) as excinfo:
        open_upload(str(tmp_path / "whatever.csv"), CSV_EXTENSIONS)
    assert _envelope(excinfo)["code"] == "upload_dir_unavailable"


def test_tools_route_through_the_jail(jail: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The three file tools must call the backend only after the jail check."""
    from mcp_server import server as server_mod

    calls: list[tuple[str, str]] = []

    def _fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        return {"ok": True}

    monkeypatch.setattr(server_mod.client, "request", _fake_request)
    outside = jail.parent / "outside.csv"
    outside.write_text("a,b\n", encoding="utf-8")

    attempts = (
        ("trades_import", lambda path: server_mod.trades_import(1, path)),
        ("imports_suggest_profile", server_mod.imports_suggest_profile),
        ("imports_upload", lambda path: server_mod.imports_upload(1, path)),
    )
    for name, call in attempts:
        with pytest.raises(ToolError) as excinfo:
            call(str(outside))
        assert _envelope(excinfo)["code"] == "path_not_allowed", name

    assert calls == [], "backend must not be called for a rejected path"

    inside = jail / "ok.csv"
    inside.write_text("date,symbol\n", encoding="utf-8")
    assert server_mod.trades_import(1, str(inside)) == {"ok": True}
    assert calls == [("POST", "/api/v1/trades/import")]


# ---------------------------------------------------------------------------
# trades_import request shape: account_id is a MULTIPART FORM FIELD
# ---------------------------------------------------------------------------


def _multipart_fields(request: httpx.Request) -> dict[str, str]:
    """Parse non-file parts of a multipart body into {name: value}.

    Only simple text parts are returned; file parts are skipped, so this
    asserts on the *shape* the backend's ``Form()`` binding needs without
    depending on part ordering or the boundary string.
    """
    content_type = request.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data"), content_type
    _, _, raw = content_type.partition("boundary=")
    boundary = raw.strip('"').encode()
    fields: dict[str, str] = {}
    for part in request.content.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        name = None
        for header in head.split(b"\r\n"):
            if header.lower().startswith(b"content-disposition:"):
                for token in header.split(b";"):
                    if token.strip().lower().startswith(b"filename="):
                        name = None
                        break
                    if token.strip().lower().startswith(b"name="):
                        name = token.strip()[len(b"name=") :].strip(b'"').decode()
        if name is not None:
            fields[name] = body.rstrip(b"\r\n-").decode()
    return fields


def test_trades_import_sends_account_id_as_form_field(
    jail: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """account_id must ride the multipart body, not the query string.

    ``app/api/trades.py::import_trades`` declares ``account_id: Form()``;
    a query-string ``params={"account_id": ...}`` leaves the form field
    missing and FastAPI answers ``422 field_required`` (or worse, a silent
    cross-tenant default). The tool must therefore post account_id as a
    multipart part, exactly like ``imports_upload`` already does.
    """
    from mcp_server import server as server_mod

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        request.read()
        captured.append(request)
        return httpx.Response(200, json={"batch_id": 1, "staged_count": 0})

    monkeypatch.setattr(
        server_mod.client,
        "_http",
        httpx.Client(base_url=server_mod.client.base_url, transport=httpx.MockTransport(_handler)),
    )
    csv = jail / "trades.csv"
    csv.write_text("date,symbol\n2026-01-02,AAPL\n", encoding="utf-8")

    result = server_mod.trades_import(4242, str(csv))

    assert result == {"batch_id": 1, "staged_count": 0}
    assert len(captured) == 1
    sent = captured[0]
    assert sent.url.path == "/api/v1/trades/import"
    # The regression: account_id used to be a query parameter.
    assert "account_id" not in sent.url.params
    fields = _multipart_fields(sent)
    assert fields.get("account_id") == "4242"
    assert b'name="file"' in sent.content
    assert b"trades.csv" in sent.content


def test_trades_import_form_field_matches_imports_upload_shape(
    jail: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both multipart tools bind ``account_id`` the same way.

    ``trades_import`` used to send it as a query param while its sibling
    sent it as a form field — the one tool whose backend binds ``Form()``
    got a 422. Pin the shared part of the shape so they cannot drift again.
    """
    from mcp_server import server as server_mod

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        request.read()
        captured.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        server_mod.client,
        "_http",
        httpx.Client(base_url=server_mod.client.base_url, transport=httpx.MockTransport(_handler)),
    )
    csv = jail / "statement.csv"
    csv.write_text("date,amount\n2026-01-02,1.00\n", encoding="utf-8")

    server_mod.trades_import(7, str(csv))
    server_mod.imports_upload(7, str(csv))

    assert len(captured) == 2
    trades_fields = _multipart_fields(captured[0])
    upload_fields = _multipart_fields(captured[1])
    # account_id rides the body in both; neither leaks it into the query.
    assert trades_fields.get("account_id") == "7"
    assert upload_fields.get("account_id") == "7"
    for sent in captured:
        assert "account_id" not in sent.url.params
        assert sent.headers.get("content-type", "").startswith("multipart/form-data")

    # The optional form field still works on the tool that declares it.
    server_mod.imports_upload(7, str(csv), profile_json='{"mapping": {}}')
    assert _multipart_fields(captured[2]).get("profile_json") == '{"mapping": {}}'
