"""SQLite persistence: settings, setup tables, imports, audit log and snapshots.

Every write for a compensation year goes through :class:`Database`, which
refuses to modify a finalized year (:class:`ReadOnlyYearError`) and records an
audit-log entry for each change.  The database path defaults to
``data/compensation.db`` and can be overridden with ``COMP_DB_PATH``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .models import Policy

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "compensation.db"
ADMIN_PASSWORD_ENV = "COMP_ADMIN_PASSWORD"


class ReadOnlyYearError(RuntimeError):
    """Raised when attempting to modify a finalized compensation year."""


# Column definitions for year-scoped setup tables: column -> kind
# kinds: "text", "bool", "num" (Decimal stored as TEXT), "date" (ISO text)
SETUP_TABLES: dict[str, dict[str, str]] = {
    "partners": {
        "name": "text", "is_managing_partner": "bool", "lockstep_weight": "num",
        "active": "bool", "notes": "text",
        "start_date": "date", "end_date": "date",
    },
    "timekeepers": {
        "timekeeper_id": "text", "name": "text", "role": "text", "category": "text",
        "linked_partner": "text", "default_supervisor": "text", "active": "bool",
    },
    "matters": {
        "matter_id": "text", "client": "text", "matter_name": "text",
        "responsible_professional": "text", "comp_supervising_partner": "text",
        "status": "text", "open_date": "date", "close_date": "date", "custom_fields": "text",
    },
    "originators": {
        "matter_id": "text", "partner": "text", "share_pct": "num", "notes": "text",
    },
    "supervision": {
        "timekeeper": "text", "matter_id": "text", "partner": "text", "start_date": "date",
        "end_date": "date", "allocation_pct": "num", "notes": "text", "source": "text",
    },
    "entry_overrides": {
        "entry_id": "text", "partner": "text", "allocation_pct": "num", "reason": "text",
        "entered_by": "text", "entered_at": "text",
    },
    "exclusions": {
        "entry_id": "text", "reason": "text", "entered_by": "text", "entered_at": "text",
    },
    "expense_categories": {
        "category": "text", "rule": "text", "associate": "text", "notes": "text",
    },
    "category_splits": {
        "category": "text", "partner": "text", "share_pct": "num", "notes": "text",
    },
    "expenses": {
        "expense_id": "text", "expense_date": "date", "category": "text", "description": "text",
        "amount": "num", "vendor": "text", "reference": "text",
    },
    "expense_overrides": {
        "expense_id": "text", "partner": "text", "share_pct": "num", "reason": "text",
        "entered_by": "text", "entered_at": "text",
    },
    "carryforwards": {
        "partner": "text", "amount": "num", "source_year": "text", "notes": "text",
    },
    "manual_shares": {
        "matter_id": "text", "partner": "text", "share_pct": "num", "notes": "text",
    },
    "lockstep_overrides": {
        "partner": "text", "override_weight": "num", "reason": "text",
        "entered_by": "text", "entered_at": "text",
    },
}

COLLECTION_COLUMNS: dict[str, str] = {
    "dedup_key": "text", "collection_date": "date", "client": "text", "matter_id": "text",
    "matter_name": "text", "invoice_id": "text", "payment_id": "text",
    "amount_collected": "num", "fee_amount": "num", "expense_amount": "num",
    "tax_amount": "num", "payment_type": "text", "payment_status": "text",
    "originator_hint": "text", "responsible_professional": "text",
    "transaction_type": "text", "credit_type": "text", "payment_method": "text",
    "account_group": "text", "allocated_amount": "num", "available_funds": "num",
    "excluded": "bool", "exclusion_reason": "text", "duplicate_of": "text", "raw_json": "text",
}

TIME_ENTRY_COLUMNS: dict[str, str] = {
    "dedup_key": "text", "entry_id": "text", "work_date": "date", "client": "text",
    "matter_id": "text", "invoice_id": "text", "timekeeper": "text", "timekeeper_id": "text",
    "timekeeper_role": "text", "hours": "num", "billable": "bool", "billed_status": "text",
    "hours_billed": "num", "rate": "num", "billed_value": "num", "task_code": "text",
    "description": "text", "raw_json": "text",
}

INVOICE_COLUMNS: dict[str, str] = {
    "dedup_key": "text", "invoice_id": "text", "invoice_date": "date", "client": "text",
    "matter_id": "text", "matter_name": "text", "fees": "num", "expenses": "num", "taxes": "num",
    "taxes2": "num", "interest": "num", "total": "num", "status": "text", "raw_json": "text",
}

DATA_TABLES = {"collections": COLLECTION_COLUMNS, "time_entries": TIME_ENTRY_COLUMNS,
               "invoices": INVOICE_COLUMNS}


def _sql_type(kind: str) -> str:
    return "INTEGER" if kind == "bool" else "TEXT"


def _to_db(value: Any, kind: str) -> Any:
    """Convert a python/pandas value to its SQLite storage representation."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if kind == "bool":
        if isinstance(value, str):
            return 1 if value.strip().lower() in ("1", "true", "yes", "y") else 0
        return 1 if bool(value) else 0
    if kind == "date":
        if hasattr(value, "isoformat"):
            return value.isoformat()[:10]
        text = str(value).strip()
        return text[:10] or None
    if kind == "num":
        from .models import to_decimal

        dec = to_decimal(value, default=None)
        return None if dec is None else str(dec)
    text = str(value).strip()
    return text if text else None


