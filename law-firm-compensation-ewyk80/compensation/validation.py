"""Validation rules for policy inputs, the partner roster, mappings and finalization."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

import pandas as pd

from .models import (HUNDRED, ZERO, Policy, Severity, WorkingMethod,
                     clean_str, norm_name, parse_date, to_decimal)


def validate_policy(policy: Policy) -> list[str]:
    """Return a list of policy problems (empty = valid)."""
    problems: list[str] = []
    if to_decimal(policy.distributable_pool) <= 0:
        problems.append("The distributable partner-compensation pool must be greater than zero.")
    for name in ("equal_pct", "ewyk_pct", "origination_pct", "working_pct"):
        if not ZERO <= to_decimal(getattr(policy, name)) <= HUNDRED:
            problems.append(f"{name.replace('_pct', '').replace('_', ' ').title()} percentage must "
                            "be between 0 and 100.")
    pools = policy.equal_pct + policy.ewyk_pct
    if pools != HUNDRED:
        problems.append(f"Equal ({policy.equal_pct}%) + EWYK ({policy.ewyk_pct}%) total {pools}%; "
                        "they must total 100%.")
    ewyk = policy.origination_pct + policy.working_pct
    if ewyk != HUNDRED:
        problems.append(f"Origination ({policy.origination_pct}%) + working ({policy.working_pct}%) "
                        f"total {ewyk}%; they must total 100%.")
    if policy.working_method not in WorkingMethod.ALL:
        problems.append(f"Unknown working-share methodology '{policy.working_method}'.")
    if policy.period_start and policy.period_end and policy.period_start > policy.period_end:
        problems.append("Compensation period start is after its end.")
    return problems


def validate_roster(partners: list[dict[str, Any]] | pd.DataFrame, policy: Policy) -> list[str]:
    """Roster checks: names present and unique, at least one active partner."""
    if isinstance(partners, pd.DataFrame):
        partners = partners.to_dict("records")
    problems: list[str] = []
    names = [clean_str(p.get("name")) for p in partners]
    if any(not n for n in names):
        problems.append("Every partner must have a name.")
    seen: set[str] = set()
    for n in names:
        if n and norm_name(n) in seen:
            problems.append(f"Partner name '{n}' appears more than once.")
        seen.add(norm_name(n))
    if not any(bool(p.get("active")) for p in partners):
        problems.append("There are no active partners.")
    mps = [p for p in partners if bool(p.get("is_managing_partner"))]
    if len(mps) > 1:
        problems.append(f"More than one partner is flagged as Managing Partner ({len(mps)}).")
    return problems


def validate_share_groups(df: pd.DataFrame, group_cols: list[str], pct_col: str,
                          label: str) -> list[str]:
    """Check that percentage groups (e.g. originators per matter) total 100%."""
    problems: list[str] = []
    if df is None or df.empty:
        return problems
    totals: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for rec in df.to_dict("records"):
        key = tuple(clean_str(rec.get(c)) for c in group_cols)
        if not any(key):
            continue
        totals[key] += to_decimal(rec.get(pct_col))
    for key, total in totals.items():
        if total != HUNDRED:
            problems.append(f"{label} {' / '.join(k or '(blank)' for k in key)}: total {total}% "
                            "(must be 100%).")
    return problems


def validate_supervision(df: pd.DataFrame, partner_names: list[str]) -> list[str]:
    """Validate associate-matter mappings: partners known, dates ordered, splits total 100%."""
    problems: list[str] = []
    if df is None or df.empty:
        return problems
    known = {norm_name(n) for n in partner_names}
    for i, rec in enumerate(df.to_dict("records"), start=1):
        if not clean_str(rec.get("timekeeper")) or not clean_str(rec.get("matter_id")):
            problems.append(f"Row {i}: timekeeper and matter ID are required.")
        if norm_name(rec.get("partner")) not in known:
            problems.append(f"Row {i}: credited partner '{clean_str(rec.get('partner'))}' is not on "
                            "the partner roster.")
        try:
            s, e = parse_date(rec.get("start_date")), parse_date(rec.get("end_date"))
        except ValueError as exc:
            problems.append(f"Row {i}: {exc}")
            continue
        if s and e and s > e:
            problems.append(f"Row {i}: effective start date is after the end date.")
    problems += validate_share_groups(
        df.assign(start_date=df["start_date"].astype(str), end_date=df["end_date"].astype(str)),
        ["timekeeper", "matter_id", "start_date", "end_date"], "allocation_pct",
        "Associate-matter mapping")
    return problems


def finalization_blockers(result: Any) -> list[str]:
    """Reasons a year cannot be finalized (empty = ready)."""
    blockers: list[str] = []
    exc = result.exceptions
    if not exc.empty:
        blocking = exc[exc["Severity"] == Severity.BLOCKING]
        for cat, n in blocking["Category"].value_counts().items():
            blockers.append(f"{n} blocking exception(s): {cat}")
    if not result.reconciled:
        blockers.append("Compensation does not reconcile to the distributable pool.")
    return blockers
