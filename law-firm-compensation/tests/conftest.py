"""Shared fixtures and small-scenario builders for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compensation.calculations import CompensationInputs  # noqa: E402
from compensation.database import Database  # noqa: E402
from compensation.models import Policy  # noqa: E402

PARTNERS = ["MP", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]


def roster(inactive: tuple[str, ...] = ()) -> pd.DataFrame:
    """Standard 11-partner roster: MP 18%, others 8.2%."""
    return pd.DataFrame([{"name": n, "is_managing_partner": n == "MP",
                          "lockstep_weight": 18.0 if n == "MP" else 8.2,
                          "active": n not in inactive, "notes": ""} for n in PARTNERS])


def timekeepers(extra: list[dict[str, Any]] | None = None) -> pd.DataFrame:
    """Partners as timekeepers plus associates Smith/Jones and paralegal Liu."""
    rows = [{"timekeeper_id": n, "name": n, "role": "Partner", "category": "Partner",
             "linked_partner": n, "default_supervisor": "", "active": True} for n in PARTNERS]
    rows += [
        {"timekeeper_id": "S", "name": "Smith", "role": "Associate", "category": "Associate",
         "linked_partner": "", "default_supervisor": "A", "active": True},
        {"timekeeper_id": "J", "name": "Jones", "role": "Associate", "category": "Associate",
         "linked_partner": "", "default_supervisor": "", "active": True},
        {"timekeeper_id": "L", "name": "Liu", "role": "Paralegal", "category": "Paralegal",
         "linked_partner": "", "default_supervisor": "B", "active": True},
    ]
    return pd.DataFrame(rows + (extra or []))


def matter(mid: str, responsible: str = "", comp: str = "") -> dict[str, Any]:
    """A matter row."""
    return {"matter_id": mid, "client": f"Client {mid}", "matter_name": f"Matter {mid}",
            "responsible_professional": responsible, "comp_supervising_partner": comp,
            "status": "Open", "open_date": None, "close_date": None, "custom_fields": ""}


def entry(eid: str, tk: str, mid: str, hours: float, invoice: str = "INV1",
          day: str = "2025-03-01", billed: bool = True) -> dict[str, Any]:
    """A billed time entry."""
    return {"entry_id": eid, "dedup_key": f"E|{eid}", "work_date": day, "client": "",
            "matter_id": mid, "invoice_id": invoice, "timekeeper": tk, "timekeeper_id": "",
            "timekeeper_role": "", "hours": hours, "billable": True,
            "billed_status": "Billed" if billed else "Unbilled",
            "hours_billed": hours if billed else 0, "rate": 300, "billed_value": hours * 300,
            "task_code": "", "description": ""}


def collection(pid: str, mid: str, fee: float, invoice: str = "INV1", expense: float = 0.0,
               day: str = "2025-05-01", **kw: Any) -> dict[str, Any]:
    """A collection row."""
    row = {"row_id": hash(pid) % 10000, "dedup_key": pid, "collection_date": day, "client": "",
           "matter_id": mid, "matter_name": "", "invoice_id": invoice, "payment_id": pid,
           "amount_collected": fee + expense, "fee_amount": fee, "expense_amount": expense,
           "tax_amount": 0.0, "payment_type": "Check", "payment_status": "Cleared",
           "originator_hint": "", "responsible_professional": "", "excluded": False,
           "exclusion_reason": "", "duplicate_of": ""}
    row.update(kw)
    return row


def make_inputs(*, entries: list[dict[str, Any]], collections: list[dict[str, Any]],
                matters: list[dict[str, Any]], originators: list[tuple[str, str, float]],
                supervision: list[dict[str, Any]] | None = None, policy: Policy | None = None,
                partners: pd.DataFrame | None = None, **kw: Any) -> CompensationInputs:
    """Build engine inputs for a scenario."""
    return CompensationInputs(
        policy=policy or Policy(year=2025, distributable_pool=1_000_000),
        partners=partners if partners is not None else roster(),
        timekeepers=timekeepers(),
        matters=pd.DataFrame(matters),
        originators=pd.DataFrame([{"matter_id": m, "partner": p, "share_pct": s, "notes": ""}
                                  for m, p, s in originators]),
        supervision=pd.DataFrame(supervision or [], columns=[
            "timekeeper", "matter_id", "partner", "start_date", "end_date", "allocation_pct",
            "notes", "source"]),
        collections=pd.DataFrame(collections),
        time_entries=pd.DataFrame(entries),
        **kw,
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """A fresh database in a temporary directory."""
    return Database(tmp_path / "test.db")
