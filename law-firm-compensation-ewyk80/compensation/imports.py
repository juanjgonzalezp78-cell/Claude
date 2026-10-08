"""TimeSolv import pipeline: read, auto-map, validate, normalize and store.

TimeSolv exports differ between report types, firm customizations and
versions, so nothing here assumes fixed column names.  Each logical dataset
is described by a :class:`DatasetSpec`; :func:`suggest_mapping` proposes a
source column for every logical field, the user corrects it in the UI, and
:func:`import_dataframe` validates and stores the data with the original file
preserved for audit.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .database import Database, now_iso, sha256
from .models import (TimekeeperCategory, clean_str, norm_name, parse_bool, parse_date,
                     to_decimal)


@dataclass(frozen=True)
class FieldSpec:
    """A logical field the application understands."""

    key: str
    label: str
    kind: str = "text"  # text | num | date | bool
    required: bool = False
    synonyms: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class DatasetSpec:
    """A logical import dataset (collections, time entries, ...)."""

    key: str
    label: str
    description: str
    fields: tuple[FieldSpec, ...]

    def field(self, key: str) -> FieldSpec:
        """Return the spec of one field."""
        return next(f for f in self.fields if f.key == key)


F = FieldSpec
DATASETS: dict[str, DatasetSpec] = {
    "collections": DatasetSpec(
        "collections", "Payment & collection allocations",
        "Preferred source: TimeSolv 'Invoice Summary with Payment Allocations' with payment "
        "allocations enabled. Only the portion allocated to professional fees earns credit.",
        (
            F("collection_date", "Collection date", "date", True,
              ("payment date", "date paid", "collection date", "received date", "receipt date",
               "deposit date", "pmt date")),
            F("client", "Client name", synonyms=("client", "client name", "customer")),
            F("matter_id", "Matter / Project ID", required=True,
              synonyms=("project id", "matter id", "project", "matter", "project number",
                        "matter number", "project code", "matter no")),
            F("matter_name", "Matter name", synonyms=("project name", "matter name",
                                                       "matter description", "project description")),
            F("invoice_id", "Invoice ID", synonyms=("invoice #", "invoice number", "invoice id",
                                                     "invoice no", "invoice", "inv #", "bill number")),
            F("payment_id", "Payment / receipt ID", synonyms=("payment id", "receipt id",
                                                              "payment #", "receipt #", "payment number",
                                                              "transaction id", "reference", "check #")),
            F("amount_collected", "Amount collected", "num",
              synonyms=("payment amount", "amount paid", "amount collected", "total payment",
                        "receipt amount", "amount received", "payment total")),
            F("fee_amount", "Amount allocated to professional fees", "num", True,
              ("applied to fees", "fees paid", "fee allocation", "fees applied", "fees collected",
               "professional fees", "fee payment", "payment applied to fees", "fees")),
            F("expense_amount", "Amount allocated to expenses", "num",
              synonyms=("applied to expenses", "expenses paid", "expense allocation",
                        "expenses applied", "costs paid", "expenses")),
            F("tax_amount", "Amount allocated to taxes", "num",
              synonyms=("applied to tax", "tax paid", "taxes", "sales tax", "tax")),
            F("payment_type", "Payment / transaction type",
              synonyms=("payment type", "transaction type", "payment method", "type", "source")),
            F("payment_status", "Payment status",
              synonyms=("payment status", "status", "cleared", "deposit status")),
            F("originator_hint", "Originating professional (if exported)",
              synonyms=("originating timekeeper", "originating attorney", "originator",
                        "origination", "originating professional")),
            F("responsible_professional", "Responsible Professional",
              synonyms=("responsible professional", "responsible attorney", "billing attorney",
                        "responsible timekeeper")),
        ),
    ),
    "time_entries": DatasetSpec(
        "time_entries", "Time entries",
        "TimeSolv time-entry / timeslip export including invoice number, billed hours and billed "
        "amount where available.",
        (
            F("entry_id", "Time-entry ID", synonyms=("entry id", "time entry id", "timeslip id",
                                                     "slip id", "id", "time id", "transaction id")),
            F("work_date", "Work date", "date", True, ("date", "work date", "entry date",
                                                       "service date", "date worked")),
            F("client", "Client", synonyms=("client", "client name")),
            F("matter_id", "Matter / Project ID", required=True,
              synonyms=("project id", "matter id", "project", "matter", "project number",
                        "matter number", "project code")),
            F("invoice_id", "Invoice ID", synonyms=("invoice number", "invoice #", "invoice id",
                                                    "invoice no", "invoice")),
            F("timekeeper", "Timekeeper", required=True,
              synonyms=("timekeeper", "professional", "timekeeper name", "user", "attorney",
                        "staff", "employee", "professional name")),
            F("timekeeper_id", "Timekeeper ID", synonyms=("timekeeper id", "professional id",
                                                          "user id", "employee id")),
            F("timekeeper_role", "Timekeeper role", synonyms=("professional type", "role",
                                                              "timekeeper role", "title", "level",
                                                              "timekeeper type")),
            F("hours", "Hours (recorded)", "num", True,
              ("hours", "time", "duration", "hours worked", "recorded hours", "actual hours")),
            F("billable", "Billable status", "bool", synonyms=("billable", "is billable",
                                                               "billable status", "billable?")),
            F("billed_status", "Billed status", synonyms=("billed status", "status", "billed",
                                                          "invoice status", "billing status")),
            F("hours_billed", "Hours billed", "num", synonyms=("billed hours", "hours billed",
                                                               "invoiced hours", "bill hours")),
            F("rate", "Billing rate", "num", synonyms=("rate", "billing rate", "hourly rate")),
            F("billed_value", "Billed value", "num", synonyms=("billed amount", "billed value",
                                                               "amount", "invoiced amount",
                                                               "bill amount", "total")),
            F("task_code", "Task code", synonyms=("task code", "activity code", "utbms",
                                                  "task", "activity")),
            F("description", "Description", synonyms=("description", "narrative", "notes",
                                                      "work description")),
        ),
    ),
    "matters": DatasetSpec(
        "matters", "Matters / projects",
        "TimeSolv project list. The Responsible Professional is NOT assumed to be the originator; "
        "map the originating partner separately or set originators in Firm Setup.",
        (
            F("matter_id", "Matter / Project ID", required=True,
              synonyms=("project id", "matter id", "project", "matter", "project number",
                        "matter number", "project code")),
            F("client", "Client", synonyms=("client", "client name")),
            F("matter_name", "Matter name", synonyms=("project name", "matter name",
                                                      "description", "project description")),
            F("originating_partner", "Originating partner",
              synonyms=("originating attorney", "originating partner", "originator",
                        "origination", "originating timekeeper", "originating professional")),
            F("responsible_professional", "Responsible Professional",
              synonyms=("responsible professional", "responsible attorney", "billing attorney",
                        "responsible timekeeper")),
            F("comp_supervising_partner", "Compensation supervising partner",
              synonyms=("supervising partner", "compensation supervisor", "supervising attorney")),
            F("status", "Status", synonyms=("status", "project status", "matter status")),
            F("open_date", "Open date", "date", synonyms=("open date", "opened", "date opened",
                                                          "start date", "created")),
            F("close_date", "Close date", "date", synonyms=("close date", "closed",
                                                            "date closed", "end date")),
        ),
    ),
    "professionals": DatasetSpec(
        "professionals", "Professionals / timekeepers",
        "TimeSolv professionals list. Classify each person as Partner, Associate, Paralegal or "
        "Other staff.",
        (
            F("timekeeper_id", "Timekeeper ID", synonyms=("professional id", "timekeeper id",
                                                          "user id", "id", "employee id")),
            F("name", "Name", required=True, synonyms=("name", "professional", "timekeeper",
                                                       "full name", "professional name")),
            F("role", "Role / title", synonyms=("title", "role", "position", "level")),
            F("category", "Category (Partner/Associate/Paralegal/Other)",
              synonyms=("type", "category", "professional type", "classification")),
            F("default_supervisor", "Default supervising partner",
              synonyms=("supervisor", "default supervisor", "supervising partner", "reports to",
                        "manager")),
            F("active", "Active status", "bool", synonyms=("active", "status", "is active")),
        ),
    ),
    "expenses": DatasetSpec(
        "expenses", "Partner expenses",
        "Expense list from the accounting system (e.g. a general-ledger or bill-payment export). "
        "Each row needs a category that matches a category defined on the Partner Expenses page.",
        (
            F("expense_id", "Expense / transaction ID",
              synonyms=("transaction id", "expense id", "id", "entry no", "journal entry",
                        "num", "ref no", "transaction #")),
            F("expense_date", "Date", "date", True, ("date", "transaction date", "bill date",
                                                     "payment date", "expense date")),
            F("category", "Expense category", required=True,
              synonyms=("category", "account", "expense category", "class", "gl account")),
            F("description", "Description", synonyms=("description", "memo", "memo/description",
                                                      "details")),
            F("amount", "Amount", "num", True, ("amount", "total", "debit", "expense amount")),
            F("vendor", "Vendor / payee", synonyms=("vendor", "payee", "name", "supplier")),
            F("reference", "Reference", synonyms=("reference", "check #", "doc number")),
        ),
    ),
    "partners": DatasetSpec(
        "partners", "Partner roster",
        "Partner names, Managing Partner flag (informational) and active flag.",
        (
            F("name", "Partner name", required=True, synonyms=("partner name", "name", "partner")),
            F("is_managing_partner", "Managing Partner (Yes/No)", "bool",
              synonyms=("managing partner", "is managing partner", "mp")),
            F("active", "Active (Yes/No)", "bool", synonyms=("active", "status", "is active")),
            F("start_date", "Partner from (date)", "date",
              synonyms=("partner since", "start date", "admitted", "partner from")),
            F("end_date", "Partner until (date)", "date",
              synonyms=("partner until", "end date", "departed", "withdrawal date")),
        ),
    ),
}


# ----------------------------------------------------------------------------
# Reading files
# ----------------------------------------------------------------------------

def read_upload(filename: str, data: bytes, header_row: int = 0,
                sheet: str | int | None = 0) -> pd.DataFrame:
    """Read a CSV or XLSX upload into a DataFrame of raw values.

    ``header_row`` is the zero-based row containing the column headings (TimeSolv
    reports sometimes include title rows above the headings).
    """
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        try:
            df = pd.read_excel(io.BytesIO(data), sheet_name=sheet if sheet is not None else 0,
                               header=header_row, dtype=object)
        except Exception as exc:  # noqa: BLE001 - surface any parser failure clearly
            raise ValueError(f"Could not read Excel file '{filename}': {exc}") from exc
    elif name.endswith((".csv", ".txt")):
        df = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False,
                                 header=header_row, encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"Could not read CSV file '{filename}': {exc}") from exc
        if df is None:
            raise ValueError(f"Could not decode '{filename}'. Save it as UTF-8 CSV and retry.")
    else:
        raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")
    df.columns = [clean_str(c) or f"Unnamed column {i + 1}" for i, c in enumerate(df.columns)]
    df = df.dropna(how="all")
    df = df[~df.apply(lambda r: all(clean_str(v) == "" for v in r), axis=1)]
    if df.empty:
        raise ValueError(f"'{filename}' contains no data rows.")
    return df.reset_index(drop=True)


def excel_sheets(data: bytes) -> list[str]:
    """Sheet names of an XLSX workbook."""
    return list(pd.ExcelFile(io.BytesIO(data)).sheet_names)


# ----------------------------------------------------------------------------
# Mapping
# ----------------------------------------------------------------------------

def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9#%]+", " ", str(text).lower()).strip()


def header_signature(columns: list[str]) -> str:
    """Stable signature of a header row, used to recall saved mappings."""
    joined = "|".join(sorted(_norm_header(c) for c in columns))
    return hashlib.sha1(joined.encode()).hexdigest()


def _score(column: str, spec: FieldSpec) -> float:
    col = _norm_header(column)
    candidates = [_norm_header(s) for s in spec.synonyms] + [_norm_header(spec.label)]
    if col in candidates:
        return 1.0 - 0.01 * candidates.index(col) / max(len(candidates), 1)
    best = 0.0
    for cand in candidates:
        if len(cand) > 3 and (cand in col or col in cand) and len(col) > 2:
            best = max(best, 0.75 + 0.1 * min(len(col), len(cand)) / max(len(col), len(cand)))
        best = max(best, difflib.SequenceMatcher(None, col, cand).ratio() * 0.85)
    return best


def suggest_mapping(dataset: str, columns: list[str], threshold: float = 0.62) -> dict[str, str]:
    """Propose ``{logical_field: source_column}`` (empty string = not mapped).

    Every (field, column) pair is scored by synonym match and fuzzy similarity;
    pairs are assigned greedily from the highest score so one column never maps
    to two fields.
    """
    spec = DATASETS[dataset]
    scored = sorted(
        ((_score(col, f), f.key, col) for f in spec.fields for col in columns),
        key=lambda t: -t[0],
    )
    mapping = {f.key: "" for f in spec.fields}
    used: set[str] = set()
    for score, key, col in scored:
        if score < threshold or mapping[key] or col in used:
            continue
        mapping[key] = col
        used.add(col)
    return mapping


def recall_or_suggest(db: Database, dataset: str, columns: list[str]) -> tuple[dict[str, str], str]:
    """Return a saved mapping for identical headers, otherwise an automatic suggestion."""
    saved = db.load_mapping(dataset, header_signature(columns))
    if saved:
        valid = {k: (v if v in columns else "") for k, v in saved.items()}
        for f in DATASETS[dataset].fields:
            valid.setdefault(f.key, "")
        return valid, "saved"
    return suggest_mapping(dataset, columns), "suggested"


def mapping_problems(dataset: str, mapping: dict[str, str]) -> list[str]:
    """Problems that prevent using a mapping (missing required fields, reuse)."""
    spec = DATASETS[dataset]
    problems = [f"Required field '{f.label}' is not mapped." for f in spec.fields
                if f.required and not mapping.get(f.key)]
    used: dict[str, str] = {}
    for key, col in mapping.items():
        if not col:
            continue
        if col in used:
            problems.append(
                f"Column '{col}' is mapped to both '{spec.field(used[col]).label}' and "
                f"'{spec.field(key).label}'."
            )
        used[col] = key
    return problems


# ----------------------------------------------------------------------------
# Normalization & validation
# ----------------------------------------------------------------------------

def infer_category(role: str, category: str = "") -> str:
    """Infer a timekeeper category from TimeSolv type/title text."""
    text = f"{category} {role}".lower()
    if "partner" in text or "shareholder" in text or "member" in text:
        return TimekeeperCategory.PARTNER
    if "paralegal" in text or "legal assistant" in text:
        return TimekeeperCategory.PARALEGAL
    if "associate" in text or "counsel" in text or "attorney" in text or "lawyer" in text:
        return TimekeeperCategory.ASSOCIATE
    return TimekeeperCategory.OTHER


@dataclass
class NormalizedData:
    """Result of normalizing a raw upload."""

    frame: pd.DataFrame
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def invalid_rows(self) -> set[int]:
        """Zero-based indexes of rows with at least one error."""
        return {e["_index"] for e in self.errors}


def normalize(dataset: str, raw: pd.DataFrame, mapping: dict[str, str]) -> NormalizedData:
    """Convert raw rows to logical fields with type validation.

    Errors are reported per row with the spreadsheet row number (header = row 1)
    so users can find and fix them in the source file.
    """
    spec = DATASETS[dataset]
    problems = mapping_problems(dataset, mapping)
    if problems:
        raise ValueError(" ".join(problems))
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    mapped_cols = {c for c in mapping.values() if c}
    for idx, row in raw.iterrows():
        rec: dict[str, Any] = {}
        for f in spec.fields:
            col = mapping.get(f.key) or ""
            value = row[col] if col else None
            try:
                if f.kind == "num":
                    rec[f.key] = to_decimal(value, default=None)
                elif f.kind == "date":
                    rec[f.key] = parse_date(value)
                elif f.kind == "bool":
                    parsed = parse_bool(value, default=None)
                    if col and clean_str(value) and parsed is None:
                        raise ValueError(f"'{value}' is not a recognizable yes/no value")
                    rec[f.key] = parsed
                else:
                    rec[f.key] = clean_str(value)
            except ValueError as exc:
                errors.append({"_index": idx, "Row": int(idx) + 2, "Field": f.label,
                               "Value": clean_str(value), "Problem": str(exc)})
                rec[f.key] = None
            if f.required and rec.get(f.key) in (None, ""):
                if not any(e["_index"] == idx and e["Field"] == f.label for e in errors):
                    errors.append({"_index": idx, "Row": int(idx) + 2, "Field": f.label,
                                   "Value": clean_str(value), "Problem": "Required value is blank"})
        raw_dict = {str(k): clean_str(v) for k, v in row.items()}
        rec["raw_json"] = json.dumps(raw_dict)
        if dataset == "matters":
            rec["custom_fields"] = json.dumps(
                {k: v for k, v in raw_dict.items() if k not in mapped_cols and v}
            )
        records.append(rec)
    frame = pd.DataFrame(records, index=raw.index)
    result = NormalizedData(frame=frame, errors=errors)
    _dataset_checks(dataset, result)
    return result


def _dataset_checks(dataset: str, data: NormalizedData) -> None:
    """Dataset-specific consistency checks (adds errors / warnings)."""
    df = data.frame
    if dataset == "collections":
        for idx, rec in df.iterrows():
            fee = to_decimal(rec.get("fee_amount"), default=None)
            exp = to_decimal(rec.get("expense_amount"))
            tax = to_decimal(rec.get("tax_amount"))
            amt = to_decimal(rec.get("amount_collected"), default=None)
            if fee is not None and amt is not None and fee + exp + tax > amt + to_decimal("0.01") \
                    and amt >= 0:
                data.errors.append({
                    "_index": idx, "Row": int(idx) + 2, "Field": "Amount allocated to professional fees",
                    "Value": str(fee),
                    "Problem": f"Fee + expense + tax allocations ({fee + exp + tax}) exceed the "
                               f"amount collected ({amt}).",
                })
        if "payment_id" in df and (df["payment_id"].fillna("") == "").all():
            data.warnings.append(
                "No payment/receipt ID is mapped; duplicates will be detected using date, matter, "
                "invoice and amounts instead."
            )
        if "invoice_id" in df and (df["invoice_id"].fillna("") == "").all():
            data.warnings.append(
                "No invoice IDs found; every collection will use the matter-level fallback for "
                "working credit."
            )
    if dataset == "time_entries":
        for idx, rec in df.iterrows():
            hours = to_decimal(rec.get("hours"), default=None)
            if hours is not None and hours < 0:
                data.errors.append({"_index": idx, "Row": int(idx) + 2, "Field": "Hours (recorded)",
                                    "Value": str(hours), "Problem": "Negative hours"})
        if "entry_id" in df and (df["entry_id"].fillna("") == "").all():
            data.warnings.append(
                "No time-entry ID is mapped; duplicates will be detected using date, matter, "
                "timekeeper, hours and description, and entry-level overrides will use that key."
            )
        if "hours_billed" in df and df["hours_billed"].isna().all():
            data.warnings.append(
                "Hours billed is not mapped. Under the 'Billed hours' method, billed entries will "
                "use recorded hours and be flagged in the exceptions report."
            )
    if dataset == "partners":
        if "is_managing_partner" in df:
            mps = int(df["is_managing_partner"].fillna(False).astype(bool).sum())
            if mps > 1:
                data.warnings.append(f"The roster identifies {mps} Managing Partners; at most one "
                                     "is expected.")


def collection_key(rec: dict[str, Any]) -> str:
    """Deduplication key for a collection row."""
    def s(v: Any) -> str:
        return norm_name(v)

    fee = to_decimal(rec.get("fee_amount"), default=None)
    exp = to_decimal(rec.get("expense_amount"), default=None)
    if s(rec.get("payment_id")):
        parts = ["P", s(rec.get("payment_id")), s(rec.get("invoice_id")), s(rec.get("matter_id")),
                 str(fee), str(exp)]
    else:
        parts = ["D", str(rec.get("collection_date")), s(rec.get("matter_id")),
                 s(rec.get("invoice_id")), str(to_decimal(rec.get("amount_collected"), None)),
                 str(fee), str(exp)]
    return "|".join(parts)


def time_entry_key(rec: dict[str, Any]) -> str:
    """Deduplication key for a time entry."""
    if norm_name(rec.get("entry_id")):
        return "E|" + norm_name(rec.get("entry_id"))
    desc = hashlib.sha1(norm_name(rec.get("description")).encode()).hexdigest()[:10]
    return "|".join(["T", str(rec.get("work_date")), norm_name(rec.get("matter_id")),
                     norm_name(rec.get("timekeeper")), str(rec.get("hours")), desc])


def effective_entry_id(rec: dict[str, Any]) -> str:
    """The identifier used for entry-level overrides/exclusions."""
    return clean_str(rec.get("entry_id")) or rec.get("dedup_key", "")


def find_duplicate_collections(df: pd.DataFrame) -> pd.DataFrame:
    """Return collection rows whose dedup key appears more than once."""
    if df.empty:
        return df
    keys = df.apply(lambda r: collection_key(r.to_dict()), axis=1)
    return df[keys.duplicated(keep=False)].assign(dedup_key=keys[keys.duplicated(keep=False)])


# ----------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------

@dataclass
class ImportResult:
    """Summary of one import."""

    dataset: str
    batch_id: int | None
    rows_read: int
    imported: int = 0
    duplicates: int = 0
    invalid: int = 0
    flagged_duplicates: int = 0
    updated: int = 0
    messages: list[str] = field(default_factory=list)


def import_dataframe(db: Database, year: int, dataset: str, raw: pd.DataFrame,
                     mapping: dict[str, str], filename: str, file_bytes: bytes, user: str,
                     skip_invalid: bool = False, save_mapping: bool = True,
                     replace_roster: bool = False) -> ImportResult:
    """Validate and store an upload. Returns counts; raises ``ValueError`` on blocking errors.

    * Rows with validation errors block the import unless ``skip_invalid``.
    * Rows whose dedup key already exists for the year are skipped (counted as
      duplicates) so re-importing the same file never double-counts.
    * Collection rows that repeat *within* the file are imported but flagged
      ``excluded`` as possible duplicates for review.
    """
    db.assert_writable(year)
    data = normalize(dataset, raw, mapping)
    if data.errors and not skip_invalid:
        raise ValueError(
            f"{len(data.invalid_rows)} row(s) failed validation. Fix the source file or choose "
            "'Skip invalid rows'."
        )
    valid = data.frame.drop(index=list(data.invalid_rows))
    result = ImportResult(dataset=dataset, batch_id=None, rows_read=len(raw),
                          invalid=len(data.invalid_rows), messages=list(data.warnings))
    if save_mapping:
        db.save_mapping(dataset, header_signature(list(raw.columns)), mapping, user,
                        name=filename)
    if db.file_hash_imported(year, dataset, sha256(file_bytes)):
        result.messages.append("An identical file was imported before; existing rows are skipped.")
    batch_id = db.create_batch(year, dataset, filename, file_bytes, mapping, user)
    result.batch_id = batch_id

    if dataset in ("collections", "time_entries"):
        _import_data_rows(db, year, dataset, valid, batch_id, result)
    elif dataset == "matters":
        _import_matters(db, year, valid, user, result)
    elif dataset == "professionals":
        _import_professionals(db, year, valid, user, result)
    elif dataset == "expenses":
        _import_expenses(db, year, valid, user, result)
    elif dataset == "partners":
        _import_partners(db, year, valid, user, result, replace_roster)
    db.finish_batch(batch_id, result.rows_read, result.imported + result.updated,
                    result.duplicates, result.invalid)
    db.log(year, user, f"Imported {DATASETS[dataset].label}", {
        "file": filename, "file_sha256": sha256(file_bytes), "batch_id": batch_id,
        "rows_read": result.rows_read, "imported": result.imported, "updated": result.updated,
        "duplicates_skipped": result.duplicates, "flagged_duplicates": result.flagged_duplicates,
        "invalid_skipped": result.invalid,
    })
    return result


def _import_data_rows(db: Database, year: int, dataset: str, valid: pd.DataFrame,
                      batch_id: int, result: ImportResult) -> None:
    existing = db.existing_keys(dataset, year)
    keyfn = collection_key if dataset == "collections" else time_entry_key
    seen: dict[str, int] = {}
    keep_rows = []
    for _, row in valid.iterrows():
        rec = row.to_dict()
        key = keyfn(rec)
        rec["dedup_key"] = key
        if key in existing:
            result.duplicates += 1
            continue
        if key in seen:
            if dataset == "collections":
                n = seen[key] + 1
                seen[key] = n
                rec["dedup_key"] = f"{key}#dup{n}"
                rec["excluded"] = True
                rec["duplicate_of"] = key
                rec["exclusion_reason"] = ("Possible duplicate payment: same payment ID, invoice, "
                                           "matter and allocation as another row in this file.")
                result.flagged_duplicates += 1
            else:
                result.duplicates += 1
                continue
        else:
            seen[key] = 0
            if dataset == "collections":
                rec["excluded"] = False
        keep_rows.append(rec)
    if keep_rows:
        result.imported = db.insert_data(dataset, year, batch_id, pd.DataFrame(keep_rows))
    if result.duplicates:
        result.messages.append(f"{result.duplicates} row(s) already imported were skipped.")
    if result.flagged_duplicates:
        result.messages.append(
            f"{result.flagged_duplicates} possible duplicate payment(s) were imported as EXCLUDED "
            "and listed on the Audit & Exceptions page for review."
        )


def _split_names(text: str) -> list[str]:
    return [n.strip() for n in re.split(r"[;/|]", text or "") if n.strip()]


def _import_matters(db: Database, year: int, valid: pd.DataFrame, user: str,
                    result: ImportResult) -> None:
    matters = db.load_table("matters", year)
    originators = db.load_table("originators", year)
    index = {norm_name(m): i for i, m in matters["matter_id"].items()}
    new_rows = []
    orig_rows = originators.to_dict("records")
    orig_matters = {norm_name(r["matter_id"]) for r in orig_rows}
    for _, rec in valid.iterrows():
        mid = clean_str(rec["matter_id"])
        values = {
            "matter_id": mid, "client": rec.get("client", ""), "matter_name": rec.get("matter_name", ""),
            "responsible_professional": rec.get("responsible_professional", ""),
            "comp_supervising_partner": rec.get("comp_supervising_partner", ""),
            "status": rec.get("status", ""), "open_date": rec.get("open_date"),
            "close_date": rec.get("close_date"), "custom_fields": rec.get("custom_fields", ""),
        }
        key = norm_name(mid)
        if key in index:
            i = index[key]
            for col, val in values.items():
                if val not in (None, "") and col != "comp_supervising_partner":
                    matters.at[i, col] = val
            if values["comp_supervising_partner"] and not clean_str(
                    matters.at[i, "comp_supervising_partner"]):
                matters.at[i, "comp_supervising_partner"] = values["comp_supervising_partner"]
            result.updated += 1
        else:
            new_rows.append(values)
            index[key] = -1
            result.imported += 1
        names = _split_names(rec.get("originating_partner", ""))
        if names:
            if key in orig_matters:
                result.messages.append(
                    f"Matter {mid}: originator(s) already set in Firm Setup were kept; imported "
                    f"value '{rec.get('originating_partner')}' ignored."
                )
            else:
                share = to_decimal(100) / len(names)
                for n in names:
                    orig_rows.append({
                        "matter_id": mid, "partner": n, "share_pct": share,
                        "notes": "Imported from TimeSolv" + (
                            " - EQUAL SPLIT ASSUMED, confirm percentages" if len(names) > 1 else ""),
                    })
                orig_matters.add(key)
    if new_rows:
        matters = pd.concat([matters, pd.DataFrame(new_rows)], ignore_index=True)
    db.save_table("matters", year, matters, user, "Matters imported/updated")
    db.save_table("originators", year, pd.DataFrame(orig_rows, columns=list(originators.columns)),
                  user, "Originators imported from matters file")


def _import_professionals(db: Database, year: int, valid: pd.DataFrame, user: str,
                          result: ImportResult) -> None:
    tks = db.load_table("timekeepers", year)
    by_id = {norm_name(v): i for i, v in tks["timekeeper_id"].items() if norm_name(v)}
    by_name = {norm_name(v): i for i, v in tks["name"].items()}
    new_rows = []
    for _, rec in valid.iterrows():
        category = infer_category(rec.get("role", ""), rec.get("category", ""))
        values = {
            "timekeeper_id": rec.get("timekeeper_id", ""), "name": rec["name"],
            "role": rec.get("role") or rec.get("category", ""), "category": category,
            "linked_partner": rec["name"] if category == TimekeeperCategory.PARTNER else "",
            "default_supervisor": rec.get("default_supervisor", ""),
            "active": True if rec.get("active") is None else bool(rec.get("active")),
        }
        i = by_id.get(norm_name(values["timekeeper_id"])) if values["timekeeper_id"] else None
        if i is None:
            i = by_name.get(norm_name(values["name"]))
        if i is not None:
            for col, val in values.items():
                if val not in (None, ""):
                    tks.at[i, col] = val
            result.updated += 1
        else:
            new_rows.append(values)
            result.imported += 1
    if new_rows:
        tks = pd.concat([tks, pd.DataFrame(new_rows)], ignore_index=True)
    db.save_table("timekeepers", year, tks, user, "Timekeepers imported/updated")
    result.messages.append("Categories were inferred from type/title text - review them in Firm "
                           "Setup > Timekeepers.")


def _import_expenses(db: Database, year: int, valid: pd.DataFrame, user: str,
                     result: ImportResult) -> None:
    current = db.load_table("expenses", year)

    def key(r: dict[str, Any]) -> str:
        if clean_str(r.get("expense_id")):
            return "I|" + norm_name(r.get("expense_id"))
        return "|".join(["K", str(parse_date(r.get("expense_date")) if r.get("expense_date") else ""),
                         norm_name(r.get("category")), str(to_decimal(r.get("amount"), None)),
                         norm_name(r.get("description"))])

    seen = {key(r) for r in current.to_dict("records")}
    rows = []
    next_n = len(current) + 1
    for _, rec in valid.iterrows():
        r = {c: rec.get(c) for c in ("expense_id", "expense_date", "category", "description",
                                     "amount", "vendor", "reference")}
        k = key(r)
        if k in seen:
            result.duplicates += 1
            continue
        seen.add(k)
        if not clean_str(r["expense_id"]):
            r["expense_id"] = f"EXP-{year}-{next_n:05d}"
            next_n += 1
        rows.append(r)
    if rows:
        db.save_table("expenses", year, pd.concat([current, pd.DataFrame(rows)], ignore_index=True),
                      user, "Expenses imported")
    result.imported = len(rows)
    if result.duplicates:
        result.messages.append(f"{result.duplicates} expense(s) already imported were skipped.")


def _import_partners(db: Database, year: int, valid: pd.DataFrame, user: str,
                     result: ImportResult, replace: bool) -> None:
    current = db.load_table("partners", year)
    if not current.empty and not replace:
        raise ValueError("A partner roster already exists for this year. Tick 'Replace the "
                         "existing roster' to overwrite it.")
    rows = [{
        "name": rec["name"], "is_managing_partner": bool(rec.get("is_managing_partner") or False),
        "active": True if rec.get("active") is None else bool(rec.get("active")), "notes": "",
        "start_date": rec.get("start_date"), "end_date": rec.get("end_date"),
    } for _, rec in valid.iterrows()]
    db.save_table("partners", year, pd.DataFrame(rows), user, "Partner roster imported")
    result.imported = len(rows)


def import_file(db: Database, year: int, dataset: str, path: str, user: str,
                mapping: dict[str, str] | None = None, **kwargs: Any) -> ImportResult:
    """Convenience wrapper used by the demo loader and tests: import a file from disk."""
    with open(path, "rb") as fh:
        data = fh.read()
    raw = read_upload(path, data)
    if mapping is None:
        mapping, _ = recall_or_suggest(db, dataset, list(raw.columns))
    return import_dataframe(db, year, dataset, raw, mapping, path.split("/")[-1], data, user,
                            **kwargs)


__all__ = [
    "DATASETS", "DatasetSpec", "FieldSpec", "ImportResult", "NormalizedData", "collection_key",
    "effective_entry_id", "excel_sheets", "find_duplicate_collections", "header_signature",
    "import_dataframe", "import_file", "infer_category", "mapping_problems", "normalize",
    "read_upload", "recall_or_suggest", "suggest_mapping", "time_entry_key", "now_iso",
]
