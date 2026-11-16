"""Format -> importer registry with a fixed detect order (docs/03)."""

from typing import Any

from app.importers.base import decode_raw  # noqa: F401  (re-export convenience)
from app.importers.csv_profile import CsvImporter
from app.importers.ofx import OfxImporter
from app.importers.qif import QifImporter

# Detect order: OFX/QFX first (they are the same SGML format — QFX is
# Quicken's branded OFX), then QIF (line-oriented, its own header), then
# CSV (the sniffer explicitly rejects OFX bytes; a QIF has no comma in its
# first line, so neither sniffer can claim the other's file).
_FORMATS: tuple[tuple[str, Any], ...] = (
    ("ofx", OfxImporter),
    ("qif", QifImporter),
    ("csv", CsvImporter),
)


def detect_importer(raw: bytes) -> tuple[str, Any] | None:
    """Sniff the byte stream; returns (format, importer) or None if unsupported."""
    for fmt, importer in _FORMATS:
        if importer.detect(raw):
            return fmt, importer
    return None


def get_importer(fmt: str) -> Any | None:
    return dict(_FORMATS).get(fmt)
