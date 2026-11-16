"""Known export formats → deterministic mapping suggestions (docs/58 §A).

`suggest_column_mapping` consults these BEFORE the LLM: a file we recognise
gets a named, deterministic suggestion (`format` = the slug) that the wizard
still shows for confirmation — a preset short-circuits the guess, never the
confirmation gate (docs/21 §4).

Detection matches header names case-insensitively with the same normalization
YNAB's parser uses (lowercase, drop a trailing ``(…)``, collapse whitespace),
and a preset only claims a file when its **signature** headers are present, so
an ordinary bank CSV (`date, description, amount`) is never labelled Monarch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = [
    "MONARCH",
    "PRESETS",
    "FormatPreset",
    "infer_date_format",
    "normalize_header",
    "preset_suggestion",
]

_WHITESPACE = re.compile(r"\s+")
_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")

# Date shapes the presets must recognise. `%m/%d` vs `%d/%m` is resolved from
# the data (a component > 12 can only be the day); ties go month-first, which
# is Monarch's own export default.
_YEAR_FIRST = re.compile(
    r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?P<time>[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_YEAR_LAST = re.compile(
    r"^(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y>\d{4})(?P<time>[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_TEXT_MONTH_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%b %d %Y",
    "%B %d %Y",
)


def normalize_header(header: str) -> str:
    """Lowercase, drop a trailing ``(…)``, collapse internal whitespace."""
    text = _SUFFIX.sub("", header.strip().lower())
    return _WHITESPACE.sub(" ", text).strip()


def _parses(sample: str, fmt: str) -> bool:
    try:
        return datetime.strptime(sample, fmt) is not None
    except ValueError:
        return False


def _time_suffix(sample: str) -> str:
    """The strptime tail matching this sample's clock part (``""`` if none)."""
    match = _YEAR_FIRST.match(sample) or _YEAR_LAST.match(sample)
    time_part = match.group("time") if match else None
    if not time_part:
        return ""
    if time_part.count(":") == 2:
        return " %H:%M:%S"
    return " %H:%M"


def infer_date_format(samples: Iterable[str]) -> str | None:
    """A strptime format that parses EVERY sample, or ``None`` when unsure.

    Returning ``None`` (instead of guessing) is what keeps a locale-dependent
    export from being staged with the wrong day/month order — the caller falls
    back to ``%Y-%m-%d`` and the wizard's confirmation still has to pass.
    """
    values = [s.strip() for s in samples if s and s.strip()]
    if not values:
        return None

    candidate: str | None = None

    year_first = [_YEAR_FIRST.match(v) for v in values]
    if all(m is not None for m in year_first):
        sep = re.search(r"[-/.]", values[0]).group(0)  # type: ignore[union-attr]
        candidate = f"%Y{sep}%m{sep}%d"
        suffixes = {_time_suffix(v) for v in values}
        if len(suffixes) == 1:
            candidate += suffixes.pop()
        else:
            candidate = None

    if candidate is None:
        year_last = [_YEAR_LAST.match(v) for v in values]
        if all(m is not None for m in year_last):
            firsts = [int(m.group("a")) for m in year_last]  # type: ignore[union-attr]
            # A component > 12 can only be the day: >12 in slot one means
            # day-first, >12 in slot two means month-first, otherwise the
            # export is ambiguous and month-first wins (Monarch's default).
            day_first = max(firsts) > 12
            sep = re.search(r"[-/.]", values[0]).group(0)  # type: ignore[union-attr]
            if day_first:
                candidate = f"%d{sep}%m{sep}%Y"
            else:
                candidate = f"%m{sep}%d{sep}%Y"
            suffixes = {_time_suffix(v) for v in values}
            if len(suffixes) == 1:
                candidate += suffixes.pop()
            else:
                candidate = None

    if candidate is None:
        for fmt in _TEXT_MONTH_FORMATS:
            if all(_parses(v, fmt) for v in values):
                candidate = fmt
                break

    if candidate is None or not all(_parses(v, candidate) for v in values):
        return None
    return candidate


@dataclass(frozen=True)
class FormatPreset:
    """One recognised export format and how to map it (docs/58 §A)."""

    slug: str
    label: str
    # logical CsvProfile field -> acceptable header names, first hit wins
    columns: dict[str, tuple[str, ...]]
    required: tuple[str, ...]  # logical fields that must all resolve
    signature: tuple[str, ...]  # at least one of these headers must exist
    date_format_from_samples: bool = False
    credits_positive: bool = False
    confidence: float = 0.95

    def resolve(self, headers: list[str]) -> dict[str, str] | None:
        """Map logical field -> the file's own header name, or ``None``."""
        index: dict[str, str] = {}
        for header in headers:
            index.setdefault(normalize_header(header), header)

        resolved: dict[str, str] = {}
        for logical, candidates in self.columns.items():
            for candidate in candidates:
                if candidate in index:
                    resolved[logical] = index[candidate]
                    break

        if any(logical not in resolved for logical in self.required):
            return None
        if self.signature and not any(sig in index for sig in self.signature):
            return None
        return resolved


# Monarch's transaction export: `date, merchant, amount, category, account,
# notes, tags`, expenses negative. The signature is `merchant` (or Monarch's
# `original statement`) — a generic `description` column alone does NOT claim
# the file (docs/58 §A).
MONARCH = FormatPreset(
    slug="monarch",
    label="Monarch Money export",
    columns={
        "date": ("date",),
        "amount_native": ("amount",),
        "raw_description": ("merchant", "original statement", "description"),
    },
    required=("date", "amount_native", "raw_description"),
    signature=("merchant", "original statement"),
    date_format_from_samples=True,
    credits_positive=True,  # Monarch exports expenses as negative numbers
)

PRESETS: tuple[FormatPreset, ...] = (MONARCH,)


def preset_suggestion(headers: list[str], rows: list[dict[str, str]]) -> dict[str, Any] | None:
    """A full ``SuggestProfileResponse``-shaped suggestion, or ``None``.

    Deterministic: no LLM, no network. The caller still gates every write on
    the user confirming this mapping (docs/21 §4).
    """
    if not headers:
        return None
    for preset in PRESETS:
        resolved = preset.resolve(headers)
        if resolved is None:
            continue

        date_format = "%Y-%m-%d"
        if preset.date_format_from_samples:
            date_header = resolved["date"]
            samples = [row.get(date_header, "") for row in rows]
            date_format = infer_date_format(samples) or "%Y-%m-%d"

        return {
            "columns": resolved,
            "date_format": date_format,
            "decimal_comma": False,
            "credits_positive": preset.credits_positive,
            "headerless": False,
            "format": preset.slug,
            "confidence": preset.confidence,
            "reasoning": (
                f"Recognised a {preset.label}: columns matched by name "
                f"({', '.join(sorted(resolved))}) and the sign convention is "
                "taken from the format. Review the mapping and confirm before "
                "anything is imported."
            ),
        }
    return None
