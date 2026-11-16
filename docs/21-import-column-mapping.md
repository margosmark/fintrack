# 21 — Import Column-Mapping Wizard + LLM Suggestions

Self-service CSV import for institutions that have no stored `importer_profile`.
When an uploaded CSV cannot be parsed (the importer raises `MissingProfileError`),
the Imports page launches a **column-mapping wizard**: it posts the CSV to the
backend, which suggests a column→field mapping (via an LLM when one is configured,
otherwise a deterministic heuristic), the user confirms/adjusts the mapping, and the
upload is retried with that mapping embedded as `profile_json`. No new model or
migration — the confirmed `CsvProfile` is persisted as JSON on
`institutions.importer_profile` (docs/03). See plan
`local://doc-reconciliation-plan.md` §3 (`docs/21`).

## Context

Generic CSV importing (docs/03) requires a per-institution `CsvProfile`
(`institutions.importer_profile`). Most institutions have none, so a naive upload
fails with `422 missing_importer_profile`. Rather than force every user to hand-write
JSON, the wizard closes the loop in-product:

1. User uploads a CSV with no institution profile → backend raises
   `missing_importer_profile`.
2. Frontend catches that code and calls `POST /imports/suggest-profile` to get a
   suggested mapping (LLM if the user has an LLM provider configured, else
   heuristic keyword/date/decimal detection).
3. `ColumnMappingWizard` renders the headers + suggested mapping; user picks the
   date / description / amount (signed or split debit+credit) / posting-date /
   currency columns, date format, and decimal-comma / credits-positive flags.
4. `POST /imports/upload` is retried with `profile_json` set to the confirmed
   `CsvProfile`; rows are staged.

Decisions (locked with current code):
- **Suggestion is best-effort, never blocking.** LLM failure falls back to the
  heuristic automatically; empty/garbage CSVs return `columns={}` with
  `confidence=0.0` and a human reason. The user can always override in the wizard.
- **Two amount modes.** `signed` (one `amount_native` column) or `split`
  (separate `debit` + `credit`), exactly as `CsvProfile._validate_mode` enforces
  (the two are mutually exclusive).
- **Profile is the source of truth at upload.** `profile_json` overrides the
  institution's stored profile when present; otherwise the institution's stored
  profile is used.
- **Wizard can save the profile back to the institution** (`saveToInstitution`
  checkbox) so future uploads for that institution skip the wizard.

## Approach

### Backend

`app/api/imports.py` — router prefix `/imports` (full path `/api/v1/imports/...`).

**`POST /imports/suggest-profile`**
- Auth: `Depends(get_current_user)` (tenant-scoped read).
- Request: `multipart/form-data`, single field `file` (`UploadFile`, the CSV).
- Logic:
  - `not (file.filename or "").lower().endswith(".csv")` → raise
    `api_error(422, "Only CSV files are supported", "not_csv")`.
  - `raw = await file.read()`; `len(raw) > 5_000_000` → raise
    `api_error(413, "File exceeds the 5 MB limit", "file_too_large")`.
  - `resolved = build_provider(current_user, get_settings())` (resolves the user's
    configured LLM provider + model; `resolved.provider` is `None` when none is
    configured).
  - `result = suggest_column_mapping(raw, resolved.provider, resolved.model)`.
  - Returns `SuggestProfileResponse(**result)`.
- Errors: `not_csv` (422), `file_too_large` (413).

**`POST /imports/upload`** (existing staged-upload endpoint, extended for the wizard)
- Request: `multipart/form-data` with `file` (`UploadFile`), `account_id`
  (`int` Form field), and optional `profile_json` (`str | None` Form field).
