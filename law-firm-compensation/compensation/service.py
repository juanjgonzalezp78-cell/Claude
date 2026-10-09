"""Application services bridging the database and the calculation engine.

Kept free of Streamlit so it can be used from tests, scripts and the UI.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from .calculations import CompensationInputs, CompensationResult, calculate
from .database import Database, ReadOnlyYearError, now_iso
from .validation import finalization_blockers


def load_inputs(db: Database, year: int) -> CompensationInputs:
    """Assemble engine inputs for ``year`` from SQLite."""
    return CompensationInputs(
        policy=db.load_policy(year),
        partners=db.load_table("partners", year),
        timekeepers=db.load_table("timekeepers", year),
        matters=db.load_table("matters", year),
        originators=db.load_table("originators", year),
        supervision=db.load_table("supervision", year),
        entry_overrides=db.load_table("entry_overrides", year),
        exclusions=db.load_table("exclusions", year),
        manual_shares=db.load_table("manual_shares", year),
        collections=db.load_data("collections", year),
        time_entries=db.load_data("time_entries", year),
        invoices=db.load_data("invoices", year),
        expense_categories=db.load_table("expense_categories", year),
        category_splits=db.load_table("category_splits", year),
        expenses=db.load_table("expenses", year),
        expense_overrides=db.load_table("expense_overrides", year),
        carryforwards=db.load_table("carryforwards", year),
        lockstep_overrides=db.load_table("lockstep_overrides", year),
    )


def run(db: Database, year: int) -> tuple[CompensationInputs, CompensationResult]:
    """Load inputs and calculate."""
    inputs = load_inputs(db, year)
    return inputs, calculate(inputs)


def _frame_json(df: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))


def build_snapshot(db: Database, year: int, inputs: CompensationInputs,
                   result: CompensationResult) -> dict[str, Any]:
    """Everything that must be preserved for a finalized year."""
    batches = db.batches(year)
    return {
        "year": year,
        "created_at": now_iso(),
        "policy": inputs.policy.to_dict(),
        "imported_files": _frame_json(batches.drop(columns=["mapping_json"])),
        "column_mappings": _frame_json(batches[["batch_id", "dataset", "filename", "mapping_json"]]),
        "partner_roster": _frame_json(inputs.partners),
        "timekeepers": _frame_json(inputs.timekeepers),
        "matters": _frame_json(inputs.matters),
        "matter_originators": _frame_json(inputs.originators),
        "supervisory_mappings": _frame_json(inputs.supervision),
        "entry_overrides": _frame_json(inputs.entry_overrides),
        "exclusions": _frame_json(inputs.exclusions),
        "manual_working_shares": _frame_json(inputs.manual_shares),
        "expense_categories": _frame_json(inputs.expense_categories),
        "expense_category_splits": _frame_json(inputs.category_splits),
        "expenses": _frame_json(inputs.expenses),
        "expense_overrides": _frame_json(inputs.expense_overrides),
        "carryforwards_in": _frame_json(inputs.carryforwards),
        "expense_allocation": _frame_json(result.expenses.detail.astype(str)),
        "results": {
            "pools": {k: str(v) for k, v in result.pools.items()},
            "partner_summary": _frame_json(result.partner_summary.astype(
                {c: str for c in result.partner_summary.columns
                 if result.partner_summary[c].dtype == object})),
            "reconciliation": _frame_json(result.reconciliation.astype(str)),
            "exceptions": _frame_json(result.exceptions),
        },
        "lockstep_proposal": _frame_json(result.lockstep.table),
        "final_lockstep": _frame_json(result.final_lockstep),
        "manual_overrides": _frame_json(inputs.lockstep_overrides),
        "audit_log": _frame_json(db.audit_log(year)),
    }


def finalize_year(db: Database, year: int, user: str) -> int:
    """Finalize a year: verify readiness, snapshot everything and lock it.

    Raises ``ValueError`` listing blockers when the year is not ready.
    """
    if db.is_finalized(year):
        raise ReadOnlyYearError(f"{year} is already finalized.")
    if not user.strip():
        raise ValueError("Enter your name in the sidebar before finalizing.")
    inputs, result = run(db, year)
    blockers = finalization_blockers(result)
    if blockers:
        raise ValueError("Cannot finalize: " + "; ".join(blockers))
    db.log(year, user, "Finalization requested", {"total": str(result.pools["distributable"])})
    snap = build_snapshot(db, year, inputs, result)
    snapshot_id = db.save_snapshot(year, "finalization", snap, user)
    db.mark_finalized(year, user)
    return snapshot_id


def start_year_from_prior(db: Database, from_year: int, to_year: int, user: str,
                          use_final_lockstep: bool = True) -> list[str]:
    """Copy setup (policy, roster, timekeepers, matters, originators, mappings) to a new year.

    When ``use_final_lockstep`` is set, current weights for the new year are the
    prior year's final (approved) weights.
    """
    db.assert_writable(to_year)
    notes = []
    policy = db.load_policy(from_year)
    policy.year = to_year
    policy.period_start = None
    policy.period_end = None
    policy.__post_init__()
    db.save_policy(policy, user)
    partners = db.load_table("partners", from_year)
    if use_final_lockstep:
        snap = db.latest_final_snapshot(from_year)
        if snap:
            finals = {r["Partner"]: r["Final weight"] for r in snap["final_lockstep"]}
            notes.append(f"Lockstep weights taken from the finalized {from_year} snapshot.")
        else:
            _, result = run(db, from_year)
            finals = dict(zip(result.final_lockstep["Partner"], result.final_lockstep["Final weight"]))
            notes.append(f"{from_year} is not finalized; its current final lockstep proposal was used.")
        partners["lockstep_weight"] = [finals.get(n, w) for n, w in
                                       zip(partners["name"], partners["lockstep_weight"])]
    db.save_table("partners", to_year, partners, user, f"Roster copied from {from_year}")
    for name in ("timekeepers", "matters", "originators", "supervision", "manual_shares"):
        db.save_table(name, to_year, db.load_table(name, from_year), user,
                      f"{name} copied from {from_year}")
    notes += copy_expense_setup(db, from_year, to_year, user)
    db.log(to_year, user, f"Year set up from {from_year}", notes)
    return notes


def rename_partner(db: Database, year: int, old: str, new: str, user: str) -> int:
    """Rename a partner in the roster and every year-scoped table that references them.

    Imported TimeSolv data is never rewritten; the partner's timekeeper record
    keeps its TimeSolv name and its ``linked_partner`` is updated instead.
    Returns the number of changed cells.
    """
    from .models import norm_name

    old, new = old.strip(), new.strip()
    if not new:
        raise ValueError("The new name cannot be blank.")
    partners = db.load_table("partners", year)
    if norm_name(new) in {norm_name(n) for n in partners["name"]} and norm_name(new) != norm_name(old):
        raise ValueError(f"A partner named '{new}' already exists.")
    targets = {
        "partners": ["name"], "originators": ["partner"], "supervision": ["partner"],
        "entry_overrides": ["partner"], "manual_shares": ["partner"],
        "category_splits": ["partner"], "expense_overrides": ["partner"],
        "carryforwards": ["partner"],
        "lockstep_overrides": ["partner"],
        "timekeepers": ["linked_partner", "default_supervisor"],
        "matters": ["comp_supervising_partner"],
    }
    changed = 0
    for table, cols in targets.items():
        df = db.load_table(table, year)
        n = 0
        for c in cols:
            mask = df[c].map(norm_name) == norm_name(old)
            n += int(mask.sum())
            df.loc[mask, c] = new
        if table == "timekeepers":
            # Partner timekeepers without an explicit link keep matching through linked_partner.
            mask = (df["name"].map(norm_name) == norm_name(old)) & (df["linked_partner"] == "")
            df.loc[mask, "linked_partner"] = new
            n += int(mask.sum())
        if n:
            db.save_table(table, year, df, user, f"Partner renamed '{old}' -> '{new}' ({table})")
            changed += n
    return changed


def assign_responsible_as_originator(db: Database, year: int, user: str) -> list[str]:
    """Explicit, audited action: for matters with NO originator, use the Responsible
    Professional as 100% originator when that person is on the partner roster."""
    from .models import norm_name

    partners = {norm_name(n): n for n in db.load_table("partners", year)["name"]}
    matters = db.load_table("matters", year)
    origs = db.load_table("originators", year)
    have = {norm_name(m) for m in origs["matter_id"]}
    added = []
    rows = origs.to_dict("records")
    for m in matters.to_dict("records"):
        name = partners.get(norm_name(m["responsible_professional"]))
        if norm_name(m["matter_id"]) not in have and name:
            rows.append({"matter_id": m["matter_id"], "partner": name, "share_pct": 100,
                         "notes": "Set from Responsible Professional by user action"})
            added.append(f"{m['matter_id']} -> {name}")
    if added:
        db.save_table("originators", year, pd.DataFrame(rows), user,
                      "Responsible Professional copied as originator (explicit user action)")
    return added


def prior_year_shortfalls(db: Database, from_year: int) -> dict[str, float]:
    """Amounts each partner carries forward out of ``from_year``.

    Uses the finalized snapshot when one exists, otherwise the live calculation.
    """
    snap = db.latest_final_snapshot(from_year)
    if snap:
        rows = snap["results"]["partner_summary"]
    else:
        _, result = run(db, from_year)
        rows = result.partner_summary.to_dict("records")
    out = {}
    for r in rows:
        amt = float(r.get("Carry forward to next year") or 0)
        if amt > 0:
            out[r["Partner"]] = amt
    return out


def load_carryforwards(db: Database, from_year: int, to_year: int, user: str) -> list[str]:
    """Replace ``to_year`` carry-forwards with the shortfalls carried out of ``from_year``."""
    db.assert_writable(to_year)
    shortfalls = prior_year_shortfalls(db, from_year)
    finalized = db.is_finalized(from_year)
    rows = [{"partner": p, "amount": round(a, 2), "source_year": str(from_year),
             "notes": "From finalized snapshot" if finalized else "From draft (not finalized) results"}
            for p, a in shortfalls.items()]
    db.save_table("carryforwards", to_year, pd.DataFrame(
        rows, columns=["partner", "amount", "source_year", "notes"]), user,
        f"Carry-forwards loaded from {from_year}")
    if not rows:
        return [f"No partner carried a shortfall out of {from_year}."]
    return [f"{len(rows)} carry-forward(s) loaded from {from_year}"
            + ("" if finalized else " (prior year not finalized - reload after finalizing)") + "."]


def copy_expense_setup(db: Database, from_year: int, to_year: int, user: str) -> list[str]:
    """Copy expense categories and their splits, and load carry-forwards."""
    for name in ("expense_categories", "category_splits"):
        db.save_table(name, to_year, db.load_table(name, from_year), user,
                      f"{name} copied from {from_year}")
    return ["Expense categories and splits copied."] + load_carryforwards(
        db, from_year, to_year, user)
