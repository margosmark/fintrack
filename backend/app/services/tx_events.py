"""tx_events — write and read the per-transaction receipt log (M106, docs/02).

One helper per job, no router in here:

* `payload_hash` — the `mcp_audit.args_hash` formula (unsalted sha256 over the
  canonical JSON: sorted keys, tight separators, `default=str`). Byte-identical
  to `api/sharing.py::_args_hash`, so every receipt in the system is verifiable
  the same way.
* `record_event` — appends ONE row to the caller's session and does NOT commit.
  Every caller commits its state change and its receipt in the SAME
  transaction: a mutation without its receipt must not land, which is the
  opposite of `sharing_audit`'s best-effort rule and deliberately so — that log
  answers "did the owner's invite work?", this one IS the user's history.
  Pre-handler refusals (422 body validation, 429 limiter, 403 demo guard) never
  reach a handler, so they write nothing — the M91-6 ruling (docs/43 §3).
* `render_summary` / `actor_label` — the drawer's human-readable line. Computed
  at READ time from `event_kind` + the stored `payload`, so the string can be
  reworded without a data migration and the payload stays the single source.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.categories import Category
from app.models.tags import Tag
from app.models.transaction_events import TransactionEvent, TransactionEventKind

# Field names the drawer shows as words. Anything not here (cipher blobs,
# derived hashes) is still recorded in the payload but never rendered: the
# receipt proves it changed, the drawer does not need to quote ciphertext.
_FIELD_LABELS: dict[str, str] = {
    "date": "date",
    "posting_date": "posting date",
    "raw_description": "description",
    "normalized_payee": "payee",
    "amount_native": "amount",
    "currency": "currency",
    "account_id": "account",
    "category_id": "category",
    "transfer_pair_id": "transfer",
    # docs/57: an applied `transaction.update` may move the note; without a
    # label the receipt rendered as the generic "you edited this transaction".
    "note": "note",
}

# Derived columns recomputed by `update_transaction` — not user intent, so
# they never show up as "you edited …" (the fields they derive from do), and
# a PATCH that only moves one of them has changed nothing the user asked for.
RECEIPT_DERIVED_FIELDS: frozenset[str] = frozenset({"dedup_hash", "cad_amount"})


def jsonable(value: Any) -> Any:
    """Make a payload value storable in a JSON column AND hashable.

    `Decimal` / `date` / `StrEnum` are what the handlers hold; a JSON column
    takes none of them. Normalising HERE (once, before both the store and the
    hash) is what keeps `sha256(payload) == payload_hash` re-checkable against
    the bytes the drawer actually renders.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return str(value)