- If `profile_json is not None`: `CsvProfile.model_validate_json(profile_json)`;
  on `pydantic.ValidationError` → `api_error(422, f"Invalid profile:
  {profile_error_message(exc)}", "invalid_profile")`. The validated profile
  (`.model_dump()`) becomes the parse context. `profile_error_message`
  (`app/importers/csv_profile.py`) renders the FIRST error as one actionable
  line and rewrites the one the mapping wizard can actually produce:
  `columns.<key> must be a non-empty header name` →
  `<key> column mapping is empty — set the <Key> column [or switch to <other>
  mode]`. Every other message passes through verbatim. Pydantic's own record
  (`1 validation error for CsvProfile … [input_value=…]`) is NEVER surfaced —
  it is internals, and it is what the M111 report quoted. The parser path
  (`CsvImporter.parse`) uses the same renderer for
  `invalid importer_profile: …`.
- If `profile_json is None`: falls back to `institution.importer_profile` (stored
  JSON) when the account's institution has one.
- Errors (full set, for reference): `file_too_large` (413 > 5 MB), `invalid_profile`
  (422 bad `profile_json`), `unsupported_format` (422 unrecognized bytes),
  `missing_importer_profile` (422 no profile and none supplied), `parse_error`
  (422 `CsvProfileError` while parsing rows), `too_many_rows` (413 > 2000 rows).

`app/schemas/imports.py` — `SuggestProfileResponse`:
```python
class SuggestProfileResponse(BaseModel):
    columns: dict[str, str]          # e.g. {"date": "Date", "raw_description": "Desc",
                                     #       "amount_native": "Amount"}
    date_format: str = "%Y-%m-%d"
    decimal_comma: bool = False
    credits_positive: bool = False
    headerless: bool = False             # True => columns map by 1-based position (no header row)
    confidence: float = 0.0          # 0.0–1.0
    reasoning: str = ""
    format: str | None = None        # known export format that resolved this
                                     # mapping (docs/58 §A), else None
```
`format` is set when `app/importers/format_presets.py` recognised the file
(for now `"monarch"`): the mapping is then deterministic and named rather
than guessed, but it is still only a **suggestion** — the confirmation gate
below is unchanged.
`headerless` is `True` when the source CSV has no header row; in that case
`columns` values are 1-based column indices (`"1"`, `"2"`, …) rather than header
names, and the wizard renders positional `Column N` selectors. When `headerless`
is `True` the LLM is skipped entirely — mapping is deterministic/offline
(`_heuristic_positional_mapping`, `confidence=0.4`).
`columns` keys are a subset of
`{date, raw_description, amount_native, debit, credit, posting_date, currency}`;
`debit` + `credit` are only present together in split mode, and `amount_native`
only in signed mode.

`app/services/column_mapping.py`:
- `suggest_column_mapping(raw: bytes, provider, model: str = "") -> dict[str, Any]`
  — returns a dict shaped like `SuggestProfileResponse`.
  - `_extract_sample_rows(raw, max_rows=5)` reads CSV rows via `csv.reader` and
    returns `(headers, rows, has_header)`. For a headerless file it synthesizes
    `col_{i+1}` header names and keeps the first row as data (`has_header=False`);
    for a headerful file it uses the first row as headers. Empty input →
    `([], [], False)`.
  - `_is_headerless(first_row)` — heuristic: a header keyword anywhere ⇒ header;
    else any date/number-looking cell ⇒ headerless. The wizard's manual
    "no header row" checkbox is the safety net for misdetection.
  - If `has_header is False`: return `_heuristic_positional_mapping(headers, rows)`
    (deterministic/offline; **LLM skipped**). `confidence=0.4`, `headerless=True`,
    `reasoning="No header row detected; columns mapped by position from sample
    data."`.
  - Else if `preset_suggestion(headers, rows)` (docs/58 §A, `app/importers/
    format_presets.py`) recognises the file: return the preset — `confidence=0.95`,
    `format=<slug>`, `credits_positive`/`date_format` taken from the format, and a
    `reasoning` that says to review and confirm. **Deterministic: no LLM, no
    network** — a known format is answered before the provider is consulted.
  - Else if `provider is not None`: try `_llm_suggest(...)`; on **any** exception,
    log a warning and fall back to `_heuristic_mapping`.
  - Else `_heuristic_mapping(headers, rows)`.
  - `_infer_date_format(sample)` — extracted helper: `%m/%d/%Y` if
    `\d{2}/\d{2}/\d{4}`, `%Y.%m.%d` if `\d{4}\.\d{2}\.\d{2}`, `%d-%b-%Y` if
    `\d{2}-[A-Za-z]{3}-\d{4}`, else `%Y-%m-%d`. Shared by both heuristics.
  - `_heuristic_positional_mapping(headers, rows)` — scores each column over the
    sample rows (`date_score`/`num_score`/`text_score`); picks `date` = max
    date-score, `amount_native` = max num-score among non-date columns,
    `raw_description` = a text-leaning unchosen column (fallback: first unchosen).
    Returns `columns` as **1-based index strings** and `headerless=True`.
