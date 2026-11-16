"""transactions — filtered list + inline correction shapes (docs/08, docs/09)."""

from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.transaction_events import TransactionEventKind
from app.models.transactions import RecurringCadence
from app.schemas.tags import TagPublic


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class TransactionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt_date
    posting_date: dt_date | None = None
    raw_description: str = Field(min_length=1)
    normalized_payee: str | None = Field(default=None, min_length=1)
    amount_native: Decimal
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    category_id: int | None = None
    # E2EE Phase 2 (docs/33): opaque ciphertext + blind index token.
    payee_cipher: str | None = None
    raw_desc_cipher: str | None = None
    amount_cipher: str | None = None
    cad_amount_cipher: str | None = None
    payee_token: str | None = None

    _validate_non_blank = field_validator("raw_description", "normalized_payee")(_non_blank)


class TransactionCreate(TransactionFields):
    account_id: int


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int | None = None
    date: dt_date | None = None
    posting_date: dt_date | None = None
    raw_description: str | None = Field(default=None, min_length=1)
    normalized_payee: str | None = Field(default=None, min_length=1)
    amount_native: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    category_id: int | None = None
    # M107 (docs/44 §4.1): `note` is a LEDGER field, so while a row is split
    # it is refused with `transaction_is_split` — tags and notes live on the
    # children for as long as the split exists (docs/44 §5).
    note: str | None = Field(default=None, max_length=500)
    payee_cipher: str | None = None
    raw_desc_cipher: str | None = None
    amount_cipher: str | None = None
    cad_amount_cipher: str | None = None
    payee_token: str | None = None

    _validate_non_blank = field_validator("raw_description", "normalized_payee")(_non_blank)


class TransactionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    date: dt_date
    posting_date: dt_date | None
    raw_description: str
    normalized_payee: str
    amount_native: Decimal
    currency: str
    cad_amount: Decimal | None
    category_id: int | None
    # M96: the recurring badge + cadence/next-expected the prompt card writes.
    is_recurring: bool = False
    recurring_cadence: RecurringCadence | None = None
    recurring_next_date: dt_date | None = None
    # M106 (docs/02): review state. `reviewed_at IS NULL` IS "unreviewed" —
    # the row never carries a boolean that could disagree with it.
    reviewed_at: datetime | None = None
    reviewed_by: int | None = None
    transfer_pair_id: int | None = None
    pair_account_name: str | None = None
    # M108 P4 (docs/09 §Receipt images): does this row have an attached image?
    # A BOOLEAN, never the image — the list route answers "is there one" in one
    # batched query; the bytes only move on `GET /transactions/{id}/receipt`.
    has_receipt: bool = False
    # M107 (docs/44 §4.1): additive split fields. `parent_split_id` is null on
    # ordinary rows and on split PARENTS, and set on children; `is_split` is
    # true on a parent that still has its children; `note` is plaintext free
    # text (v1 exposes it on children and in the split editor).
    parent_split_id: int | None = None
    is_split: bool = False
    note: str | None = None
    tags: list[TagPublic] = []
    payee_cipher: str | None = None
    raw_desc_cipher: str | None = None
    amount_cipher: str | None = None
    cad_amount_cipher: str | None = None
    payee_token: str | None = None


class SplitParentPublic(TransactionPublic):
    """A split parent's row plus the children it has (M107, docs/44 D11).

    `child_count` is independent of the current filter — it counts the
    parent's children in the TABLE, so a content filter that returned only
    some of them still renders a header claiming the true number of parts.
    """

    child_count: int = 0


class TransactionPage(BaseModel):
    items: list[TransactionPublic]
    total: int
    limit: int
    offset: int
    # M137: rows the request left out because it asked for them to be hidden
    # (`hide_encrypted`, a locked-vault client). 0 unless asked, so every
    # existing consumer keeps its exact payload.
    withheld: int = 0
    # M107 (docs/44 §6.4): the parents of the children IN `items`, keyed by
    # the page's distinct `parent_split_id`s, ordered `date desc, id desc`.
    # `[]` when the page carries no children. Because `items` is LEDGER
    # (children + ordinary rows, never a split parent) a naive consumer that
    # sums `items` CANNOT double-count, while this array still lets a page
    # render its group headers.
    split_parents: list[SplitParentPublic] = []


