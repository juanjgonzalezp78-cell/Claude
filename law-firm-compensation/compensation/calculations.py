"""Compensation engine: credits, EWYK, equal share, lockstep, reconciliation.

This module is UI-independent.  :func:`calculate` takes a
:class:`CompensationInputs` bundle of pandas DataFrames (as stored in SQLite or
built in tests) and returns a :class:`CompensationResult` containing every
intermediate table needed for drill-down, exceptions and the Excel report.

Money is handled with :class:`~decimal.Decimal`; every split of a dollar
amount uses :func:`~compensation.models.allocate_cents` so the parts always
add back exactly to the whole.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from .attribution import AttributedEntry, Attributor, Roster
from .expenses import ExpenseResult, allocate_expenses, apply_net
from .lockstep import LockstepPartner, LockstepProposal, check_final_weights, propose_lockstep
from .models import (CENT, HUNDRED, ZERO, AttributionSource, ExceptionItem, Policy, Severity,
                     WorkingBucket, WorkingMethod, allocate_cents, clean_str, money, norm_name,
                     parse_date, to_decimal)

METHOD_INVOICE = "Invoice-level"
METHOD_MATTER = "Matter-level fallback"
METHOD_MANUAL = "Manual percentages"
BUCKET_MANUAL = "Manual allocation"


def _records(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out = []
    for rec in df.to_dict("records"):
        out.append({k: (None if _isna(v) else v) for k, v in rec.items()})
    return out


def _isna(v: Any) -> bool:
    try:
        return bool(pd.isna(v)) if not isinstance(v, (list, dict, set)) else False
    except (TypeError, ValueError):
        return False


@dataclass
class CompensationInputs:
    """Everything the engine needs for one compensation year."""

    policy: Policy
    partners: pd.DataFrame
    timekeepers: pd.DataFrame = field(default_factory=pd.DataFrame)
    matters: pd.DataFrame = field(default_factory=pd.DataFrame)
    originators: pd.DataFrame = field(default_factory=pd.DataFrame)
    supervision: pd.DataFrame = field(default_factory=pd.DataFrame)
    entry_overrides: pd.DataFrame = field(default_factory=pd.DataFrame)
    exclusions: pd.DataFrame = field(default_factory=pd.DataFrame)
    manual_shares: pd.DataFrame = field(default_factory=pd.DataFrame)
    collections: pd.DataFrame = field(default_factory=pd.DataFrame)
    time_entries: pd.DataFrame = field(default_factory=pd.DataFrame)
    expense_categories: pd.DataFrame = field(default_factory=pd.DataFrame)
    category_splits: pd.DataFrame = field(default_factory=pd.DataFrame)
    expenses: pd.DataFrame = field(default_factory=pd.DataFrame)
    expense_overrides: pd.DataFrame = field(default_factory=pd.DataFrame)
    carryforwards: pd.DataFrame = field(default_factory=pd.DataFrame)
    lockstep_overrides: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class CompensationResult:
    """All outputs of a calculation run."""

    policy: Policy
    pools: dict[str, Decimal]
    partner_summary: pd.DataFrame
    origination_detail: pd.DataFrame
    working_detail: pd.DataFrame
    attribution_detail: pd.DataFrame
    collection_status: pd.DataFrame
    exceptions: pd.DataFrame
    reconciliation: pd.DataFrame
    lockstep: LockstepProposal
    final_lockstep: pd.DataFrame
    metrics: dict[str, Any]
    expenses: ExpenseResult

    @property
    def blocking_count(self) -> int:
        """Number of blocking exceptions."""
        if self.exceptions.empty:
            return 0
        return int((self.exceptions["Severity"] == Severity.BLOCKING).sum())

    @property
    def reconciled(self) -> bool:
        """True when every reconciliation check passes."""
        return bool((self.reconciliation["Status"] != "DIFFERENCE").all()) if not \
            self.reconciliation.empty else False


# ----------------------------------------------------------------------------
# Collections
# ----------------------------------------------------------------------------

NOT_COLLECTED_WORDS = ("void", "bounced", "nsf", "reversed", "reversal", "pending", "failed",
                       "declined", "unpaid", "outstanding", "uncollected", "chargeback", "refunded")
NON_CASH_TYPES = ("write off", "write-off", "writeoff", "written off", "discount", "credit memo",
                  "credit note", "adjustment")
TRUST_WORDS = ("trust", "retainer", "unapplied", "advance deposit")


def classify_collection(c: dict[str, Any], policy: Policy) -> tuple[bool, str]:
    """Decide whether a collection row earns EWYK credit. Returns (include, reason)."""
    if c.get("excluded"):
        return False, clean_str(c.get("exclusion_reason")) or "Manually excluded"
    ptype = norm_name(c.get("payment_type"))
    status = norm_name(c.get("payment_status"))
    if any(w in status for w in NOT_COLLECTED_WORDS) or any(w in ptype for w in NOT_COLLECTED_WORDS):
        return False, "Payment not actually collected (status/type indicates void, pending, " \
                      "reversed or outstanding)"
    if any(w in ptype for w in NON_CASH_TYPES):
        return False, "Write-off / discount / credit - not a collection"
    if any(w in ptype for w in TRUST_WORDS) and not clean_str(c.get("invoice_id")):
        return False, "Unapplied trust / retainer deposit"
    d = parse_date(c.get("collection_date"))
    if d is None:
        return False, "Missing collection date"
    if d < policy.period_start or d > policy.period_end:
        return False, f"Collected outside the compensation period ({d})"
    fee = to_decimal(c.get("fee_amount"))
    if fee == 0:
        return False, "No amount allocated to professional fees (expense/tax only)"
    if fee < 0:
        return False, "Negative fee allocation (refund/reversal) - requires policy review"
    return True, ""


def collection_ref(c: dict[str, Any]) -> str:
    """Readable identifier for a collection row."""
    pid = clean_str(c.get("payment_id")) or f"row {c.get('row_id', '?')}"
    inv = clean_str(c.get("invoice_id"))
    return f"{pid} / {inv}" if inv else pid


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------

def calculate(inputs: CompensationInputs) -> CompensationResult:
    """Run the full compensation calculation."""
    policy = inputs.policy
    exc: list[ExceptionItem] = []
    partners = _records(inputs.partners)
    for p in partners:
        p["active"] = bool(p.get("active"))
        p["is_managing_partner"] = bool(p.get("is_managing_partner"))
        p["lockstep_weight"] = to_decimal(p.get("lockstep_weight"))
    roster = Roster(partners)
    active = roster.active_names()

    _policy_checks(policy, exc)
    _roster_checks(partners, policy, exc)

    attributor = Attributor(policy, roster, _records(inputs.timekeepers), _records(inputs.matters),
                            _records(inputs.supervision), _records(inputs.entry_overrides),
                            _records(inputs.exclusions))
    matter_index = attributor.matters

    # ---------------------------------------------------------------- originators
    originators: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in _records(inputs.originators):
        if norm_name(o.get("matter_id")):
            originators[norm_name(o["matter_id"])].append(o)
    orig_valid: dict[str, list[tuple[str, Decimal]]] = {}
    for mkey, rows in originators.items():
        total = sum((to_decimal(r.get("share_pct")) for r in rows), ZERO)
        names = [(roster.resolve(r.get("partner")), to_decimal(r.get("share_pct")), r) for r in rows]
        bad = [clean_str(r.get("partner")) for n, _, r in names if n is None]
        mid = clean_str(rows[0].get("matter_id"))
        if total != HUNDRED:
            exc.append(ExceptionItem(Severity.BLOCKING, "Originator allocations not totaling 100%",
                                     f"Originator shares for matter {mid} total {total}%.",
                                     matter_id=mid, resolution="Firm Setup > Matter originators: "
                                     "make the shares total exactly 100%."))
        elif bad:
            exc.append(ExceptionItem(Severity.BLOCKING, "Missing originating partners",
                                     f"Originator(s) {', '.join(bad)} on matter {mid} are not on "
                                     "the partner roster.", matter_id=mid,
                                     resolution="Correct the originator name or add the partner."))
        else:
            orig_valid[mkey] = [(n, s) for n, s, _ in names]
            for n, _, _ in names:
                if not roster.is_active(n):
                    exc.append(ExceptionItem(Severity.WARNING, "Credit to inactive partner",
                                             f"Inactive partner {n} originates matter {mid}; that "
                                             "credit earns no compensation.", matter_id=mid,
                                             resolution="Confirm the originator or reassign."))

    # ---------------------------------------------------------------- manual shares
    manual: dict[str, list[tuple[str, Decimal]]] = {}
    manual_bad: dict[str, str] = {}
    if policy.working_method == WorkingMethod.MANUAL:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in _records(inputs.manual_shares):
            grouped[norm_name(r.get("matter_id"))].append(r)
        for mkey, rows in grouped.items():
            total = sum((to_decimal(r.get("share_pct")) for r in rows), ZERO)
            resolved = [(roster.resolve(r.get("partner")), to_decimal(r.get("share_pct"))) for r in rows]
            if total != HUNDRED or any(n is None for n, _ in resolved):
                manual_bad[mkey] = f"Manual working shares total {total}% or name unknown partners"
            else:
                manual[mkey] = [(n, s) for n, s in resolved if n]

    # ---------------------------------------------------------------- time entries
    entries: list[AttributedEntry] = []
    flag_counts: dict[str, int] = defaultdict(int)
    issue_entries: dict[str, list[AttributedEntry]] = defaultdict(list)
    for e in _records(inputs.time_entries):
        ae, issues, flags = attributor.attribute(e)
        entries.append(ae)
        for f in flags:
            flag_counts[f] += 1
        for i in issues:
            issue_entries[i].append(ae)
    by_invoice: dict[tuple[str, str], list[AttributedEntry]] = defaultdict(list)
    by_matter: dict[str, list[AttributedEntry]] = defaultdict(list)
    for ae in entries:
        by_matter[norm_name(ae.matter_id)].append(ae)
        if ae.invoice_id:
            by_invoice[(norm_name(ae.matter_id), norm_name(ae.invoice_id))].append(ae)

    # ---------------------------------------------------------------- collections
    coll_rows = _records(inputs.collections)
    included: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []
    for c in coll_rows:
        ok, reason = classify_collection(c, policy)
        c["_ref"] = collection_ref(c)
        c["_fee"] = money(to_decimal(c.get("fee_amount")))
        if ok:
            included.append(c)
        else:
            status_rows.append(_status_row(c, False, reason))
    _duplicate_checks(coll_rows, included, exc)

    invoice_matched = {
        (norm_name(c.get("matter_id")), norm_name(c.get("invoice_id"))) for c in included
        if clean_str(c.get("invoice_id"))
        and (norm_name(c.get("matter_id")), norm_name(c.get("invoice_id"))) in by_invoice
    }

    orig_detail: list[dict[str, Any]] = []
    work_detail: list[dict[str, Any]] = []
    credits: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    unallocated_total = ZERO
    fallback_count = 0
    no_hours_matters: set[str] = set()
    missing_orig_matters: set[str] = set()

    for c in included:
        fee = c["_fee"]
        mid = clean_str(c.get("matter_id"))
        mkey = norm_name(mid)
        base = {"Collection": c["_ref"], "Collection date": parse_date(c.get("collection_date")),
                "Client": clean_str(c.get("client")) or clean_str(matter_index.get(mkey, {}).get("client")),
                "Matter ID": mid, "Invoice ID": clean_str(c.get("invoice_id")),
                "Payment ID": clean_str(c.get("payment_id")), "Fees collected": fee}
        if not mid:
            exc.append(ExceptionItem(Severity.BLOCKING, "Missing matter IDs",
                                     f"Collection {c['_ref']} has no matter/project ID; "
                                     f"{fee} of fees cannot be credited.", c["_ref"], amount=fee,
                                     resolution="Correct the source export (or exclude the row on "
                                                "the Imports page) and re-import."))
            unallocated_total += fee
            status_rows.append(_status_row(c, True, "", unallocated=fee, method="-"))
            continue
        if mkey not in matter_index:
            exc.append(ExceptionItem(Severity.WARNING, "Collections for unknown matters",
                                     f"Matter {mid} on collection {c['_ref']} is not in the "
                                     "matters list.", c["_ref"], mid, amount=fee,
                                     resolution="Import the matters file or add the matter in "
                                                "Firm Setup."))
        orig_part, work_part = allocate_cents(fee, [policy.origination_pct, policy.working_pct]) \
            if policy.origination_pct + policy.working_pct > 0 else (ZERO, ZERO)
        unalloc = ZERO

        # ---- origination
        origs = orig_valid.get(mkey)
        if not origs:
            unalloc += orig_part
            if mkey not in originators:
                missing_orig_matters.add(mid)
        else:
            parts = allocate_cents(orig_part, [s for _, s in origs])
            for (pname, share), amt in zip(origs, parts):
                credits[pname]["orig"] += amt
                orig_detail.append({**base, "Origination %": policy.origination_pct,
                                    "Origination portion": orig_part, "Partner": pname,
                                    "Originator share %": share, "Origination credit": amt})
        originator_names = {n for n, _ in (origs or [])}

        # ---- working
        method, basis, note = _working_basis(c, mkey, policy, by_invoice, by_matter,
                                             invoice_matched, manual, originator_names)
        if method == METHOD_MATTER:
            fallback_count += 1
        if policy.working_method == WorkingMethod.MANUAL and mkey in manual_bad:
            basis = {}
            note = manual_bad[mkey]
        total_measure = sum(basis.values(), ZERO)
        if total_measure <= 0:
            unalloc += work_part
            no_hours_matters.add(mid)
            exc.append(ExceptionItem(
                Severity.WARNING, "Collections without qualifying hours",
                f"Collection {c['_ref']} ({method}): no eligible credited hours"
                f"{' - ' + note if note else ''}. Working credit of {work_part} is unallocated.",
                c["_ref"], mid, amount=work_part,
                resolution="Check time-entry import / invoice IDs, attribution, or enter manual "
                           "working percentages for the matter."))
            work_detail.append({**base, "Working portion": work_part, "Method": method,
                                "Partner": "(unallocated)", "Component": "-",
                                "Credited measure": 0.0, "Total credited measure": 0.0,
                                "Working share": 0.0, "Working credit": ZERO, "Note": note})
        else:
            keys = list(basis.keys())
            parts = allocate_cents(work_part, [basis[k] for k in keys])
            for (pname, bucket), amt in zip(keys, parts):
                credits[pname][bucket] += amt
                work_detail.append({**base, "Working portion": work_part, "Method": method,
                                    "Partner": pname, "Component": bucket,
                                    "Credited measure": float(basis[(pname, bucket)]),
                                    "Total credited measure": float(total_measure),
                                    "Working share": float(basis[(pname, bucket)] / total_measure),
                                    "Working credit": amt, "Note": note})
        unallocated_total += unalloc
        status_rows.append(_status_row(c, True, "", orig=orig_part if origs else ZERO,
                                       work=work_part if total_measure > 0 else ZERO,
                                       unallocated=unalloc, method=method))

    for mid in sorted(missing_orig_matters):
        amt = sum((c["_fee"] for c in included if clean_str(c.get("matter_id")) == mid), ZERO)
        exc.append(ExceptionItem(Severity.BLOCKING, "Missing originating partners",
                                 f"Matter {mid} has collections ({amt} fees) but no originating "
                                 "partner.", matter_id=mid, amount=amt,
                                 resolution="Firm Setup > Matter originators: assign the "
                                            "originator(s). The Responsible Professional is not "
                                            "assumed to be the originator."))

    _entry_exceptions(entries, issue_entries, flag_counts, matter_index, roster, exc)

    # ---------------------------------------------------------------- pools & compensation
    pools = _pools(policy)
    summary, ewyk_total_active = _partner_summary(partners, active, credits, pools, policy, exc)

    # ---------------------------------------------------------------- lockstep proposal
    shares = {r["Partner"]: Decimal(str(r["_share_exact"])) for r in summary
              if r["Active"]} if ewyk_total_active > 0 else {}
    lp_inputs = [LockstepPartner(p["name"], p["is_managing_partner"], p["lockstep_weight"],
                                 shares.get(p["name"]) if shares else None)
                 for p in partners if p["active"]]
    proposal = propose_lockstep(lp_inputs, policy.max_lockstep_change_pct,
                                policy.managing_partner_floor_pct)
    final_lockstep = _apply_overrides(proposal, lp_inputs, _records(inputs.lockstep_overrides),
                                      policy, exc)

    # ---------------------------------------------------------------- partner expenses & net
    attribution_df = _attribution_frame(entries)
    exp_result = allocate_expenses(policy, inputs.partners, inputs.expense_categories,
                                   inputs.category_splits, inputs.expenses,
                                   inputs.expense_overrides, inputs.carryforwards,
                                   pd.DataFrame(summary), attribution_df)
    exc.extend(exp_result.exceptions)
    apply_net(summary, exp_result, policy, exc)

    for r in summary:
        r.pop("_share_exact", None)
    summary_df = pd.DataFrame(summary)

    # ---------------------------------------------------------------- reconciliation
    total_fees_included = sum((c["_fee"] for c in included), ZERO)
    total_orig = sum((to_decimal(r["Origination credit"]) for r in orig_detail), ZERO)
    total_work = sum((to_decimal(r["Working credit"]) for r in work_detail), ZERO)
    recon = _reconciliation(policy, pools, summary_df, coll_rows, included, total_fees_included,
                            total_orig, total_work, unallocated_total, exp_result)
    for r in recon:
        if r["Status"] == "DIFFERENCE":
            exc.append(ExceptionItem(Severity.BLOCKING, "Compensation reconciliation differences",
                                     f"{r['Check']}: expected {r['Expected']}, actual "
                                     f"{r['Actual']} (difference {r['Difference']}).",
                                     resolution="Resolve the related blocking exceptions; the "
                                                "check passes when inputs are complete."))

    exceptions_df = pd.DataFrame([e.as_row() for e in exc], columns=list(
        ExceptionItem("", "", "").as_row().keys()))
    if not exceptions_df.empty:
        order = {Severity.BLOCKING: 0, Severity.WARNING: 1, Severity.INFO: 2}
        exceptions_df = exceptions_df.sort_values(
            by=["Severity", "Category"], key=lambda s: s.map(order) if s.name == "Severity" else s,
            kind="stable").reset_index(drop=True)

    unassigned_hours = sum((ae.hours * sum((a.pct for a in ae.allocations if a.partner is None), ZERO)
                            / HUNDRED for ae in entries if ae.unassigned), ZERO)
    metrics = {
        "distributable_pool": policy.distributable_pool,
        "total_collections": sum((to_decimal(c.get("amount_collected"), default=None)
                                  or to_decimal(c.get("fee_amount")) + to_decimal(c.get("expense_amount"))
                                  + to_decimal(c.get("tax_amount")) for c in coll_rows
                                  if not c.get("excluded")), ZERO),
        "total_fees_collected": total_fees_included,
        "equal_pool": pools["equal"], "ewyk_pool": pools["ewyk"], "lockstep_pool": pools["lockstep"],
        "active_partners": len(active),
        "unassigned_hours": unassigned_hours,
        "collections_missing_originator": sum(
            1 for c in included if clean_str(c.get("matter_id")) in missing_orig_matters),
        "matters_incomplete_supervision": len({norm_name(ae.matter_id) for ae in entries
                                               if ae.unassigned}),
        "fallback_collections": fallback_count,
        "unallocated_credit": unallocated_total,
        "blocking_exceptions": int((exceptions_df["Severity"] == Severity.BLOCKING).sum())
        if not exceptions_df.empty else 0,
        "reconciled": all(r["Status"] != "DIFFERENCE" for r in recon),
        "time_entries": len(entries),
        "total_expenses": exp_result.total_expenses,
        "total_net_payable": sum((to_decimal(v) for v in summary_df["Net payable"]), ZERO)
        if not summary_df.empty else ZERO,
        "total_carry_forward_out": sum((to_decimal(v) for v in summary_df[
            "Carry forward to next year"]), ZERO) if not summary_df.empty else ZERO,
        "collections_included": len(included),
        "collections_excluded": len(coll_rows) - len(included),
    }
    return CompensationResult(
        policy=policy, pools=pools, partner_summary=summary_df,
        origination_detail=pd.DataFrame(orig_detail, columns=[
            "Collection", "Collection date", "Client", "Matter ID", "Invoice ID", "Payment ID",
            "Fees collected", "Origination %", "Origination portion", "Partner",
            "Originator share %", "Origination credit"]),
        working_detail=pd.DataFrame(work_detail, columns=[
            "Collection", "Collection date", "Client", "Matter ID", "Invoice ID", "Payment ID",
            "Fees collected", "Working portion", "Method", "Partner", "Component",
            "Credited measure", "Total credited measure", "Working share", "Working credit", "Note"]),
        attribution_detail=attribution_df,
        collection_status=pd.DataFrame(status_rows),
        exceptions=exceptions_df,
        reconciliation=pd.DataFrame(recon),
        lockstep=proposal,
        final_lockstep=final_lockstep,
        metrics=metrics,
        expenses=exp_result,
    )


def _status_row(c: dict[str, Any], included: bool, reason: str, orig: Decimal = ZERO,
                work: Decimal = ZERO, unallocated: Decimal = ZERO, method: str = "") -> dict[str, Any]:
    return {
        "Row ID": c.get("row_id"), "Collection": c.get("_ref"),
        "Collection date": parse_date(c.get("collection_date")) if c.get("collection_date") else None,
        "Client": clean_str(c.get("client")), "Matter ID": clean_str(c.get("matter_id")),
        "Invoice ID": clean_str(c.get("invoice_id")), "Payment ID": clean_str(c.get("payment_id")),
        "Amount collected": to_decimal(c.get("amount_collected"), default=None),
        "Fees": to_decimal(c.get("fee_amount")), "Expenses": to_decimal(c.get("expense_amount")),
        "Taxes": to_decimal(c.get("tax_amount")),
        "Included": included, "Exclusion reason": reason, "Working method": method,
        "Origination credited": orig, "Working credited": work, "Unallocated": unallocated,
    }


def _working_basis(c: dict[str, Any], mkey: str, policy: Policy,
                   by_invoice: dict[tuple[str, str], list[AttributedEntry]],
                   by_matter: dict[str, list[AttributedEntry]],
                   invoice_matched: set[tuple[str, str]],
                   manual: dict[str, list[tuple[str, Decimal]]],
                   originator_names: set[str]) -> tuple[str, dict[tuple[str, str], Decimal], str]:
    """Return (method label, {(partner, bucket): credited measure}, note) for one collection."""
    if policy.working_method == WorkingMethod.MANUAL:
        shares = manual.get(mkey, [])
        basis: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for name, pct in shares:
            if not policy.originator_working_eligible and name in originator_names:
                continue
            basis[(name, BUCKET_MANUAL)] += pct
        return METHOD_MANUAL, dict(basis), "" if shares else "No manual working shares entered"
    inv_key = (mkey, norm_name(c.get("invoice_id")))
    if clean_str(c.get("invoice_id")) and inv_key in by_invoice:
        pool = by_invoice[inv_key]
        method = METHOD_INVOICE
    else:
        method = METHOD_MATTER
        pool = [ae for ae in by_matter.get(mkey, [])
                if ae.work_date is not None
                and policy.period_start <= ae.work_date <= policy.period_end
                and (not ae.invoice_id or (mkey, norm_name(ae.invoice_id)) not in invoice_matched)]
    basis = defaultdict(lambda: ZERO)
    unassigned = ZERO
    excluded_orig = ZERO
    for ae in pool:
        if ae.measure <= 0 or ae.excluded_reason:
            continue
        for a in ae.allocations:
            credited = ae.measure * a.pct / HUNDRED
            if a.partner is None:
                unassigned += credited
                continue
            if not policy.originator_working_eligible and a.partner in originator_names:
                excluded_orig += credited
                continue
            basis[(a.partner, ae.bucket)] += credited
    notes = []
    if unassigned > 0:
        notes.append(f"PROVISIONAL: {unassigned:.2f} unassigned measure excluded until resolved")
    if excluded_orig > 0:
        notes.append(f"{excluded_orig:.2f} originator measure excluded (originator not eligible)")
    return method, dict(basis), "; ".join(notes)


def _duplicate_checks(all_rows: list[dict[str, Any]], included: list[dict[str, Any]],
                      exc: list[ExceptionItem]) -> None:
    from .imports import collection_key

    for c in all_rows:
        if clean_str(c.get("duplicate_of")):
            reviewed = not c.get("excluded") and clean_str(c.get("exclusion_reason"))
            if c.get("excluded"):
                sev, state = Severity.WARNING, "excluded pending review"
            elif reviewed:
                sev, state = Severity.INFO, ("reviewed and INCLUDED as a separate payment: "
                                             + clean_str(c.get("exclusion_reason")))
            else:
                sev, state = Severity.BLOCKING, "INCLUDED without a review note - may double count"
            exc.append(ExceptionItem(
                sev, "Duplicate payments",
                f"Collection {c.get('_ref') or collection_key(c)} repeats another payment row "
                f"({state}).",
                c.get("_ref", ""), clean_str(c.get("matter_id")), amount=to_decimal(c.get("fee_amount")),
                resolution="Imports > Review collections: keep it excluded if it is a true "
                           "duplicate, or include it with a reason if it is a separate payment."))
    seen: dict[str, str] = {}
    for c in included:
        key = collection_key(c)
        if key in seen and not clean_str(c.get("duplicate_of")):
            exc.append(ExceptionItem(Severity.BLOCKING, "Duplicate payments",
                                     f"Collection {c['_ref']} has the same payment ID, invoice, "
                                     f"matter and amounts as {seen[key]}.", c["_ref"],
                                     clean_str(c.get("matter_id")),
                                     amount=to_decimal(c.get("fee_amount")),
                                     resolution="Exclude one of the rows on the Imports page."))
        seen.setdefault(key, c["_ref"])


def _entry_exceptions(entries: list[AttributedEntry], issue_entries: dict[str, list[AttributedEntry]],
                      flag_counts: dict[str, int], matter_index: dict[str, Any], roster: Roster,
                      exc: list[ExceptionItem]) -> None:
    def hours(items: list[AttributedEntry]) -> Decimal:
        return sum((a.hours for a in items), ZERO)

    grouped: dict[tuple[str, str], list[AttributedEntry]] = defaultdict(list)
    for ae in issue_entries.get("unassigned", []):
        if ae.measure > 0:
            grouped[(ae.timekeeper, ae.matter_id)].append(ae)
    for (tk, mid), items in grouped.items():
        exc.append(ExceptionItem(
            Severity.BLOCKING, "Unassigned associate hours",
            f"{hours(items)} hours by {tk} on matter {mid or '(none)'} ({len(items)} entries) "
            "have no supervising partner.", ", ".join(a.entry_id for a in items[:10]), mid,
            hours=hours(items),
            resolution="Supervisory Mappings: add an associate-matter mapping, set the matter's "
                       "compensation supervising partner, set a default supervisor, or expressly "
                       "exclude the entries."))
    for key, cat, label in (
        ("mapping_invalid", "Working allocations not totaling 100%", "associate-matter mapping"),
        ("override_invalid", "Working allocations not totaling 100%", "time-entry override"),
    ):
        items = [a for a in issue_entries.get(key, []) if a.measure > 0]
        by_note: dict[str, list[AttributedEntry]] = defaultdict(list)
        for a in items:
            by_note[f"{a.timekeeper} / {a.matter_id}: {a.allocations[0].note}"].append(a)
        for note, group in by_note.items():
            exc.append(ExceptionItem(Severity.BLOCKING, cat, f"Invalid {label} - {note}.",
                                     ", ".join(a.entry_id for a in group[:10]), group[0].matter_id,
                                     hours=hours(group),
                                     resolution="Supervisory Mappings: correct the percentages "
                                                "so each group totals 100%."))
    unknown = [a for a in issue_entries.get("unknown_timekeeper", [])]
    by_tk: dict[str, list[AttributedEntry]] = defaultdict(list)
    for a in unknown:
        by_tk[a.timekeeper].append(a)
    for tk, items in by_tk.items():
        qualifying = [a for a in items if a.measure > 0]
        exc.append(ExceptionItem(
            Severity.BLOCKING if qualifying else Severity.WARNING, "Unknown timekeepers",
            f"Timekeeper '{tk}' ({len(items)} entries, {hours(items)} hours) is not in the "
            "timekeeper list.", tk, hours=hours(items),
            resolution="Firm Setup > Timekeepers: add the person with the correct category."))
    for a in issue_entries.get("partner_not_on_roster", []):
        exc.append(ExceptionItem(Severity.BLOCKING, "Unknown timekeepers",
                                 f"Partner timekeeper '{a.timekeeper}' is not on the partner roster.",
                                 a.entry_id, a.matter_id, hours=a.hours,
                                 resolution="Set the timekeeper's linked partner or fix the roster."))
    orphan = [a for a in entries if a.matter_id and norm_name(a.matter_id) not in matter_index]
    by_m: dict[str, list[AttributedEntry]] = defaultdict(list)
    for a in orphan:
        by_m[a.matter_id].append(a)
    for mid, items in by_m.items():
        exc.append(ExceptionItem(Severity.WARNING, "Hours without corresponding matters",
                                 f"{hours(items)} hours ({len(items)} entries) are recorded to "
                                 f"matter {mid}, which is not in the matters list.",
                                 matter_id=mid, hours=hours(items),
                                 resolution="Import/add the matter, or correct the project ID."))
    no_matter = [a for a in entries if not a.matter_id]
    if no_matter:
        exc.append(ExceptionItem(Severity.WARNING, "Missing matter IDs",
                                 f"{len(no_matter)} time entries have no matter ID.",
                                 ", ".join(a.entry_id for a in no_matter[:10]),
                                 hours=hours(no_matter),
                                 resolution="Correct the time-entry export."))
    inactive: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for a in entries:
        for al in a.allocations:
            if al.partner and not roster.is_active(al.partner) and a.measure > 0:
                inactive[al.partner] += a.hours * al.pct / HUNDRED
    for name, hrs in inactive.items():
        exc.append(ExceptionItem(Severity.WARNING, "Credit to inactive partner",
                                 f"{hrs:.2f} credited hours are attributed to inactive partner "
                                 f"{name}; resulting working credit earns no compensation.",
                                 name, hours=hrs,
                                 resolution="Reassign the hours with a mapping/override if the "
                                            "firm's policy requires."))
    if flag_counts.get("hours_billed_missing"):
        exc.append(ExceptionItem(Severity.WARNING, "Data quality",
                                 f"{flag_counts['hours_billed_missing']} billed entries had no "
                                 "'hours billed' value; recorded hours were used.",
                                 resolution="Map the Hours Billed column in the import wizard."))
    if flag_counts.get("billable_assumed"):
        exc.append(ExceptionItem(Severity.INFO, "Data quality",
                                 f"{flag_counts['billable_assumed']} entries had no billable flag; "
                                 "billable status was inferred from the billed-status text.",
                                 resolution="Map the Billable column in the import wizard."))
    if flag_counts.get("billed_value_derived"):
        exc.append(ExceptionItem(Severity.INFO, "Data quality",
                                 f"{flag_counts['billed_value_derived']} entries had no billed "
                                 "value; hours x rate was used.",
                                 resolution="Map the Billed Amount column."))


def _pools(policy: Policy) -> dict[str, Decimal]:
    pool = money(policy.distributable_pool)
    pcts = [policy.equal_pct, policy.ewyk_pct, policy.lockstep_pct]
    if sum(pcts, ZERO) == HUNDRED:
        eq, ew, lk = allocate_cents(pool, pcts)
    else:
        eq, ew, lk = (money(pool * p / HUNDRED) for p in pcts)
    return {"distributable": pool, "equal": eq, "ewyk": ew, "lockstep": lk}


def _partner_summary(partners: list[dict[str, Any]], active: list[str],
                     credits: dict[str, dict[str, Decimal]], pools: dict[str, Decimal],
                     policy: Policy, exc: list[ExceptionItem]) -> tuple[list[dict[str, Any]], Decimal]:
    n = len(active)
    equal_parts = dict(zip(active, allocate_cents(pools["equal"], [Decimal(1)] * n))) if n else {}

    def total_ewyk(name: str) -> Decimal:
        c = credits.get(name, {})
        return sum(c.values(), ZERO)

    ewyk_active = {name: total_ewyk(name) for name in active}
    ewyk_total_active = sum(ewyk_active.values(), ZERO)
    if ewyk_total_active > 0:
        ewyk_parts = dict(zip(active, allocate_cents(pools["ewyk"], [ewyk_active[a] for a in active])))
    else:
        ewyk_parts = {a: ZERO for a in active}
        exc.append(ExceptionItem(Severity.BLOCKING, "No EWYK data",
                                 "No active partner has EWYK credit, so the EWYK pool cannot be "
                                 "allocated.", amount=pools["ewyk"],
                                 resolution="Import collections and time entries, and assign "
                                            "originators."))
    weights = {p["name"]: to_decimal(p["lockstep_weight"]) for p in partners if p["active"]}
    wsum = sum(weights.values(), ZERO)
    if wsum == HUNDRED:
        lock_parts = dict(zip(active, allocate_cents(pools["lockstep"], [weights[a] for a in active])))
    else:
        lock_parts = {a: money(pools["lockstep"] * weights[a] / HUNDRED) for a in active}

    rows = []
    for p in partners:
        name = p["name"]
        c = credits.get(name, {})
        orig = c.get("orig", ZERO)
        own = c.get(WorkingBucket.OWN, ZERO)
        assoc = c.get(WorkingBucket.ASSOCIATE, ZERO)
        deleg = c.get(WorkingBucket.DELEGATED, ZERO)
        manual = c.get(BUCKET_MANUAL, ZERO)
        work = own + assoc + deleg + manual
        is_active = p["active"]
        share = (orig + work) / ewyk_total_active * HUNDRED if is_active and ewyk_total_active > 0 \
            else ZERO
        eq = equal_parts.get(name, ZERO) if is_active else ZERO
        ew = ewyk_parts.get(name, ZERO) if is_active else ZERO
        lk = lock_parts.get(name, ZERO) if is_active else ZERO
        rows.append({
            "Partner": name, "Managing Partner": p["is_managing_partner"], "Active": is_active,
            "Originating credit": orig, "Own partner-hour working credit": own,
            "Supervised associate-hour working credit": assoc,
            "Other delegated-hour working credit": deleg,
            "Manual-allocation working credit": manual,
            "Total working credit": work, "Total EWYK credit": orig + work,
            "EWYK performance share %": float(share.quantize(Decimal("0.000001"))),
            "_share_exact": share,
            "Equal compensation": eq, "EWYK compensation": ew,
            "Current lockstep weight %": float(p["lockstep_weight"]),
            "Lockstep compensation": lk, "Total compensation": eq + ew + lk,
        })
    return rows, ewyk_total_active


def _apply_overrides(proposal: LockstepProposal, lp_inputs: list[LockstepPartner],
                     overrides: list[dict[str, Any]], policy: Policy,
                     exc: list[ExceptionItem]) -> pd.DataFrame:
    by_name = {norm_name(o.get("partner")): o for o in overrides}
    rows = []
    proposed = proposal.proposed
    for p in lp_inputs:
        o = by_name.get(norm_name(p.name))
        prop = proposed.get(p.name, to_decimal(p.current_weight))
        final = to_decimal(o.get("override_weight")) if o else prop
        rows.append({
            "Partner": p.name, "Managing Partner": p.is_managing_partner,
            "Current weight": float(p.current_weight), "Proposed weight": float(prop),
            "Final weight": float(final), "Final change": float(final - to_decimal(p.current_weight)),
            "Overridden": bool(o), "Override reason": clean_str(o.get("reason")) if o else "",
            "Override by": clean_str(o.get("entered_by")) if o else "",
            "Override at": clean_str(o.get("entered_at")) if o else "",
        })
    problems = check_final_weights(
        [{"name": r["Partner"], "is_managing_partner": r["Managing Partner"],
          "current": Decimal(str(r["Current weight"])), "final": Decimal(str(r["Final weight"]))}
         for r in rows], policy.max_lockstep_change_pct, policy.managing_partner_floor_pct)
    for prob in problems + [p for p in proposal.problems if "Current active" not in p]:
        exc.append(ExceptionItem(Severity.BLOCKING, "Lockstep proposal", prob,
                                 resolution="Lockstep Update: correct the overrides or roster."))
    return pd.DataFrame(rows)


def _reconciliation(policy: Policy, pools: dict[str, Decimal], summary: pd.DataFrame,
                    coll_rows: list[dict[str, Any]], included: list[dict[str, Any]],
                    fees: Decimal, orig: Decimal, work: Decimal,
                    unallocated: Decimal, exp: ExpenseResult | None = None) -> list[dict[str, Any]]:
    def row(check: str, expected: Decimal, actual: Decimal, tolerance: Decimal = ZERO,
            failure: str = "DIFFERENCE") -> dict[str, Any]:
        diff = actual - expected
        return {"Check": check, "Expected": expected, "Actual": actual, "Difference": diff,
                "Status": "OK" if abs(diff) <= tolerance else failure}

    def col(name: str, active_only: bool = False) -> Decimal:
        if summary.empty:
            return ZERO
        df = summary[summary["Active"]] if active_only else summary
        return sum((to_decimal(v) for v in df[name]), ZERO)

    out = [
        row("Policy pools total 100%", HUNDRED,
            policy.equal_pct + policy.ewyk_pct + policy.lockstep_pct),
        row("EWYK origination + working portions total 100%", HUNDRED,
            policy.origination_pct + policy.working_pct),
        row("Equal + EWYK + lockstep pools = distributable pool", pools["distributable"],
            pools["equal"] + pools["ewyk"] + pools["lockstep"]),
        row("Equal compensation paid = equal pool", pools["equal"], col("Equal compensation")),
        row("EWYK compensation paid = EWYK pool", pools["ewyk"], col("EWYK compensation")),
        row("Lockstep compensation paid = lockstep pool", pools["lockstep"],
            col("Lockstep compensation")),
        row("Total compensation = distributable pool", pools["distributable"],
            col("Total compensation")),
        row("Inactive partners receive no compensation", ZERO,
            col("Total compensation") - col("Total compensation", active_only=True)),
        row("Active lockstep weights total 100%", HUNDRED,
            sum((Decimal(str(w)) for w in summary[summary["Active"]]["Current lockstep weight %"]),
                ZERO) if not summary.empty else ZERO),
        row("Origination + working + unallocated credit = collected fees", fees,
            orig + work + unallocated),
        row("Unallocated EWYK credit (see exceptions)", ZERO, unallocated, failure="REVIEW"),
        row("Partner EWYK credits = origination + working credit", orig + work,
            col("Total EWYK credit")),
    ]
    if exp is not None:
        out += [
            row("Expenses allocated to partners = expenses entered", exp.total_expenses,
                exp.total_allocated),
            row("Gross - expenses - prior carry-forward = net compensation",
                col("Total compensation") - col("Allocated expenses")
                - col("Prior-year carry-forward"), col("Net compensation")),
            row("Net payable - shortfalls = net compensation", col("Net compensation"),
                col("Net payable") - col("Carry forward to next year") - col("Owed to the firm")),
        ]
    return out


def _attribution_frame(entries: list[AttributedEntry]) -> pd.DataFrame:
    rows = []
    for ae in entries:
        allocs = ae.allocations or [None]
        for a in allocs:
            pct = a.pct if a else HUNDRED
            rows.append({
                "Entry ID": ae.entry_id, "Work date": ae.work_date, "Matter ID": ae.matter_id,
                "Invoice ID": ae.invoice_id, "Timekeeper": ae.timekeeper, "Category": ae.category,
                "Component": ae.bucket, "Recorded hours": float(ae.hours),
                "Hours credited to partner": float(ae.hours * pct / HUNDRED),
                "Qualifying measure": float(ae.measure), "Measure basis": ae.measure_note,
                "Credited partner": (a.partner or "UNASSIGNED") if a else "",
                "Allocation %": float(pct),
                "Credited measure": float(ae.measure * pct / HUNDRED),
                "Attribution source": a.source if a else "",
                "Attribution note": a.note if a else "",
                "Excluded": ae.excluded_reason,
            })
    return pd.DataFrame(rows, columns=[
        "Entry ID", "Work date", "Matter ID", "Invoice ID", "Timekeeper", "Category", "Component",
        "Recorded hours", "Hours credited to partner", "Qualifying measure", "Measure basis",
        "Credited partner", "Allocation %", "Credited measure", "Attribution source",
        "Attribution note", "Excluded"])


def _policy_checks(policy: Policy, exc: list[ExceptionItem]) -> None:
    from .validation import validate_policy

    for prob in validate_policy(policy):
        exc.append(ExceptionItem(Severity.BLOCKING, "Policy inputs", prob,
                                 resolution="Firm Setup > Policy."))


def _roster_checks(partners: list[dict[str, Any]], policy: Policy, exc: list[ExceptionItem]) -> None:
    from .validation import validate_roster

    for prob in validate_roster(partners, policy):
        cat = "Active lockstep weights not totaling 100%" if "total" in prob.lower() \
            else "Partner roster"
        exc.append(ExceptionItem(Severity.BLOCKING, cat, prob,
                                 resolution="Firm Setup > Partner roster."))


__all__ = ["CompensationInputs", "CompensationResult", "calculate", "classify_collection",
           "METHOD_INVOICE", "METHOD_MATTER", "METHOD_MANUAL", "CENT", "date"]
