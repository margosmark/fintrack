"""Path jail for the MCP tools that accept a caller-supplied ``file_path`` (H6).

``trades_import``, ``imports_suggest_profile`` and ``imports_upload`` used to
``open()`` whatever absolute path the MCP client sent — inside a container that
also holds the full ``.env``. Every path now goes through :func:`open_upload`:

1. ``expanduser`` + ``Path.resolve(strict=True)`` — the target must exist and
   all symlinks are followed *before* containment is judged;
2. containment — the resolved path must be a file strictly inside
   ``MCP_UPLOAD_DIR`` (default ``/home/appuser/mcp_uploads``);
3. extension allowlist — only the formats the backend endpoint genuinely parses
   (``app/importers/registry.py`` sniffs OFX/QFX/CSV; ``/imports/suggest-profile``
   and ``/trades/import`` accept CSV only);
4. 5 MB cap — the same ``5_000_000`` byte limit ``app/api/imports.py`` and
   ``app/api/trades.py`` enforce, re-checked on the opened fd (the file may
   have grown between the stat and the open);
5. symlink-race guard — the file opens with ``O_NOFOLLOW`` (final component),
   must be a regular file, and its ``(st_dev, st_ino)`` is compared against the
   jail path plus a re-resolve after open, so a parent directory swapped for a
   symlink between ``resolve()`` and ``open()`` is rejected.

Failures raise ``ToolError`` carrying a ``{"detail", "code"}`` JSON envelope —
the SDK turns that into an ``is_error`` tool result containing exactly that
JSON and no traceback (mcp/server/mcpserver/server.py logs the message at INFO
without a stack trace).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import BinaryIO

from mcp.server.mcpserver.exceptions import ToolError

# Drop-folder for file_path tools; compose bind-mounts ./mcp_uploads here.
DEFAULT_UPLOAD_DIR = "/home/appuser/mcp_uploads"

# Same cap the backend enforces per upload (imports.py/trades.py: 5_000_000).
MAX_UPLOAD_BYTES = 5_000_000

# Per-tool allowlists — what each backend endpoint actually parses.
# /api/v1/trades/import and /api/v1/imports/suggest-profile are CSV-only
# (the latter rejects non-.csv filenames with 422 `not_csv`); /api/v1/imports/upload
# sniffs OFX/QFX (same OfxImporter) or CSV via detect_importer().
CSV_EXTENSIONS = frozenset({".csv"})
STATEMENT_EXTENSIONS = frozenset({".csv", ".ofx", ".qfx"})


def tool_error(detail: str, code: str) -> ToolError:
    """Structured tool error: a ``{"detail", "code"}`` envelope, never a traceback."""
    return ToolError(json.dumps({"detail": detail, "code": code}))


def _upload_root() -> Path:
    """Resolved jail root, created on first use (drop-folder UX)."""
    raw = os.getenv("MCP_UPLOAD_DIR", DEFAULT_UPLOAD_DIR)
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise tool_error(
            f"MCP_UPLOAD_DIR must be an absolute path: {raw}", "upload_dir_unavailable"
        )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise tool_error(f"MCP_UPLOAD_DIR is not usable: {raw}", "upload_dir_unavailable") from None
    return root.resolve()


def _no_symlink_opener(path: str, flags: int) -> int:
    """``open()`` opener: refuse a symlink as the final path component."""
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def _verify_open(handle: BinaryIO, resolved: Path) -> None:
    """Post-open checks: regular file, size cap, and no symlink race."""
    st_fd = os.fstat(handle.fileno())
    if not stat.S_ISREG(st_fd.st_mode):
        raise tool_error(f"Not a regular file: {resolved.name}", "not_a_regular_file")
    if st_fd.st_size > MAX_UPLOAD_BYTES:
        raise tool_error(f"File exceeds the {MAX_UPLOAD_BYTES} byte limit", "file_too_large")
    # Symlink-race guard: the opened fd must still be the jail path's file and
    # the path must still resolve to where we validated it. A parent directory
    # swapped for a symlink between resolve() and open() fails one of these.
    try:
        if resolved.resolve(strict=True) != resolved:
            raise tool_error(f"file_path changed while opening: {resolved}", "path_not_allowed")
        st_path = os.stat(resolved)
    except FileNotFoundError:
        raise tool_error(f"File not found: {resolved}", "file_not_found") from None
    except OSError:
        raise tool_error(f"Cannot re-verify file_path: {resolved}", "invalid_path") from None
    if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
        raise tool_error(f"file_path changed while opening: {resolved}", "path_not_allowed")


def open_upload(file_path: str, allowed_extensions: frozenset[str]) -> BinaryIO:
    """Open ``file_path`` only if it is an allowed file inside the upload jail.

    Returns a binary file object usable as an httpx multipart part; ``.name``
    is the validated absolute path. Raises :class:`ToolError` with a
    ``{"detail", "code"}`` envelope for every rejection (no traceback).
    """
    root = _upload_root()
    try:
        resolved = Path(file_path).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise tool_error(f"File not found: {file_path}", "file_not_found") from None
    except OSError:
        raise tool_error(f"Cannot resolve file_path: {file_path}", "invalid_path") from None

    if resolved == root or not resolved.is_relative_to(root):
        raise tool_error(
            f"file_path must be a file inside MCP_UPLOAD_DIR ({root})",
            "path_not_allowed",
        )
    if resolved.suffix.lower() not in allowed_extensions:
        expected = ", ".join(sorted(allowed_extensions))
        raise tool_error(
            f"Unsupported file type {resolved.suffix or '(none)'}; expected: {expected}",
            "unsupported_extension",
        )

    try:
        handle: BinaryIO = open(resolved, "rb", opener=_no_symlink_opener)
    except IsADirectoryError:
        raise tool_error(f"Not a regular file: {resolved.name}", "not_a_regular_file") from None
    except FileNotFoundError:
        raise tool_error(f"File not found: {file_path}", "file_not_found") from None
    except OSError:
        raise tool_error(f"Cannot open file: {file_path}", "invalid_path") from None
    try:
        _verify_open(handle, resolved)
    except BaseException:
        handle.close()
        raise
    return handle