def _from_db(df: pd.DataFrame, schema: dict[str, str]) -> pd.DataFrame:
    """Restore column types after a SQL read."""
    for col, kind in schema.items():
        if col not in df.columns:
            df[col] = None
        if kind == "bool":
            df[col] = df[col].fillna(0).astype(int).astype(bool)
        elif kind == "num":
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif kind == "text":
            df[col] = df[col].astype(object).where(df[col].notna(), "")
    return df


class Database:
    """Thin data-access layer over a single SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("COMP_DB_PATH") or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ------------------------------------------------------------------ basics
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create all tables if they do not exist."""
        with self.connect() as conn:
            for name, cols in SETUP_TABLES.items():
                col_sql = ", ".join(f"{c} {_sql_type(k)}" for c, k in cols.items())
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {name} "
                    f"(row_id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, {col_sql})"
                )
            for name, cols in DATA_TABLES.items():
                col_sql = ", ".join(f"{c} {_sql_type(k)}" for c, k in cols.items())
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {name} (row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    f"year INTEGER NOT NULL, batch_id INTEGER, {col_sql})"
                )
                conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{name}_key ON {name}(year, dedup_key)")
            # Lightweight migration: add columns introduced after a database was created.
            for name, cols in {**SETUP_TABLES, **DATA_TABLES}.items():
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()}
                for c, k in cols.items():
                    if c not in have:
                        conn.execute(f"ALTER TABLE {name} ADD COLUMN {c} {_sql_type(k)}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    year INTEGER PRIMARY KEY, policy_json TEXT NOT NULL, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS import_batches (
                    batch_id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, dataset TEXT,
                    filename TEXT, file_hash TEXT, file_blob BLOB, mapping_json TEXT,
                    rows_read INTEGER, rows_imported INTEGER, rows_duplicate INTEGER,
                    rows_invalid INTEGER, imported_by TEXT, imported_at TEXT);
                CREATE TABLE IF NOT EXISTS column_mappings (
                    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT, signature TEXT,
                    name TEXT, mapping_json TEXT, updated_by TEXT, updated_at TEXT,
                    UNIQUE(dataset, signature));
                CREATE TABLE IF NOT EXISTS audit_log (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, year INTEGER,
                    user TEXT, action TEXT, details TEXT);
                CREATE TABLE IF NOT EXISTS year_status (
                    year INTEGER PRIMARY KEY, status TEXT, finalized_at TEXT, finalized_by TEXT,
                    reopened_at TEXT, reopened_by TEXT, reopen_reason TEXT);
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, kind TEXT,
                    created_at TEXT, created_by TEXT, payload_json TEXT);
                """
            )

    # ------------------------------------------------------------------ audit
    def log(self, year: int | None, user: str, action: str, details: Any = "") -> None:
        """Append an audit-log entry (never blocked by finalization)."""
        if not isinstance(details, str):
            details = json.dumps(details, default=str)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, year, user, action, details) VALUES (?,?,?,?,?)",
                (now_iso(), year, user or "unknown", action, details),
            )

    def audit_log(self, year: int | None = None) -> pd.DataFrame:
        """Return audit entries (optionally for one year), newest first."""
        with self.connect() as conn:
            if year is None:
                return pd.read_sql("SELECT * FROM audit_log ORDER BY log_id DESC", conn)
            return pd.read_sql(
                "SELECT * FROM audit_log WHERE year = ? OR year IS NULL ORDER BY log_id DESC",
                conn, params=(year,),
            )

    # ------------------------------------------------------------------ status
    def is_finalized(self, year: int) -> bool:
        """True if ``year`` is finalized (read-only)."""
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM year_status WHERE year = ?", (year,)).fetchone()
        return bool(row and row["status"] == "finalized")

    def year_status(self, year: int) -> dict[str, Any]:
        """Return the status record for ``year`` (``open`` if never finalized)."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM year_status WHERE year = ?", (year,)).fetchone()
        return dict(row) if row else {"year": year, "status": "open"}

    def assert_writable(self, year: int) -> None:
        """Raise :class:`ReadOnlyYearError` if ``year`` is finalized."""
        if self.is_finalized(year):
            raise ReadOnlyYearError(
                f"Compensation year {year} is finalized and read-only. "
                "An administrator must reopen it before changes can be made."
            )

    def known_years(self) -> list[int]:
        """Years that have settings or data."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT year FROM settings UNION SELECT year FROM partners "
                "UNION SELECT year FROM year_status ORDER BY year"
            ).fetchall()
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------ policy
    def load_policy(self, year: int) -> Policy:
        """Load the policy for ``year`` (defaults if never saved)."""
        with self.connect() as conn:
            row = conn.execute("SELECT policy_json FROM settings WHERE year = ?", (year,)).fetchone()
        if row:
            policy = Policy.from_dict(json.loads(row["policy_json"]))
            policy.year = year
            return policy
        return Policy(year=year)

    def save_policy(self, policy: Policy, user: str) -> None:
        """Persist policy settings with an audit entry showing changed fields."""
        self.assert_writable(policy.year)
        before = self.load_policy(policy.year).to_dict()
        after = policy.to_dict()
        changes = {k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (year, policy_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(year) DO UPDATE SET policy_json=excluded.policy_json, "
                "updated_at=excluded.updated_at",
                (policy.year, json.dumps(after), now_iso()),
            )
        self.log(policy.year, user, "Policy settings saved", changes or "no changes")

    # ------------------------------------------------------------------ setup tables
    def load_table(self, name: str, year: int) -> pd.DataFrame:
        """Load a year-scoped setup table as a typed DataFrame."""
        schema = SETUP_TABLES[name]
        cols = ", ".join(schema)
        with self.connect() as conn:
            df = pd.read_sql(
                f"SELECT {cols} FROM {name} WHERE year = ? ORDER BY row_id", conn, params=(year,)
            )
        return _from_db(df, schema)

    def save_table(self, name: str, year: int, df: pd.DataFrame, user: str,
                   action: str | None = None) -> dict[str, int]:
        """Replace all rows of a setup table for ``year`` and audit the differences."""
        self.assert_writable(year)
        schema = SETUP_TABLES[name]
        before = self.load_table(name, year)
        rows = []
        for rec in (df.to_dict("records") if df is not None else []):
            values = [_to_db(rec.get(c), k) for c, k in schema.items()]
            if all(v is None or v == 0 and schema[c] == "bool" for v, c in zip(values, schema)):
                continue  # drop fully blank editor rows
            rows.append(values)
        with self.connect() as conn:
            conn.execute(f"DELETE FROM {name} WHERE year = ?", (year,))
            placeholders = ", ".join("?" for _ in range(len(schema) + 1))
            conn.executemany(
                f"INSERT INTO {name} (year, {', '.join(schema)}) VALUES ({placeholders})",
                [[year] + r for r in rows],
            )
        after = self.load_table(name, year)
        diff = _diff_frames(before, after)
        self.log(year, user, action or f"Updated {name}", diff)
        return {"rows": len(rows), **{k: len(v) for k, v in diff.items() if isinstance(v, list)}}

    # ------------------------------------------------------------------ data tables
    def load_data(self, name: str, year: int, include_raw: bool = False) -> pd.DataFrame:
        """Load imported collections or time entries for ``year``."""
        schema = dict(DATA_TABLES[name])
        if not include_raw:
            schema.pop("raw_json")
        cols = ", ".join(["row_id", "batch_id"] + list(schema))
        with self.connect() as conn:
            df = pd.read_sql(
                f"SELECT {cols} FROM {name} WHERE year = ? ORDER BY row_id", conn, params=(year,)
            )
        return _from_db(df, schema)

    def existing_keys(self, name: str, year: int) -> set[str]:
        """Dedup keys already stored for ``year``."""
        with self.connect() as conn:
            rows = conn.execute(f"SELECT dedup_key FROM {name} WHERE year = ?", (year,)).fetchall()
        return {r[0] for r in rows}

    def insert_data(self, name: str, year: int, batch_id: int, df: pd.DataFrame) -> int:
        """Append normalized rows to a data table."""
        self.assert_writable(year)
        schema = DATA_TABLES[name]
        rows = [[year, batch_id] + [_to_db(rec.get(c), k) for c, k in schema.items()]
                for rec in df.to_dict("records")]
        placeholders = ", ".join("?" for _ in range(len(schema) + 2))
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO {name} (year, batch_id, {', '.join(schema)}) VALUES ({placeholders})",
                rows,
            )
        return len(rows)

    def delete_keys(self, name: str, year: int, keys: list[str]) -> int:
        """Delete data rows whose dedup key is in ``keys`` (used to update invoices)."""
        self.assert_writable(year)
        with self.connect() as conn:
            n = 0
            for k in keys:
                n += conn.execute(f"DELETE FROM {name} WHERE year = ? AND dedup_key = ?",
                                  (year, k)).rowcount
        return n

    def update_collection_flags(self, year: int, flags: pd.DataFrame, user: str) -> int:
        """Update ``excluded`` / ``exclusion_reason`` for collection rows (by row_id)."""
        self.assert_writable(year)
        current = self.load_data("collections", year).set_index("row_id")
        changes = []
        with self.connect() as conn:
            for rec in flags.to_dict("records"):
                rid = int(rec["row_id"])
                if rid not in current.index:
                    continue
                old = current.loc[rid]
                new_ex = bool(rec.get("excluded"))
                new_reason = str(rec.get("exclusion_reason") or "")
                if new_ex != bool(old["excluded"]) or new_reason != str(old["exclusion_reason"] or ""):
                    conn.execute(
                        "UPDATE collections SET excluded = ?, exclusion_reason = ? "
                        "WHERE row_id = ? AND year = ?",
                        (1 if new_ex else 0, new_reason, rid, year),
                    )
                    changes.append({"row_id": rid, "payment_id": old["payment_id"],
                                    "excluded": new_ex, "reason": new_reason})
        if changes:
            self.log(year, user, "Collection inclusion flags changed", changes)
        return len(changes)

    def delete_batch(self, year: int, batch_id: int, user: str) -> None:
        """Remove an import batch and its rows (the batch record is kept as history)."""
        self.assert_writable(year)
        with self.connect() as conn:
            for name in DATA_TABLES:
                conn.execute(f"DELETE FROM {name} WHERE year = ? AND batch_id = ?", (year, batch_id))
            conn.execute(
                "UPDATE import_batches SET rows_imported = 0, filename = filename || ' (removed)' "
                "WHERE batch_id = ?", (batch_id,),
            )
        self.log(year, user, "Import batch removed", {"batch_id": batch_id})

    def clear_year_data(self, year: int, user: str) -> None:
        """Delete all imported data and setup rows for a year (demo reset)."""
        self.assert_writable(year)
        with self.connect() as conn:
            for name in list(SETUP_TABLES) + list(DATA_TABLES):
                conn.execute(f"DELETE FROM {name} WHERE year = ?", (year,))
            conn.execute("DELETE FROM import_batches WHERE year = ?", (year,))
            conn.execute("DELETE FROM settings WHERE year = ?", (year,))
        self.log(year, user, "All data for the year cleared")

    # ------------------------------------------------------------------ import batches
    def create_batch(self, year: int, dataset: str, filename: str, file_bytes: bytes,
                     mapping: dict[str, str], user: str) -> int:
        """Record an import batch, keeping the original file for audit."""
        self.assert_writable(year)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO import_batches (year, dataset, filename, file_hash, file_blob, "
                "mapping_json, rows_read, rows_imported, rows_duplicate, rows_invalid, "
                "imported_by, imported_at) VALUES (?,?,?,?,?,?,0,0,0,0,?,?)",
                (year, dataset, filename, sha256(file_bytes), file_bytes,
                 json.dumps(mapping), user, now_iso()),
            )
            return int(cur.lastrowid)

    def finish_batch(self, batch_id: int, rows_read: int, imported: int, duplicates: int,
                     invalid: int) -> None:
        """Store final row counts for a batch."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE import_batches SET rows_read=?, rows_imported=?, rows_duplicate=?, "
                "rows_invalid=? WHERE batch_id=?",
                (rows_read, imported, duplicates, invalid, batch_id),
            )

    def batches(self, year: int) -> pd.DataFrame:
        """List import batches for a year (without file contents)."""
        with self.connect() as conn:
            return pd.read_sql(
                "SELECT batch_id, dataset, filename, file_hash, rows_read, rows_imported, "
                "rows_duplicate, rows_invalid, imported_by, imported_at, mapping_json "
                "FROM import_batches WHERE year = ? ORDER BY batch_id", conn, params=(year,),
            )

    def batch_file(self, batch_id: int) -> tuple[str, bytes] | None:
        """Return (filename, bytes) of an original imported file."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT filename, file_blob FROM import_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return (row["filename"], bytes(row["file_blob"])) if row else None

    def file_hash_imported(self, year: int, dataset: str, file_hash: str) -> bool:
        """True if an identical file was already imported for this dataset/year."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM import_batches WHERE year=? AND dataset=? AND file_hash=? "
                "AND rows_imported > 0", (year, dataset, file_hash),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ column mappings
    def save_mapping(self, dataset: str, signature: str, mapping: dict[str, str], user: str,
                     name: str = "") -> None:
        """Save (upsert) a column mapping for a dataset and header signature."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO column_mappings (dataset, signature, name, mapping_json, updated_by, "
                "updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(dataset, signature) DO UPDATE SET "
                "mapping_json=excluded.mapping_json, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at, name=excluded.name",
                (dataset, signature, name, json.dumps(mapping), user, now_iso()),
            )
        self.log(None, user, f"Column mapping saved for {dataset}", mapping)

    def load_mapping(self, dataset: str, signature: str) -> dict[str, str] | None:
        """Exact-signature mapping lookup."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT mapping_json FROM column_mappings WHERE dataset=? AND signature=?",
                (dataset, signature),
            ).fetchone()
        return json.loads(row["mapping_json"]) if row else None

    def mappings(self, dataset: str | None = None) -> pd.DataFrame:
        """All saved mappings (most recent first)."""
        with self.connect() as conn:
            if dataset:
                return pd.read_sql(
                    "SELECT * FROM column_mappings WHERE dataset=? ORDER BY updated_at DESC",
                    conn, params=(dataset,),
                )
            return pd.read_sql("SELECT * FROM column_mappings ORDER BY updated_at DESC", conn)

    # ------------------------------------------------------------------ snapshots
    def save_snapshot(self, year: int, kind: str, payload: dict[str, Any], user: str) -> int:
        """Store a JSON snapshot (used for finalization)."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO snapshots (year, kind, created_at, created_by, payload_json) "
                "VALUES (?,?,?,?,?)",
                (year, kind, now_iso(), user, json.dumps(payload, default=str)),
            )
            return int(cur.lastrowid)

    def snapshots(self, year: int | None = None) -> pd.DataFrame:
        """List snapshots (without payloads)."""
        with self.connect() as conn:
            sql = "SELECT snapshot_id, year, kind, created_at, created_by FROM snapshots"
            if year is not None:
                return pd.read_sql(sql + " WHERE year=? ORDER BY snapshot_id DESC", conn,
                                   params=(year,))
            return pd.read_sql(sql + " ORDER BY snapshot_id DESC", conn)

    def load_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """Return a snapshot payload."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def latest_final_snapshot(self, year: int) -> dict[str, Any] | None:
        """Most recent finalization snapshot for ``year``."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM snapshots WHERE year=? AND kind='finalization' "
                "ORDER BY snapshot_id DESC LIMIT 1", (year,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def mark_finalized(self, year: int, user: str) -> None:
        """Set the year's status to finalized."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO year_status (year, status, finalized_at, finalized_by) "
                "VALUES (?, 'finalized', ?, ?) ON CONFLICT(year) DO UPDATE SET "
                "status='finalized', finalized_at=excluded.finalized_at, "
                "finalized_by=excluded.finalized_by",
                (year, now_iso(), user),
            )
        self.log(year, user, "Year finalized")

    def reopen_year(self, year: int, user: str, password: str, reason: str) -> None:
        """Reopen a finalized year. Requires the administrator password from the environment."""
        expected = os.environ.get(ADMIN_PASSWORD_ENV)
        if not expected:
            raise PermissionError(
                f"Reopening is disabled: set the {ADMIN_PASSWORD_ENV} environment variable "
                "before starting the application."
            )
        if not password or not _constant_time_eq(password, expected):
            self.log(year, user, "Reopen attempt rejected (bad password)")
            raise PermissionError("Incorrect administrator password.")
        if not reason.strip():
            raise ValueError("A written reason is required to reopen a finalized year.")
        with self.connect() as conn:
            conn.execute(
                "UPDATE year_status SET status='reopened', reopened_at=?, reopened_by=?, "
                "reopen_reason=? WHERE year=?", (now_iso(), user, reason, year),
            )
        self.log(year, user, "Year reopened", {"reason": reason})


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def _diff_frames(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, Any]:
    """Row-level diff (added/removed rows) between two versions of a table."""
    def rows(df: pd.DataFrame) -> list[str]:
        return [json.dumps(r, default=str, sort_keys=True) for r in df.astype(str).to_dict("records")]

    b, a = rows(before), rows(after)
    bs, as_ = set(b), set(a)
    added = [json.loads(r) for r in a if r not in bs]
    removed = [json.loads(r) for r in b if r not in as_]
    return {"added": added[:200], "removed": removed[:200],
            "rows_before": len(b), "rows_after": len(a)}


def sha256(data: bytes) -> str:
    """Hex SHA-256 of bytes."""
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    """Current local timestamp (seconds precision)."""
    return datetime.now().isoformat(timespec="seconds")