# --- M107: split transactions (docs/44 §4) ------------------------------------


class SplitChildIn(BaseModel):
    """One leg of a split (docs/44 §4.1).

    Validated for SHAPE here (types, `extra=forbid`, note length); validated
    for SEMANTICS in the handler (count, sign, zero, ownership, sum) so the
    error envelope can carry the SPECIFIC code §4.3 pins instead of a generic
    `validation_error` — docs/44 §4.2 makes that ordering part of the contract.
    """

    model_config = ConfigDict(extra="forbid")

    amount_native: Decimal
    category_id: int | None = None
    note: str | None = Field(default=None, max_length=500)
    tag_ids: list[int] = []
    # Required when `users.encryption_required` (docs/44 §8 F-10); the
    # payee/raw-description ciphers and the blind index are copied
    # server-side from the parent, which already holds them.
    amount_cipher: str | None = None
    cad_amount_cipher: str | None = None


class SplitCreatePayload(BaseModel):
    """`POST` / `PUT` `/transactions/{id}/split` body (docs/44 §4.1)."""

    model_config = ConfigDict(extra="forbid")

    children: list[SplitChildIn]


class TransactionSplit(BaseModel):
    """Everything the drilldown and the two writes answer (docs/44 §4.1).

    `sum_ok` is recomputed on READ, never stored — so a test (and QA) can
    assert the D3 invariant without writing anything first.
    """

    model_config = ConfigDict(from_attributes=True)

    parent: SplitParentPublic
    children: list[TransactionPublic]  # id ASC = entry order
    split_checksum: Decimal | None
    sum_ok: bool


# --- M106: review state + the per-transaction receipt log (docs/02, docs/08) ---


class ReviewTogglePayload(BaseModel):
    """`POST /transactions/{id}/review` — one flip, one receipt."""

    model_config = ConfigDict(extra="forbid")

    reviewed: bool


class UnreviewedCount(BaseModel):
    """`GET /transactions/unreviewed-count` — the header hint and the queue
    chip read the SAME number, so the two can never disagree (docs/09)."""

    count: int


class TransactionEventPublic(BaseModel):
    """One row of the History drawer (`GET /transactions/{id}/events`).

    `at` is the event's `created_at` (TimestampMixin is mandatory on every
    table, so the log keeps the house column name while this shape uses the
    spec's — docs/02). `payload` is the human detail, `payload_hash` its
    canonical sha256, `summary` the rendered line: the payload is what makes
    the line true, the hash is what makes it checkable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tx_id: int
    event_kind: TransactionEventKind
    actor: int | None
    actor_label: str
    at: datetime | None
    payload_hash: str
    summary: str
    payload: dict = Field(default_factory=dict)


class TransactionEventPage(BaseModel):
    """Newest first — the drawer reads top-down from the last receipt written
    (docs/09 §History drawer)."""

    items: list[TransactionEventPublic]
    total: int


class SimilarTransaction(BaseModel):
    """Ranked past example (fuzzy payee match); `score` is trigram similarity."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    date: dt_date
    raw_description: str
    normalized_payee: str
    amount_native: Decimal
    currency: str
    cad_amount: Decimal | None
    category_id: int | None
    category_name: str | None
    score: float
    # Payee stored as ciphertext (E2EE tenant). Plaintext on the server, so it
    # always MATCHED — a locked client is what hides it, and it hides on this.
    encrypted: bool = False


class SimilarPage(BaseModel):
    query: str
    items: list[SimilarTransaction]
    # `total` = matches this request kept; `withheld` = matches left out only
    # because `hide_encrypted` was set. Together they are the honest
    # "Showing X of Y rows" (`total + withheld` = every match).
    total: int = 0
    withheld: int = 0


class LinkTransferPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: int