def payload_hash(payload: Mapping[str, Any] | None) -> str:
    """sha256 hex over the canonical payload JSON — `mcp_audit.args_hash`."""
    canonical = json.dumps(
        jsonable(dict(payload or {})),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def record_event(
    db: Session,
    *,
    tx_id: int,
    user_id: int,
    kind: TransactionEventKind,
    payload: Mapping[str, Any] | None,
    actor: int | None,
) -> TransactionEvent:
    """Append one receipt to `db` (NOT committed — the caller owns the commit).

    `actor=None` marks a cloud-service/system action (a bank-sync pull): the
    partner projection (docs/43 §6) keeps owner/partner receipts and drops
    these, so a partner never reads a machine's opinion of the ledger. M120
    hands it the same value for every pipeline apply — the three layers
    categorize with nobody at the keyboard, so their receipts are owner-only
    through the existing `actor.in_(allowed)` filter, with no new predicate.
    """
    body = jsonable(dict(payload or {}))
    event = TransactionEvent(
        tx_id=tx_id,
        user_id=user_id,
        event_kind=kind,
        payload=body,
        payload_hash=payload_hash(body),
        actor=actor,
    )
    db.add(event)
    return event


def load_category_names(db: Session, category_ids: Any) -> dict[int, str]:
    """`{id: name}` for a batch of categories, resolved AT WRITE TIME.

    A receipt stores the name the drawer will print forever; looking the name
    up at read time would let a later rename rewrite history (M106's own
    reasoning on the PATCH route). One query for a whole apply run — the
    pipeline writes receipts for hundreds of rows in one commit.
    """
    ids = sorted({cid for cid in category_ids if cid is not None})
    if not ids:
        return {}
    rows = db.exec(select(Category.id, Category.name).where(Category.id.in_(ids))).all()
    return {int(cid): str(name) for cid, name in rows}


def load_tag_names(db: Session, tag_ids: Any) -> list[str]:
    """Tag NAMES for a `tagged` payload — the drawer reads words, not ids.

    Sorted by name so the same set always hashes to the same receipt (a
    receipt that reshuffles on every read is not a receipt). Shared by
    `PUT/POST/DELETE /transactions/{id}/tags…` and by the rule engine's own
    `tagged` riders (M120), so the two payload shapes cannot drift.
    """
    ids = sorted({tid for tid in tag_ids if tid is not None})
    if not ids:
        return []
    rows = db.exec(select(Tag.name).where(Tag.id.in_(ids)).order_by(Tag.name)).all()
    return [str(name) for name in rows]


def actor_label(actor: int | None, viewer_id: int, names: Mapping[int, str | None]) -> str:
    """How the drawer words who acted, from THIS viewer's chair.

    `None` is a system action, the viewer's own id is "you" (docs/09 §History
    drawer writes every line in the second person), anyone else is named.
    """
    if actor is None:
        return "the system"
    if actor == viewer_id:
        return "you"
    name = names.get(actor)
    if name:
        return name
    return f"user #{actor}"


def render_summary(
    kind: TransactionEventKind,
    payload: Mapping[str, Any],
    actor: str,
) -> str:
    """`"you set category Groceries"` — the drawer line, newest-first list.

    Pure: same (kind, payload, actor) always renders the same string, which is
    what makes the shape testable without a browser.
    """
    if kind == TransactionEventKind.created:
        batch_id = payload.get("batch_id")
        if payload.get("source") == "sync":
            return f"{actor} created this row from bank sync (Batch #{batch_id})"
        if payload.get("source") == "import":
            return f"{actor} created this from import Batch #{batch_id}"
        if payload.get("source") == "transfer":
            return f"{actor} created this from a transfer suggestion"
        if payload.get("source") == "trade":
            return f"{actor} created this from a dividend capture"
        return f"{actor} created this transaction"
    if kind == TransactionEventKind.categorized:
        # M120 (docs/02 §transaction_events, docs/M120_RECEIPTS_SPEC §4): a
        # write whose provenance the drawer must distinguish carries `via`, so
        # the reader can tell theirs from the machine's — a suggestion is a
        # confession, a rule is a prediction (docs/04's three layers). A plain
        # PATCH payload carries NO `via`, so its line stays byte-identical to
        # the M106 wording (the hash-digest tests pin that).
        name = payload.get("category_name")
        if not name:
            return f"{actor} cleared the category"
        via = payload.get("via")
        if via == "rule":
            return f"{actor} set category {name} (rule)"
        if via == "learned":
            return f"{actor} set category {name} (learned from your edits)"
        if via == "llm":
            return f"{actor} set category {name} (assistant suggestion, auto-applied)"
        if via == "rule_backfill":
            return f"{actor} set category {name} (rule backfill)"
        if via == "suggestion_accept":
            return f"{actor} set category {name} (accepted suggestion)"
        if via == "assistant":
            # docs/57 §4: a category the plain-English assistant proposed and
            # the person applied (or undid) — provenance the drawer can state
            # without claiming the machine acted alone.
            return f"{actor} set category {name} (assistant proposal)"
        return f"{actor} set category {name}"
    if kind == TransactionEventKind.tagged:
        added = list(payload.get("added") or [])
        removed = list(payload.get("removed") or [])
        parts = []
        if added:
            parts.append(f"added tag {_joined(added)}")
        if removed:
            parts.append(f"removed tag {_joined(removed)}")
        return f"{actor} {' and '.join(parts)}"
    if kind == TransactionEventKind.reviewed:
        if payload.get("reviewed"):
            return f"{actor} marked this reviewed"
        return f"{actor} marked this unreviewed"
    if kind == TransactionEventKind.deleted:
        return f"{actor} deleted this transaction"
    # M107 (docs/44 §11): ONE receipt per mutation — `split` on create AND on
    # replace, `merge` on merge-back. Children write none: their lifecycle IS
    # the parent's events, and a later edit to a child writes an ordinary
    # receipt with `tx_id = child`.
    if kind == TransactionEventKind.split:
        if payload.get("op") == "replace":
            return f"{actor} re-split this transaction"
        return f"{actor} split this transaction"
    if kind == TransactionEventKind.merge:
        return f"{actor} merged this transaction back"
    # `edited` (and anything a future kind forgets to special-case).
    if "transfer_pair_id" in payload:
        verb = "linked" if payload.get("transfer_pair_id") else "unlinked"
        return f"{actor} {verb} a transfer"
    fields = [
        _FIELD_LABELS[name]
        for name in (payload.get("fields") or [])
        if name in _FIELD_LABELS and name not in RECEIPT_DERIVED_FIELDS
    ]
    if not fields:
        return f"{actor} edited this transaction"
    return f"{actor} edited {_joined(fields)}"


def _joined(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values)


def record_edit_event(
    db: Session,
    *,
    transaction_id: int,
    user_id: int,
    actor: int | None,
    changed: Mapping[str, Any],
) -> TransactionEvent:
    """One receipt for a PATCH: `categorized` when the category moved,
    `edited` otherwise — never two, never zero (M106 tests pin that)."""
    if "category_id" in changed:
        return record_event(
            db,
            tx_id=transaction_id,
            user_id=user_id,
            kind=TransactionEventKind.categorized,
            payload=dict(changed),
            actor=actor,
        )
    return record_event(
        db,
        tx_id=transaction_id,
        user_id=user_id,
        kind=TransactionEventKind.edited,
        payload=dict(changed),
        actor=actor,
    )


def record_transfer_event(
    db: Session,
    *,
    transaction_id: int,
    user_id: int,
    actor: int | None,
    pair_id: int | None,
    category_id: int | None = None,
    category_name: str | None = None,
) -> TransactionEvent:
    """One `edited` receipt for a transfer accept / detect / link / unlink.

    The four transfer routes mutate rows directly rather than through PATCH,
    so they carry their own writer. `pair_id` is the NEW counterpart — None
    for an unlink — and it lands BOTH inside `changes` (the PATCH shape, so
    `changed_fields` and `fields` read the same everywhere) and at the payload
    top level, because `render_summary`'s transfer branch keys off that key's
    presence and truthiness: `42 -> "you linked a transfer"`,
    `None -> "you unlinked a transfer"` (docs/02 §transaction_events).
    """
    changes: dict[str, Any] = {"transfer_pair_id": pair_id}
    fields = ["transfer_pair_id"]
    if category_id is not None:  # the accept route moves both rows to `transfers`
        changes["category_id"] = category_id
        fields.append("category_id")
    payload: dict[str, Any] = {
        "fields": fields,
        "changes": changes,
        "transfer_pair_id": pair_id,
    }
    if category_id is not None:
        payload["category_id"] = category_id
        payload["category_name"] = category_name
    return record_event(
        db,
        tx_id=transaction_id,
        user_id=user_id,
        kind=TransactionEventKind.edited,
        payload=payload,
        actor=actor,
    )