- `COLUMN_MAPPING_SCHEMA: dict` — the JSON-schema constraint handed to the LLM.
  Properties: `columns{date, raw_description, amount_native, debit, credit,
  posting_date, currency}` (all `string`), plus `date_format` (string),
  `decimal_comma` (boolean), `credits_positive` (boolean), `confidence` (number),
  `reasoning` (string). `required = [columns, date_format, decimal_comma,
  confidence, reasoning]`; `additionalProperties = False`.
- `_heuristic_mapping(headers, rows)` — keyword sets:
  `_DATE_KEYWORDS={date,trans,posted,transaction,value,booking}`,
  `_DESC_KEYWORDS={desc,memo,narrative,payee,detail,particulars,reference}`,
  `_AMOUNT_KEYWORDS={amount,total,sum,value,balance}`,
  `_DEBIT_KEYWORDS={debit,withdrawal,out}`,
  `_CREDIT_KEYWORDS={credit,deposit,in}`,
  `_CURRENCY_KEYWORDS={currency,ccy,curr}`,
  `_POSTING_KEYWORDS={posting,settlement,settled}`.
  - Picks first header containing any keyword (case-insensitive, trimmed).
  - Split vs signed: if both a debit- and credit-keyword header are found →
    `columns["debit"]` + `columns["credit"]`; otherwise an amount-keyword header →
    `columns["amount_native"]`.
  - `date_format` guessed from the first non-empty date sample:
    `%m/%d/%Y` if `\d{2}/\d{2}/\d{4}`, `%Y.%m.%d` if `\d{4}\.\d{2}\.\d{2}`,
    `%d-%b-%Y` if `\d{2}-[A-Za-z]{3}-\d{4}`, else `%Y-%m-%d`.
  - `decimal_comma = True` if an amount column value contains `,` but no `.`.
  - Returns `confidence=0.4`, `reasoning="Heuristic fallback based on column name
    patterns."`, `credits_positive=False`. (Headerful path only — headerless
    files never reach this function; see `_heuristic_positional_mapping`.)
- `_llm_suggest(headers, rows, provider, model)` — builds a system+user prompt
  describing the target fields and rules, calls `provider.chat(messages,
  COLUMN_MAPPING_SCHEMA)`, validates that `columns` is a `dict`, and normalizes the
  result (defaults `date_format="%Y-%m-%d"`, `confidence=0.5` if absent).

`app/importers/registry.py` — `detect_importer(raw)` sniffs bytes; order is
`("ofx", OfxImporter)` then `("csv", CsvImporter)`. `CsvImporter.detect` explicitly
rejects OFX bytes so OFX wins when ambiguous. `get_importer(fmt)` looks up by key.