class TransferSuggestion(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: int
    source_account_id: int
    source_account_name: str | None
    source_amount: str
    source_date: str
    source_payee: str
    candidate_id: int
    candidate_account_id: int
    candidate_account_name: str | None
    candidate_amount: str
    candidate_date: str
    candidate_payee: str


class TransferSuggestionPage(BaseModel):
    items: list[TransferSuggestion]


class AcceptTransferSuggestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: int
    candidate_id: int


class TransactionTagsPage(BaseModel):
    items: list[TagPublic]


class SetTransactionTagsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_ids: list[int]


class AddTransactionTagPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: int


# --- Recurring-transactions detection (M96, docs/09 §Recurring prompt) --------


#: M138 (docs/09 §Bills you probably have) — which surface is asking.
#:
#: Both the M96 prompt card and the dashboard's subscriptions card read
#: `GET /transactions/recurring/candidates`, but they dismiss INDEPENDENTLY:
#: a `"subscriptions"` answer must never silence the prompt for the same
#: merchant (and vice versa). The source picks which suppression key the
#: route subtracts, and which key `RecurringCandidate.dismiss_key` reports so
#: the caller posts back exactly what was filtered against.
RecurringSource = Literal["recurring_prompt", "subscriptions"]


class RecurringCandidate(BaseModel):
    """One candidate group the prompt card offers to tag.

    Shapes are built by the handler from `services/recurring.py::RecurringGroup`
    (a frozen dataclass), so `from_attributes` is not involved here.
    """

    group_key: str
    #: The key THIS surface suppresses under (`services/recurring.py`):
    #: `group_key` for `recurring_prompt`, `make_subscriptions_group_key(payee)`
    #: for `subscriptions`. Always populated so a caller never re-derives the
    #: normalization client-side.
    dismiss_key: str
    payee: str
    cadence: RecurringCadence
    occurrence_count: int
    transaction_ids: list[int]
    monthly_total: Decimal
    first_date: dt_date
    last_date: dt_date
    next_expected_date: dt_date


class RecurringCandidatePage(BaseModel):
    groups: list[RecurringCandidate]
    # Combined monthly total of the groups SHOWN (signed: debits negative).
    combined_monthly_total: Decimal
    # Groups this scan found but hid because the user already declined them.
    suppressed_count: int
    window_start: dt_date


class RecurringAcceptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str = Field(min_length=1, max_length=120)
    transaction_ids: list[int] = Field(min_length=1)
    cadence: RecurringCadence


class RecurringAcceptResult(BaseModel):
    updated: int
    group_key: str
    cadence: RecurringCadence
    next_expected_date: dt_date | None


# --- M108 P4: receipt images (docs/09 §Receipt images, docs/08) ---------------
#
# NOTE on the word "receipt": M106's `transaction_events` rows are also called
# receipts in this codebase — ACTIVITY receipts, an append-only audit line.
# Everything below is the photographed paper one. Neither shares a route,
# column or helper.

# The only content types a receipt may carry. The client re-encodes to webp
# before it POSTs, but the server cannot trust that: a `text/html` body
# masquerading as an upload must never be stored and later served back from
# our own origin with an image content type.
RECEIPT_CONTENT_TYPES = frozenset({"image/webp", "image/jpeg", "image/png", "image/gif"})

# 2 MB hard cap per upload (M108 P4). Checked twice — against the declared
# `Content-Length` before a byte is read, and against the actual body after —
# because a chunked request can declare nothing and still be huge.
RECEIPT_MAX_BYTES = 2 * 1024 * 1024


class TransactionReceiptPublic(BaseModel):
    """A receipt WITHOUT its bytes (docs/08): the list route carries
    `has_receipt`, and this is what PUT/DELETE answer. The image itself only
    ever moves on `GET /transactions/{id}/receipt`."""

    model_config = ConfigDict(from_attributes=True)

    transaction_id: int
    content_type: str
    size_bytes: int


class RecurringSuppressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str = Field(min_length=1, max_length=120)


# --- M125: server-side bulk over a matched set (docs/09 §Bulk selection
# toolbar §Select-all-matched, docs/08 §Bulk over a matched set) -----------------
#
# M96's toolbar drives the SINGLE-row routes one id at a time. That is the
# right shape for an explicit page of ids (≤ `PAGE_SIZE` rows, a visible
# `n / total` readout, one conflict per row the server refuses) and it stays
# exactly as it was. It is the WRONG shape for "every row the filter matches":
# 345 rows would be 345 authenticated requests on the one surface an
# import-heavy user hammers hardest.
#
# So the matched set gets its own execution: ONE request whose body carries
# the FILTER the page is showing plus the ids the user unchecked out of it —
# never the 345 ids themselves. The count the confirmation prints is the
# server's own `matched`, so a body can never destroy a set bigger than the
# one the person read.


class BulkFilter(BaseModel):
    """The page's filter model as a JSON body.

    Field-for-field the SAME contract as `GET /transactions`' query params
    (docs/08) — same names, same types, same "AND-compose" meaning — because
    both are folded into `TxnFilters` by ONE builder (`_build_txn_filters`).
    A bulk run can therefore only ever select the rows the page would render.

    `category_id` keeps the query contract's spelling: an int id, or the
    STRING `"null"` for "uncategorized". A JSON `null` means "no category
    filter at all", exactly like omitting `?category_id=`.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: int | None = None
    category_id: int | Literal["null"] | None = None
    from_: dt_date | None = Field(default=None, alias="from")
    to: dt_date | None = None
    payee: str | None = Field(default=None, min_length=1)
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    direction: Literal["paid", "received"] | None = None
    tag_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    payee_token: str | None = None
    reviewed: bool | None = None
    # Declared SO THAT it fails the way `GET /transactions` fails. `q` is not
    # part of this contract (docs/33 S5 — fuzzy scoring runs in the browser),
    # and a stale client that still sends it gets the SAME 422 `q_removed`
    # rather than a silently unfiltered bulk run.
    q: str | None = None


class BulkSelection(BaseModel):
    """WHAT a bulk run acts on: the page's filter, minus the rows unchecked.

    `exclude_ids` is the "exceptions list" (docs/09): while the matched set is
    marked, a row the user deselects is NOT dropped from the selection — it
    becomes an exception, so the set stays server-described and the counter
    stays honest (`matched - len(exclude_ids)`).
    """

    model_config = ConfigDict(extra="forbid")

    filter: BulkFilter
    # An exception is a row a person unchecked by hand, one click at a time;
    # 5000 is far past any human click-path and keeps the `NOT IN` list that
    # carries them bounded.
    exclude_ids: list[int] = Field(default_factory=list, max_length=5000)


class BulkRecategorizePayload(BulkSelection):
    model_config = ConfigDict(extra="forbid")

    category_id: int | None


class BulkPayeePayload(BulkSelection):
    model_config = ConfigDict(extra="forbid")

    normalized_payee: str = Field(min_length=1)
    # E2EE (docs/33): ONE ciphertext for the whole run — every row gets the
    # same new payee, so the blind index and its ciphertext are row-independent
    # (amount/raw-description ciphers are untouched: a payee rename moves no
    # money and no description).
    payee_cipher: str | None = None
    payee_token: str | None = None


class BulkReviewPayload(BulkSelection):
    model_config = ConfigDict(extra="forbid")

    reviewed: bool


class BulkTagPayload(BulkSelection):
    model_config = ConfigDict(extra="forbid")

    tag_id: int


class BulkDeletePayload(BulkSelection):
    """`expected_count` is the count-aware gate (docs/09 §Destructive actions).

    The confirmation the user read said "Delete 345 transactions?". The body
    repeats that number, and the server refuses to destroy a set that no
    longer matches it — so a ledger that moved between the read and the click
    can never delete rows nobody confirmed. `409 bulk_count_mismatch` writes
    NOTHING (the check runs before the first row is touched).
    """

    model_config = ConfigDict(extra="forbid")

    expected_count: int = Field(ge=0)


class BulkSkippedRow(BaseModel):
    """One row the server refused, in the M96 conflict shape + a label.

    `label` is the row's own `normalized_payee` — the same field `GET
    /transactions` returns for every row — so a skipped row on page 7 of a
    matched set is still NAMED in the notice instead of printed as `#4711`.
    """

    id: int
    label: str
    code: str | None
    detail: str


class BulkResult(BaseModel):
    """What one bulk run did.

    `matched` is the SERVER's count of the selection — the same number the
    pagination's `total` reports when `exclude_ids` is empty, and the number
    the delete confirmation is checked against. `updated` is the rows the run
    actually processed without a refusal; `skipped` are the ones it refused,
    each carrying the server's own `{detail, code}` (M96's partial-success
    report, now computed in one request instead of N).
    """

    matched: int
    updated: int
    skipped: list[BulkSkippedRow] = Field(default_factory=list)
