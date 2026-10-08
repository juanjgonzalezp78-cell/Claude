"""Shared Streamlit helpers: database handle, sidebar context, cached results, formatting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

import pandas as pd
import streamlit as st

from compensation.calculations import CompensationInputs, CompensationResult
from compensation.database import Database, ReadOnlyYearError
from compensation.service import run

MONEY = st.column_config.NumberColumn(format="$%,.2f")
PCT = st.column_config.NumberColumn(format="%.3f%%")


@st.cache_resource
def get_db() -> Database:
    """Single Database instance per server process."""
    return Database()


def year() -> int:
    """Selected compensation year."""
    return int(st.session_state.get("year", date.today().year - 1))


def user() -> str:
    """Name entered in the sidebar (used for the audit log)."""
    return str(st.session_state.get("user_name", "")).strip()


def bump() -> None:
    """Invalidate cached calculation results after any data change."""
    st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1


def results(force: bool = False) -> tuple[CompensationInputs, CompensationResult]:
    """Run (or reuse) the calculation for the selected year."""
    key = (year(), st.session_state.get("data_version", 0))
    cached = st.session_state.get("_calc")
    if force or not cached or cached[0] != key:
        inputs, result = run(get_db(), year())
        st.session_state["_calc"] = (key, inputs, result)
        return inputs, result
    return cached[1], cached[2]


def _store_user() -> None:
    st.session_state["user_name"] = st.session_state.get("_user_input", "")


def sidebar() -> None:
    """Year selector, user name and year status."""
    db = get_db()
    with st.sidebar:
        st.markdown("### Compensation year")
        years = sorted(set(db.known_years()) | {year()})
        new = st.number_input("Year", min_value=2000, max_value=2100, step=1, value=year(),
                              key="year_input", label_visibility="collapsed")
        if int(new) != year():
            st.session_state["year"] = int(new)
            bump()
            st.rerun()
        if years:
            st.caption("Years with data: " + ", ".join(str(y) for y in years))
        # Widget state is copied to a plain session key so it survives page switches.
        st.session_state["_user_input"] = st.session_state.get("user_name", "")
        st.text_input("Your name (recorded in the audit log)", key="_user_input",
                      placeholder="e.g. J. Gonzalez", on_change=_store_user)
        status = db.year_status(year())
        if status.get("status") == "finalized":
            st.error(f"🔒 {year()} is FINALIZED ({status.get('finalized_at')} by "
                     f"{status.get('finalized_by')}). Read-only.")
        elif status.get("status") == "reopened":
            st.warning(f"{year()} was reopened by {status.get('reopened_by')}: "
                       f"{status.get('reopen_reason')}")
        else:
            st.info(f"{year()} is open (draft).")


def require_user() -> bool:
    """Show a warning and return False if no user name has been entered."""
    if not user():
        st.warning("Enter your name in the sidebar first - every change is recorded in the "
                   "audit log with the user's name.")
        return False
    return True


def writable() -> bool:
    """True if the selected year can be edited; otherwise shows a notice."""
    if get_db().is_finalized(year()):
        st.info("This year is finalized and read-only. Reopen it on the Finalize & History page "
                "(administrator password required) to make changes.")
        return False
    return True


def attempt(action: Callable[[], Any], success: str | None = None) -> Any:
    """Run a data-changing action with friendly error messages, then refresh results."""
    try:
        out = action()
    except ReadOnlyYearError as exc:
        st.error(str(exc))
        return None
    except (ValueError, PermissionError, KeyError) as exc:
        st.error(str(exc))
        return None
    bump()
    if success:
        st.success(success)
    return out


def show(df: pd.DataFrame) -> pd.DataFrame:
    """Convert Decimal columns to float for display."""
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object and out[c].map(lambda v: isinstance(v, Decimal)).any():
            out[c] = out[c].map(lambda v: float(v) if isinstance(v, Decimal) else v)
    return out


def money_config(df: pd.DataFrame) -> dict[str, Any]:
    """Column config: currency for money-like numeric columns, percent for ``%`` columns."""
    cfg: dict[str, Any] = {}
    money_words = ("credit", "compensation", "fees", "amount", "collected", "portion",
                   "expenses", "taxes", "unallocated", "credited", "pool", "rate", "value")
    for c in df.columns:
        lc = c.lower()
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        if "%" in c or lc.endswith("weight") or lc.endswith("share") or "weight %" in lc:
            cfg[c] = PCT
        elif any(w in lc for w in money_words) and "measure" not in lc and "hours" not in lc:
            cfg[c] = MONEY
    return cfg


def fmt_money(v: Any) -> str:
    """$1,234.56"""
    v = float(v or 0)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def partner_names(active_only: bool = False) -> list[str]:
    """Partner names for the selected year."""
    df = get_db().load_table("partners", year())
    if active_only:
        df = df[df["active"]]
    return [n for n in df["name"].tolist() if n]