`app/importers/format_presets.py` — known export formats, consulted **before**
the LLM (docs/58 §A):
- `normalize_header(header)` — lowercase, strip a trailing `(…)`, collapse
  internal whitespace (the same normalization YNAB's parser uses).
- `FormatPreset(slug, label, columns, required, signature,
  date_format_from_samples, credits_positive, confidence)`; `resolve(headers)`
  maps each logical field to the file's own header name (first candidate wins)
  and returns `None` unless every `required` logical field resolved **and** at
  least one `signature` header exists — the signature is what stops an ordinary
  bank CSV (`date, description, amount`) from being claimed as a known format.
- `MONARCH` — the only pinned preset today: required `date` + `amount_native` +
  `raw_description` (candidates `merchant`, `original statement`,
  `description`), signature `merchant`/`original statement`,
  `credits_positive=True` (Monarch exports expenses as negative numbers),
  `date_format` inferred from the file's samples.
- `infer_date_format(samples)` — one strptime format that parses EVERY sample:
  numeric shapes are resolved by the >12 rule (a component >12 can only be the
  day; an ambiguous pair goes month-first, which is Monarch's own default),
  then the text-month formats. Returns `None` rather than guessing — the caller
  falls back to `%Y-%m-%d` and the wizard's confirmation still applies.
- `preset_suggestion(headers, rows)` — a full `SuggestProfileResponse`-shaped
  dict (`format=<slug>`, `confidence=0.95`, `reasoning` naming the format) or
  `None`.

`app/importers/csv_profile.py` — `CsvProfile` (the validated, persisted shape):
```python
class CsvProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, str]                 # {logical: header OR 1-based index}, REQUIRED: date + raw_description
    date_format: str = "%Y-%m-%d"
    decimal_comma: bool = False
    credits_positive: bool = False          # ignored in split mode
    headerless: bool = False                # True => columns values are 1-based positions
    signs: dict[str, Any] | None = None     # split mode only; vals ∈ {-1, 1}, keys ⊆ {debit, credit}
```
- `headerless: bool = False` — when `True`, `columns` values are **1-based column
  indices** (`"1"`, `"2"`, …) and `parse_rows` takes the positional path
  (`_parse_rows_positional`, via `csv.reader`, no header row consumed). When
  `False`, `columns` values are header names (original behavior). `extra="forbid"`
  means older stored profiles (no flag) default to `False` and stay valid.
- `REQUIRED_COLUMNS = ("date", "raw_description")` — always required in `columns`.
- `_resolve_mode_conflict` (auto-resolves): when both `amount_native` AND `debit`+`credit` are present (from LLM or stored profiles), removes `amount_native` preferring split mode. Then `_validate_mode` (mode rule): `amount_native` XOR `debit`+`credit`. Both present →
  `422`-style `CsvProfileError` "either amount_native (signed amount) or
  debit+credit (split columns), not both". Neither signed nor full split → error
  listing the missing keys.
- `_validate_signs` (split mode): each `signs` value must be exactly `-1` or `1`
  (booleans rejected) and keys limited to `{debit, credit}`; `None` → `DEFAULT_SIGNS
  = {"debit": -1, "credit": 1}`.
- `CsvProfileError(ValueError)` — base for profile/parse failures surfaced as the
  `422` envelope by `api_error`. `MissingProfileError(CsvProfileError)` — raised when
  the file needs a profile but none is supplied/stored.
- Persisted as JSON on `institutions.importer_profile`; `CURRENCY_PATTERN =
  re.compile(r"[A-Z]{3}")` (used by the importer for the optional `currency` column).
- `_parse_rows_positional(raw, profile)` — headerless parse path: `csv.reader`
  (no header consumed), per-row index-based `cell`/`opt_cell`, blank-row skip,
  and the same date/sign/split/currency logic as the headerful `parse_rows`.
- `_parse_magnitude(text, profile)` / `_parse_amount(text, profile)` — the
  signed-amount parser: strips spaces/NBSP/`$`; **accounting parentheses are a
  negative number** (`(58.12)` → `-58.12`, applied whatever the inner sign and
  before the `credits_positive` inversion — docs/58 §A); with
  `decimal_comma=True` `.` is a thousands separator and `,` the decimal mark;
  anything unparseable raises `CsvProfileError` (422, never a silent row).
  `_parse_amount` then applies the file's convention: `credits_positive=True`
  → the file's sign is already canonical, else it is inverted.
- `_validate_headerless` (header rule): when `headerless` is `True`, every
  `columns` value must match `^\d+$` and parse to `≥ 1`, else
  `columns.{key} must be a 1-based column index when headerless=true, got
  {value!r}`; when `False`, every value must be a non-empty stripped header name.
### Frontend

`frontend/src/api/imports.ts`:
- `suggestProfile(file: File): Promise<SuggestProfileResponse>` — `multipart/form-data`
  field `file` → `POST /imports/suggest-profile` (`apiForm`).
- `uploadImportWithProfile(file: File, accountId: number, profile: Record<string, unknown>)`
  — same as `uploadImport` but appends `profile_json = JSON.stringify(profile)`.
- `uploadImport(file: File, accountId: number)` — plain upload (no profile).

`frontend/src/pages/Imports.tsx`:
- `ColumnMappingWizard({ headers, suggestedProfile, onConfirm, onCancel, saving })`:
  - `MappingMode = "signed" | "split"` (SegmentedControl). Required columns:
    split → `[date, raw_description, debit, credit]`; signed →
    `[date, amount_native, raw_description]`. Confirm disabled while any required
    column is unselected.
  - Selects for `date`, `raw_description`, `amount_native` (signed) or `debit` +
    `credit` (split), `posting_date` (optional), `currency` (optional); a
    `date_format` text input (placeholder `%Y-%m-%d`); checkboxes `decimalComma`,
    `creditsPositive` (signed only), `saveToInstitution`, and (when headerless)
    "This file has no header row".
  - **Headerless support.** When `suggestedProfile.headerless` is `True` (auto-detected
    by `_is_headerless`), the column `<select>` options are rendered as positional
    `Column N` labels (1-based) instead of header names, and a one-line note explains
    columns map by position. A "This file has no header row" checkbox (`draft.headerless`)
    lets the user override detection manually; toggling it clears `draft.columns` because
    name vs position semantics differ. Toggling off restores the real header `<select>`s.
  - Confidence badge: `>= 0.7` "High confidence" (success), `>= 0.4` "Review needed"
    (warn), else "Low confidence" (danger); shows `suggestedProfile.reasoning` or a
    `confidence%` line.
  - **`columns` is built by MODE, never spread (M111).** Two key lists decide
    the payload: `BASE_COLUMNS = [date, raw_description]` +
    `OPTIONAL_COLUMNS = [posting_date, currency]` + the mode's own
    (`signed → [amount_native]`, `split → [debit, credit]`). A key whose value
    is blank is OMITTED — an empty string must never reach the API, because
    `_validate_headerless` rejects it. Unknown keys (a future suggester field)
    pass through only when populated, and never the other mode's keys.
  - `columnsForMode(columns, mode)` deletes the other mode's keys AND every
    blank mapping. It runs when a suggestion is ingested and at EVERY mode
    switch (the SegmentedControl `onChange`), so `draft.columns` cannot
    accumulate stale state — the draft record is persistent and that is exactly
    how `credit: ""` used to survive a switch to single-amount.
  - `draftFromSuggestion(sug)` → mode = `split` iff both `debit` and `credit` are
    populated in `sug.columns`, else `signed`; `headerless = sug.headerless ?? false`;
    `columns` is passed through `columnsForMode` first, so the suggester cannot
    seed a blank or cross-mode key.
  - `draftToProfile(draft)` → split returns
    `{ columns: payloadColumns(draft), date_format, decimal_comma, headerless }`;
    signed adds `credits_positive`.
    (`ImporterProfile` shape, `frontend/src/api/institutions.ts`.)
  - `onConfirm(profile: ImporterProfile, saveToInstitution: boolean)`.
- `fetchHeaders` mutation → `suggestProfile(file)`; on success builds `headers` as
  `{ label, value }` pairs from the file's first line (always the real header names)
  and opens the wizard (`mappingWizard = { headers, profile: result }`). The headerless
  guess travels via `result.headerless` → `suggestedProfile.headerless` → `draft.headerless`;
  positional `Column N` options are synthesized from `headers.length` in the wizard.
- Upload/preview error handling (M112): both `runPreview.onError` and
  `upload.onError` call `isMissingImporterProfile(err)` — `err instanceof ApiError
  && err.code === "missing_importer_profile"`, i.e. the envelope's machine-readable
  **`code`** (docs/08), never its human `detail` — and on a match
  `fetchHeaders.mutate(file)` auto-launches the wizard (bank-statement accounts
  only; the investment branch toasts first). Any other error → `toast.error`.
  `apiErrorMessage(err)` returns `ApiError.message`, which is `super(detail)` — the
  `detail` text here is `"institution has no importer_profile for CSV import"`, so
  the pre-M112 `msg.includes("missing_importer_profile") || msg.includes("No
  importer profile")` test could not match either token (the second does not exist
  in the backend at all) and the branch was dead: the 422 fell through to a toast
  and the wizard never opened.
- `uploadWithProfile` mutation → `uploadImportWithProfile(file, accountId, profile)`;
  on success sets the staged `UploadResponse` preview.
- Inline "+ New account" creation mid-flow (`createInlineAccount` → `createAccount`)
  so a CSV can be imported to a freshly created account without leaving the wizard.
`frontend/src/api/institutions.ts` — `ImporterProfile`:
```ts
interface ImporterProfile {
  columns: Record<string, string>;
  date_format?: string;
  decimal_comma?: boolean;
  credits_positive?: boolean;     // signed mode only
  headerless?: boolean;           // True => columns values are 1-based positions
  signs?: ProfileSigns;           // split mode only; {debit,credit} ∈ {-1,1}
}
```

## Touch points

- `backend/app/api/imports.py` — `POST /imports/suggest-profile` (not_csv,
  file_too_large); `POST /imports/upload` `profile_json` + `invalid_profile`.
- `backend/app/schemas/imports.py` — `SuggestProfileResponse`.
  `COLUMN_MAPPING_SCHEMA`, `_heuristic_mapping`, `_llm_suggest`, `_extract_sample_rows`,
  `_is_headerless`, `_heuristic_positional_mapping`, `_infer_date_format`.
- `backend/app/importers/registry.py` — `detect_importer`, `get_importer`.
- `backend/app/importers/format_presets.py` — `FormatPreset`, `MONARCH`,
  `PRESETS`, `preset_suggestion`, `infer_date_format`, `normalize_header`.
- `backend/tests/test_mapping_confirmation_gate.py` — the gate: suggest names
  the format and writes nothing; upload without `profile_json` is 422; only
  `/{batch_id}/commit` reaches the ledger.
- `backend/app/importers/csv_profile.py` — `CsvProfile`, `CsvProfileError`,
  `MissingProfileError`, `REQUIRED_COLUMNS`, `OPTIONAL_COLUMNS`, `_validate_mode`,
  `_validate_signs`, `_drop_empty_optionals`, `_validate_headerless`,
  `profile_error_message`, `parse_rows`, `_parse_rows_positional`.
- `frontend/src/api/imports.ts` — `suggestProfile`, `uploadImportWithProfile`.
- `frontend/src/api/institutions.ts` — `ImporterProfile`, `ProfileSigns`.
- `frontend/src/pages/Imports.tsx` — `ColumnMappingWizard`, `fetchHeaders`,
  `uploadWithProfile`, `draftFromSuggestion`, `draftToProfile`.

## Verification

- **Endpoint existence (code):**
  `grep -rn '@router.post' backend/app/api/imports.py` →
  `/suggest-profile` and `/upload` both present; `suggest-profile` signature takes
  `file: UploadFile`.
- **Error-code existence (code):**
  `grep -rn 'api_error(422, .*, "not_csv")|api_error(413, .*, "file_too_large")|api_error(422, .*, "invalid_profile")|api_error(422, .*, "missing_importer_profile")' backend/app`
  → each appears in `app/api/imports.py` (`not_csv`/`file_too_large` in
  `suggest_profile`; `invalid_profile` in `upload`; `missing_importer_profile` in
  `_parse_file`).
  exposes exactly `columns, date_format, decimal_comma, credits_positive, headerless,
  confidence, reasoning`; `CsvProfile.columns` requires `date` + `raw_description` and
  forbids `amount_native` together with `debit`/`credit` (validators `_validate_mode`).
  - Headerless flag: `grep -n "headerless" backend/app/schemas/imports.py
    backend/app/importers/csv_profile.py` → present on `SuggestProfileResponse` and
    `CsvProfile`; `_validate_headerless` enforces index strings when `headerless=True`.
- **Backend unit (running stack, no UI):**
  - Upload an unprofiled CSV (`date,description,amount` with `1/2/2024` dates) →
    `POST /imports/upload` (no `profile_json`) returns `422 missing_importer_profile`.
  - `POST /imports/suggest-profile` with that file → `200` with
    `columns.date`, `columns.raw_description`, `columns.amount_native` populated,
    `date_format == "%m/%d/%Y"`, `confidence` a float, `reasoning` non-empty.
  - `POST /imports/upload` with `profile_json = SuggestProfileResponse.columns` mapped
    back into a `CsvProfile` → `201` with a staged `UploadResponse.preview` of the
    parsed rows.
  - Bad `profile_json` (e.g. `{"columns": {"date": "Date"}}` missing
    `raw_description`, or both `amount_native` and `debit`) →
    `422 invalid_profile` (`ValidationError` from `_validate_mode`); the
    `detail` is the plain-language line, never pydantic's record.
  - Non-`.csv` filename → `422 not_csv`; a > 5 MB file → `413 file_too_large`.
  - LLM disabled (privacy mode / no provider): suggestion still returns `200` via the
    heuristic (`confidence == 0.4`, reasoning "Heuristic fallback…").
- **Headerless (no header row):**
  - Detection: a 3-column no-header CSV (`2024-01-05,COFFEE,-12.50` …) →
    `POST /imports/suggest-profile` returns `headerless == True` and `columns ==
    {"date": "1", "raw_description": "2", "amount_native": "3"}` (positional, LLM skipped).
  - Parse: `CsvProfile(columns={"date":"1",...}, headerless=True)` over that file →
    `CsvImporter.parse` yields one `RawTransaction` per data row (no header consumed).
  - Validation: a headerless profile with a non-numeric `columns` value (e.g. `"Date"`)
    is rejected by `_validate_headerless` (`422`).
  - End-to-end: upload the no-header CSV to an account whose institution has no profile
    → wizard opens with `Column 1 / Column 2 / Column 3` selectors; confirming stages 3
    rows. Toggling "This file has no header row" on a headerful-looking file switches the
    selectors to positional and clears the mappings.
- **End-to-end (running stack):**
  - On Imports, upload a CSV for an institution with no profile → wizard opens
    (`422 missing_importer_profile` caught on the envelope `code`). Suggested
    mapping is shown; user maps the
    columns and clicks "Confirm mappings" → staged preview appears, and (if
    "Save as institution profile" checked) a subsequent upload for that institution
    skips the wizard.
  - `npx tsc --noEmit` + `npx eslint . --max-warnings 0` clean for the frontend.
- **M111 — payload shape + friendly error:**
  - Unit (frontend) `frontend/tests/m111StaleProfileColumns.test.tsx` — renders
    `ColumnMappingWizard` and asserts the CONFIRMED profile's `columns`:
    `signed → split → signed` round trip carries `[amount_native, date,
    raw_description]` (no `debit`/`credit`); `signed → split` carries
    `[credit, date, debit, raw_description]` (no `amount_native`); a suggestion
    seeded with a populated `debit` and a blank `credit` in signed mode ships
    neither; and NO payload value is ever `""`. Each assertion was proven real
    by reverting `payloadColumns`/`columnsForMode`/the mode `onChange` to the
    old spread — 6/6 failed.
  - Unit (backend) `backend/tests/test_csv_profile_errors.py` — the stale
    `profile_json` above → `422 invalid_profile` with
    `detail == "Invalid profile: credit column mapping is empty — set the
    Credit column or switch to single-amount mode"` and no
    `validation error for CsvProfile` / `input_value`; the parser path returns
    the same line; a split profile with `posting_date: ""` / `currency: "   "`
    validates with those keys ABSENT; a blank REQUIRED key still 422s. Proven
    real by removing `profile_error_message`'s rewrite (2 fail) and by deleting
    `_drop_empty_optionals` (1 fail).
  - Live: on the deployed build, switch the wizard's Amount columns control
    through Debit & Credit and back, then Confirm → staged preview, no 422.
- **M112 — auto-launch keyed off the envelope `code` (this change):**
  - Unit (frontend) `frontend/tests/m112MissingProfileWizard.test.tsx` — renders
    the whole `Imports` page (real `<input type=file>`, real account `<select>`,
    the real submit button) and drives BOTH channels:
    `runPreview` rejecting with `ApiError(422, "institution has no
    importer_profile for CSV import", "missing_importer_profile")` →
    `suggestProfile` IS called exactly once and `Column Mapping` renders, with
    NO "Preview failed" toast (the prod case: the DETAIL carries no token);
    `upload.onError` — Confirm after a successful dry run — opens it too;
    a detail that CONTAINS the token under a different `code`, and any other
    code, must NOT open it (they toast). `isMissingImporterProfile` is then
    asserted token-by-token (detail-without-code → true, code-without-detail →
    false, `code: null` → false, plain `Error` → false, `null` → false).
    **5 tests.** Proven real by restoring the old
    `msg.includes("missing_importer_profile") || msg.includes("No importer
    profile")` predicate → **4/5 fail** (the fifth is the negative "other code
    still toasts" case, which passes either way by design).
  - Gates: `ruff format --check` + `ruff check` clean (368 files);
    `tsc --noEmit` + `eslint . --max-warnings 0` exit 0; frontend suite
    **94 files, 987 passed, 1 skipped**; backend suite **1289 passed** (local
    PG17); `npm run build` exit 0. Re-run after rebasing onto `4e5a84c` (a
    docs-only sibling commit; `backend/` byte-identical to the tree the 1289
    came from): lint + suite + build all green again.
  - Live A/B on the deployed build — headed Chrome, ONE pinned tab, throwaway
    account created and deleted by the probe (net zero rows left behind):
    - **before** (live bundle `index-BOyoO_GU.js`, example.internal `5dfbbd4`) —
      **8/8 checks**: `POST /imports/preview → 422`, `POST /imports/suggest-profile`
      **never fired**, `Column Mapping` absent, `Preview failed` toast shown.
      The reported symptom, reproduced independently of the card that reported it.
    - **after** (live bundle `index-uuwxNf3V.js`, example.internal `f030793`) —
      **9/9 checks**: the same click → `preview → 422` **then**
      `suggest-profile → 200`, `Column Mapping` rendered with Date /
      Description / Amount pre-filled, **no** toast. Screenshot
      `m112_verify.png` (wizard open, `Confirm mappings` waiting).
    - API channel: `POST /imports/preview` for an unprofiled institution
      answers `422` / `missing_importer_profile` with `detail ==
      "institution has no importer_profile for CSV import"` — the detail
      carries no code token, which is the whole confusion.
- **Regression:** existing import flow (OFX upload, profiled-CSV upload via
  `institution.importer_profile`) is unchanged — `profile_json` is purely additive.
- **Format presets (docs/58 §A):** `POST /imports/suggest-profile` with
  `tests/fixtures/monarch_transactions.csv` → 200 with `format == "monarch"`,
  `columns == {"date": "Date", "amount_native": "Amount", "raw_description":
  "Merchant"}`, `date_format == "%m/%d/%Y"`, `credits_positive is True`,
  `confidence == 0.95` — and zero rows written. The same call over a plain
  `date, description, amount` CSV → `format is None` (signature not met), so the
  LLM/heuristic path answers exactly as before. Pinned by
  `tests/test_mapping_confirmation_gate.py`.
- **Parentheses:** `_parse_amount("(4.50)", credits_positive=True)` →
  `Decimal("-4.50")` — pinned end-to-end by the Monarch round trip in
  `tests/test_file_import_formats.py` (the `Coffee Hut` row).
