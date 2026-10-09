"""Work out the professional-fee portion of payment allocations.

TimeSolv's 'Payment & Allocation' export gives, per row, the amount of a payment
allocated to an invoice but not how much of it paid fees versus expenses or
tax.  Using the invoice breakdown from the 'Invoice' export, the fee portion is
derived by one of two policy-selected methods:

* **TimeSolv order** (default): TimeSolv applies payments to an invoice's tax,
  expenses and interest before fees.  Allocations to the same invoice are
  replayed in date order; each one first absorbs whatever non-fee balance
  remains, and the rest is fees (never more than the invoice's fees).
* **Pro rata**: each allocation counts as fees in proportion to
  ``fees / (fees + expenses + taxes + interest)`` on the invoice.

Rows that already carry a fee amount (other export formats) are left as they are.
Rows whose fee portion cannot be determined are marked with ``_fee_error`` and
reported as blocking exceptions - they are never silently treated as all-fee.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .models import (ZERO, ExceptionItem, FeeSplit, Policy, Severity, clean_str, money,
                     norm_name, parse_date, to_decimal)

VOID_WORDS = ("void", "bounced", "nsf", "reversed", "reversal", "failed", "declined", "refund")


def _inv_parts(inv: dict[str, Any]) -> tuple[Decimal | None, Decimal]:
    """(fees, non-fee charges) for an invoice record.

    TimeSolv's Invoice export gives the invoice amount and the time (fee) and expense totals.
    Any excess of the invoice amount over time + expenses (tax, interest, other charges) is
    treated as a non-fee charge; a shortfall (a discount) reduces the fees.
    """
    nonfee = sum((to_decimal(inv.get(k)) for k in ("expenses", "taxes", "taxes2", "interest")),
                 ZERO)
    fees = to_decimal(inv.get("fees"), default=None)
    total = to_decimal(inv.get("total"), default=None)
    if fees is None:
        return (total - nonfee if total is not None else None), nonfee
    if total is not None:
        other = total - fees - nonfee
        if other > 0:
            nonfee += other
        elif other < 0:
            fees = max(ZERO, fees + other)
    return fees, nonfee


def derive_fee_portions(rows: list[dict[str, Any]], invoices: list[dict[str, Any]],
                        policy: Policy, exc: list[ExceptionItem]) -> None:
    """Fill ``fee_amount`` (and ``_nonfee``/``_fee_source``) on rows lacking it (in place)."""
    inv_index = {norm_name(i.get("invoice_id")): i for i in invoices if clean_str(i.get("invoice_id"))}
    for c in rows:
        c["_fee_source"] = "From export" if to_decimal(c.get("fee_amount"), default=None) \
            is not None else ""

    def participates(c: dict[str, Any]) -> bool:
        text = " ".join(norm_name(c.get(k)) for k in ("transaction_type", "payment_status",
                                                       "credit_type", "payment_type"))
        return (not c.get("excluded") and to_decimal(c.get("allocated_amount")) > 0
                and clean_str(c.get("invoice_id")) != "" and not any(w in text for w in VOID_WORDS))

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in rows:
        if participates(c):
            groups[norm_name(c.get("invoice_id"))].append(c)

    missing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, allocs in groups.items():
        needs = [c for c in allocs if not c["_fee_source"]]
        if not needs:
            continue
        inv = inv_index.get(key)
        if inv is None:
            missing[clean_str(allocs[0].get("invoice_id"))].extend(needs)
            continue
        fees, nonfee = _inv_parts(inv)
        if fees is None:
            for c in needs:
                c["_fee_error"] = f"invoice {clean_str(inv.get('invoice_id'))} has no fee amount"
            continue
        allocs.sort(key=lambda c: (parse_date(c.get("collection_date")) or parse_date("1900-01-01"),
                                   c.get("row_id") or 0))
        cum = ZERO
        for c in allocs:
            a = to_decimal(c.get("allocated_amount"))
            if policy.fee_split_method == FeeSplit.PRO_RATA:
                denom = fees + nonfee
                fee = money(a * fees / denom) if denom > 0 else ZERO
                fee = min(fee, a)
                source = "Derived: pro rata to invoice fees"
            else:
                before = min(fees, max(ZERO, cum - nonfee))
                after = min(fees, max(ZERO, cum + a - nonfee))
                fee = money(after - before)
                source = "Derived: TimeSolv order (non-fee charges first)"
            cum += a
            if not c["_fee_source"]:
                c["fee_amount"] = fee
                c["_nonfee"] = a - fee
                c["_fee_source"] = source

    for inv_id, needs in missing.items():
        for c in needs:
            c["_fee_error"] = f"invoice {inv_id} is not in the Invoice export"
        amt = sum((to_decimal(c.get("allocated_amount")) for c in needs), ZERO)
        exc.append(ExceptionItem(
            Severity.BLOCKING, "Fee portion unknown",
            f"Invoice {inv_id} received {len(needs)} payment allocation(s) totalling {amt}, but it "
            "is not in the Invoice export, so the fee portion cannot be worked out.", inv_id,
            clean_str(needs[0].get("matter_id")), amount=amt,
            resolution="Import TimeSolv's Invoice export covering this invoice (Imports page), "
                       "or exclude the payment with a reason."))
