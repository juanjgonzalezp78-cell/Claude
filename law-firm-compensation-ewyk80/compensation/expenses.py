"""Partner-borne expenses: allocation rules, net compensation and carry-forwards.

Expenses are entered (or imported) individually and belong to a *category*.
Each category has a default allocation rule; an individual expense may
override the rule with its own partner split (a written reason is required).

Allocation rules
----------------
* **Direct** - 100% to one partner (the category's single split row).
* **Fixed split** - fixed percentages among named partners (must total 100%).
* **Equal** - equally among active partners, prorated by the days each partner
  was a partner during the compensation period (start/end dates on the roster)
  when the policy enables proration.
* **Proportional to EWYK credit** - by each active partner's total EWYK credit.
* **Proportional to gross compensation** - by each active partner's gross
  compensation.
* **By associate hours** - by the hours of the category's associate that were
  credited to each partner by the supervisory-attribution hierarchy.

Every split uses cent-exact largest-remainder allocation, so allocated amounts
always equal the expense.  Anything that cannot be allocated is reported as a
blocking exception.

Net compensation
----------------
``Net = Gross compensation - Allocated expenses - Prior-year carry-forward``.
When Net is negative the policy decides whether the shortfall is carried
forward (deducted from next year's compensation) or recorded as an amount owed
to the firm now.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from .models import (HUNDRED, ZERO, ExceptionItem, Policy, Severity, allocate_cents, clean_str,
                     money, norm_name, parse_date, to_decimal)


class ExpenseRule:
    """Allowed allocation rules."""

    DIRECT = "Direct (one partner)"
    FIXED = "Fixed split"
    EQUAL = "Equal (active partners)"
    EWYK = "Proportional to EWYK credit"
    GROSS = "Proportional to gross compensation"
    ASSOCIATE_HOURS = "By associate hours"

    ALL = (DIRECT, FIXED, EQUAL, EWYK, GROSS, ASSOCIATE_HOURS)


class NegativeNet:
    """Treatment of negative net compensation."""

    CARRY_FORWARD = "Carry forward to next year"
    OWED_NOW = "Amount owed to the firm now"

    ALL = (CARRY_FORWARD, OWED_NOW)


@dataclass
class ExpenseResult:
    """Outputs of the expense allocation."""

    detail: pd.DataFrame
    by_partner: pd.DataFrame
    partner_totals: dict[str, Decimal]
    carry_in: dict[str, Decimal]
    total_expenses: Decimal
    total_allocated: Decimal
    exceptions: list[ExceptionItem] = field(default_factory=list)


def _records(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out = []
    for rec in df.to_dict("records"):
        clean = {}
        for k, v in rec.items():
            try:
                clean[k] = None if pd.isna(v) else v
            except (TypeError, ValueError):
                clean[k] = v
        out.append(clean)
    return out


def active_fraction(rec: dict[str, Any], start: date, end: date) -> Decimal:
    """Share of the period during which a partner was a partner (0-1)."""
    try:
        s = parse_date(rec.get("start_date")) or start
        e = parse_date(rec.get("end_date")) or end
    except ValueError:
        return Decimal(1)
    s, e = max(s, start), min(e, end)
    if e < s:
        return ZERO
    return Decimal((e - s).days + 1) / Decimal((end - start).days + 1)


def allocate_expenses(policy: Policy, partners: pd.DataFrame, categories: pd.DataFrame,
                      category_splits: pd.DataFrame, expenses: pd.DataFrame,
                      expense_overrides: pd.DataFrame, carryforwards: pd.DataFrame,
                      partner_summary: pd.DataFrame,
                      attribution_detail: pd.DataFrame) -> ExpenseResult:
    """Allocate every expense to partners according to its rule."""
    exc: list[ExceptionItem] = []
    roster = {norm_name(p.get("name")): p for p in _records(partners) if clean_str(p.get("name"))}
    names = {k: clean_str(v["name"]) for k, v in roster.items()}
    active = [names[k] for k, p in roster.items() if p.get("active")]

    def resolve(name: Any) -> str | None:
        return names.get(norm_name(name))

    # ------------------------------------------------------------ drivers
    summary = {r["Partner"]: r for r in _records(partner_summary)}
    weights: dict[str, dict[str, Decimal]] = {
        ExpenseRule.EQUAL: {
            n: (active_fraction(roster[norm_name(n)], policy.period_start, policy.period_end)
                if policy.prorate_equal_expenses else Decimal(1)) for n in active},
        ExpenseRule.EWYK: {n: to_decimal(summary.get(n, {}).get("Total EWYK credit")) for n in active},
        ExpenseRule.GROSS: {n: to_decimal(summary.get(n, {}).get("Total compensation"))
                            for n in active},
    }
    assoc_hours: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in _records(attribution_detail):
        partner = resolve(r.get("Credited partner"))
        if partner and to_decimal(r.get("Qualifying measure")) > 0 and not clean_str(r.get("Excluded")):
            assoc_hours[norm_name(r.get("Timekeeper"))][partner] += to_decimal(
                r.get("Hours credited to partner"))

    # ------------------------------------------------------------ categories
    cats = {norm_name(c.get("category")): c for c in _records(categories)
            if clean_str(c.get("category"))}
    splits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in _records(category_splits):
        splits[norm_name(s.get("category"))].append(s)
    overrides: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in _records(expense_overrides):
        overrides[norm_name(o.get("expense_id"))].append(o)

    def split_weights(rows: list[dict[str, Any]], label: str) -> dict[str, Decimal] | str:
        total = sum((to_decimal(r.get("share_pct")) for r in rows), ZERO)
        if not rows:
            return f"{label} has no partner split"
        if total != HUNDRED:
            return f"{label} split totals {total}% (must be 100%)"
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in rows:
            p = resolve(r.get("partner"))
            if p is None:
                return f"{label} names '{clean_str(r.get('partner'))}', who is not on the roster"
            out[p] += to_decimal(r.get("share_pct"))
        return dict(out)

    detail: list[dict[str, Any]] = []
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    total_exp = ZERO
    total_alloc = ZERO
    for i, e in enumerate(_records(expenses), start=1):
        amount = money(to_decimal(e.get("amount")))
        if amount == 0:
            continue
        total_exp += amount
        eid = clean_str(e.get("expense_id")) or f"row {i}"
        cat_name = clean_str(e.get("category"))
        cat = cats.get(norm_name(cat_name))
        base = {"Expense ID": eid, "Date": parse_date(e.get("expense_date")) if
                e.get("expense_date") else None, "Category": cat_name,
                "Description": clean_str(e.get("description")), "Expense amount": amount}
        if norm_name(eid) in overrides:
            rule = "Entry override"
            res = split_weights(overrides[norm_name(eid)], f"Override for expense {eid}")
        elif cat is None:
            rule = "-"
            res = f"category '{cat_name}' is not defined"
        else:
            rule = clean_str(cat.get("rule"))
            if rule in (ExpenseRule.DIRECT, ExpenseRule.FIXED):
                res = split_weights(splits.get(norm_name(cat_name), []), f"Category '{cat_name}'")
                if rule == ExpenseRule.DIRECT and isinstance(res, dict) and len(res) != 1:
                    res = f"Category '{cat_name}' is Direct but lists {len(res)} partners"
            elif rule in weights:
                res = {k: v for k, v in weights[rule].items() if v > 0}
                if not res:
                    res = f"no active partner has a positive basis for rule '{rule}'"
            elif rule == ExpenseRule.ASSOCIATE_HOURS:
                assoc = clean_str(cat.get("associate"))
                res = {k: v for k, v in assoc_hours.get(norm_name(assoc), {}).items() if v > 0}
                if not assoc:
                    res = f"Category '{cat_name}' uses associate hours but names no associate"
                elif not res:
                    res = f"no credited hours found for associate '{assoc}'"
            else:
                res = f"Category '{cat_name}' has unknown rule '{rule}'"
        if isinstance(res, str):
            exc.append(ExceptionItem(Severity.BLOCKING, "Expenses not allocated",
                                     f"Expense {eid} ({cat_name}, {amount}): {res}.", eid,
                                     amount=amount,
                                     resolution="Partner Expenses: fix the category rule/split "
                                                "or the entry override."))
            detail.append({**base, "Rule": rule, "Partner": "(unallocated)", "Basis": 0.0,
                           "Share %": 0.0, "Allocated amount": ZERO})
            continue
        partners_sorted = list(res.keys())
        basis_total = sum(res.values(), ZERO)
        parts = allocate_cents(amount, [res[p] for p in partners_sorted])
        for p, amt in zip(partners_sorted, parts):
            totals[p] += amt
            total_alloc += amt
            detail.append({**base, "Rule": rule, "Partner": p, "Basis": float(res[p]),
                           "Share %": float(res[p] / basis_total * HUNDRED), "Allocated amount": amt})
            if norm_name(p) in roster and not roster[norm_name(p)].get("active"):
                exc.append(ExceptionItem(Severity.WARNING, "Expense charged to inactive partner",
                                         f"{amt} of expense {eid} is charged to inactive partner {p}.",
                                         eid, amount=amt,
                                         resolution="Confirm the charge or change the split."))

    carry_in: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for c in _records(carryforwards):
        p = resolve(c.get("partner"))
        amt = money(to_decimal(c.get("amount")))
        if p is None:
            exc.append(ExceptionItem(Severity.BLOCKING, "Expenses not allocated",
                                     f"Carry-forward for '{clean_str(c.get('partner'))}' does not "
                                     "match a partner on the roster.", amount=amt,
                                     resolution="Partner Expenses > Carry-forwards."))
            continue
        carry_in[p] += amt

    by_partner = pd.DataFrame(detail)
    if not by_partner.empty:
        by_partner = (by_partner[by_partner["Partner"] != "(unallocated)"]
                      .assign(**{"Allocated amount": lambda d: d["Allocated amount"].astype(float)})
                      .pivot_table(index="Partner", columns="Category", values="Allocated amount",
                                   aggfunc="sum", fill_value=0.0))
        by_partner["Total expenses"] = by_partner.sum(axis=1)
        by_partner = by_partner.reset_index()
    return ExpenseResult(
        detail=pd.DataFrame(detail, columns=["Expense ID", "Date", "Category", "Description",
                                             "Expense amount", "Rule", "Partner", "Basis",
                                             "Share %", "Allocated amount"]),
        by_partner=by_partner, partner_totals=dict(totals), carry_in=dict(carry_in),
        total_expenses=total_exp, total_allocated=total_alloc, exceptions=exc)


def apply_net(summary_rows: list[dict[str, Any]], result: ExpenseResult, policy: Policy,
              exc: list[ExceptionItem]) -> None:
    """Add expense, carry-forward and net columns to the partner summary rows (in place)."""
    for r in summary_rows:
        name = r["Partner"]
        gross = to_decimal(r["Total compensation"])
        expenses = result.partner_totals.get(name, ZERO)
        carry = result.carry_in.get(name, ZERO)
        net = gross - expenses - carry
        r["Allocated expenses"] = expenses
        r["Prior-year carry-forward"] = carry
        r["Net compensation"] = net
        r["Net payable"] = net if net > 0 else ZERO
        short = -net if net < 0 else ZERO
        carry_out = short if policy.negative_net_treatment == NegativeNet.CARRY_FORWARD else ZERO
        owed = short if policy.negative_net_treatment == NegativeNet.OWED_NOW else ZERO
        r["Carry forward to next year"] = carry_out
        r["Owed to the firm"] = owed
        if short > 0:
            if not r["Active"] and carry_out > 0:
                exc.append(ExceptionItem(
                    Severity.WARNING, "Negative net compensation",
                    f"Inactive partner {name} has a shortfall of {short} that would be carried "
                    "forward, but will not receive future compensation to offset it.", name,
                    amount=short, resolution="Consider collecting this amount directly."))
            else:
                exc.append(ExceptionItem(
                    Severity.INFO, "Negative net compensation",
                    f"{name}'s expenses exceed compensation by {short}; "
                    + ("carried forward to next year." if carry_out else "owed to the firm."),
                    name, amount=short, resolution="No action required unless the policy changes."))
