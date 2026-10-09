"""Tests for TimeSolv's real export layouts (Payment & Allocation, Time, Invoice, Matter,
Matter Originating Professional)."""

from __future__ import annotations

import io
from decimal import Decimal

import pandas as pd

from compensation.calculations import calculate
from compensation.demo import SAMPLE_DIR, load_demo
from compensation.imports import import_dataframe, import_file, read_upload, suggest_mapping
from compensation.models import FeeSplit, Policy
from compensation.service import run
from conftest import entry, make_inputs, matter

PAY_HEAD = ("Transaction Type,Transaction Date,Client Name,Client Id,Project Name,Project Id,"
            "Credit Type,Payment Method,Invoice Number,Payment Amount,Available Funds,"
            "Allocated Amount,Payment Account Group,Client Type\n")


def D(x) -> Decimal:  # noqa: N802
    return Decimal(str(x))


def alloc(day: str, inv: str, amount: float, payment: float | None = None, **kw):
    row = {"row_id": hash((day, inv, amount)) % 100000, "collection_date": day, "client": "",
           "matter_id": "M1", "invoice_id": inv, "payment_id": "",
           "amount_collected": payment if payment is not None else amount,
           "allocated_amount": amount, "available_funds": 0, "fee_amount": None,
           "expense_amount": None, "tax_amount": None, "transaction_type": "Payment",
           "credit_type": "", "payment_method": "Check", "account_group": "Operating",
           "payment_type": "", "payment_status": "", "excluded": False, "exclusion_reason": "",
           "duplicate_of": "", "dedup_key": f"{day}{inv}{amount}"}
    row.update(kw)
    return row


def invoice(inv: str, fees: float, expenses: float, total: float):
    return {"invoice_id": inv, "fees": fees, "expenses": expenses, "taxes": None, "taxes2": None,
            "interest": None, "total": total, "matter_id": "M1", "dedup_key": inv}


def fees_by_row(res) -> list[Decimal]:
    cs = res.collection_status
    return [D(v) for v in cs.sort_values("Collection date")["Fees"]]


def scenario(collections, invoices, policy=None):
    return calculate(make_inputs(
        entries=[entry("e1", "A", "M1", 10, invoice="INV1")], collections=collections,
        matters=[matter("M1")], originators=[("M1", "MP", 100)], policy=policy,
        invoices=pd.DataFrame(invoices)))


def test_timesolv_order_pays_expenses_and_tax_before_fees():
    # invoice: fees 9,000, expenses 600, invoice amount 10,000 => 400 tax/other
    res = scenario([alloc("2025-03-01", "INV1", 4000), alloc("2025-04-01", "INV1", 6000)],
                   [invoice("INV1", 9000, 600, 10000)])
    assert fees_by_row(res) == [D("3000.00"), D("6000.00")]
    assert res.metrics["total_fees_collected"] == D("9000.00")
    assert res.reconciled


def test_pro_rata_method():
    pol = Policy(year=2025, distributable_pool=1_000_000, fee_split_method=FeeSplit.PRO_RATA)
    res = scenario([alloc("2025-03-01", "INV1", 5000)], [invoice("INV1", 9000, 600, 10000)],
                   policy=pol)
    assert fees_by_row(res) == [D("4500.00")]


def test_prior_period_payment_is_applied_first_but_not_credited():
    res = scenario([alloc("2024-12-15", "INV1", 700), alloc("2025-02-01", "INV1", 9300)],
                   [invoice("INV1", 9000, 1000, 10000)])
    cs = res.collection_status.set_index("Collection date")
    # December payment cleared 700 of the 1,000 expenses; February pays 300 expenses + 9,000 fees
    assert res.metrics["total_fees_collected"] == D("9000.00")
    assert len(res.collection_status[res.collection_status["Included"]]) == 1


def test_discount_reduces_fees():
    res = scenario([alloc("2025-03-01", "INV1", 9100)], [invoice("INV1", 9000, 600, 9100)])
    assert fees_by_row(res) == [D("8500.00")]


def test_missing_invoice_blocks_and_is_not_credited():
    res = scenario([alloc("2025-03-01", "INV9", 5000)], [invoice("INV1", 9000, 600, 10000)])
    ex = res.exceptions
    assert ((ex["Category"] == "Fee portion unknown") & (ex["Severity"] == "Blocking")).any()
    assert res.metrics["total_fees_collected"] == 0


def test_unapplied_void_and_credit_rows_excluded():
    rows = [alloc("2025-02-01", "", 0, payment=25000, available_funds=25000,
                  account_group="Trust"),
            alloc("2025-03-01", "INV1", 1000, transaction_type="Void"),
            alloc("2025-03-02", "INV1", 500, transaction_type="Credit", credit_type="Write Off"),
            alloc("2025-03-03", "INV1", 2000)]
    res = scenario(rows, [invoice("INV1", 9000, 600, 10000)])
    st = res.collection_status
    assert int(st["Included"].sum()) == 1
    # the write-off credit consumed 500 of the 1,000 non-fee charges; the 2,000 payment pays
    # the remaining 500 non-fee and 1,500 fees
    assert res.metrics["total_fees_collected"] == D("1500.00")


def test_repeated_payment_amount_is_not_double_counted(db):
    csv = (PAY_HEAD +
           "Payment,05/01/2025,Acme,C1,Lease,M1,,Check,INV1,15000.00,0.00,10000.00,Operating,Business\n"
           "Payment,05/01/2025,Acme,C1,Lease,M1,,Check,INV2,15000.00,0.00,5000.00,Operating,Business\n"
           ).encode()
    raw = read_upload("p.csv", csv)
    mapping = suggest_mapping("collections", list(raw.columns))
    assert mapping["allocated_amount"] == "Allocated Amount"
    assert mapping["payment_type"] == ""
    res = import_dataframe(db, 2025, "collections", raw, mapping, "p.csv", csv, "t")
    assert res.imported == 2 and res.flagged_duplicates == 0
    db.save_table("partners", 2025, pd.DataFrame([{"name": "A", "is_managing_partner": True,
                                                   "lockstep_weight": 100, "active": True}]), "t")
    _, result = run(db, 2025)
    assert result.metrics["total_collections"] == D("15000.00")


def test_time_export_layout_and_billing_rules(db):
    csv = ("Time Entry Status,Date,Firm User,Client Id,Client Name,Project Id,Project Name,"
           "Plan Task,Plan Task Completed?,Task Code,Task Description,Sub-Task Code,"
           "Sub Task Description,Hours,Rate,Amount,Notes,Invoice Number,Client Category,Custom,"
           "Billable Type\n"
           "Approved,03/01/2025,Jane Roe,C1,Acme,M1,Lease,,,A101,Plan,,,1.5,300,450,Email,INV1,,,Billable\n"
           "Approved,03/01/2025,Jane Roe,C1,Acme,M1,Lease,,,A101,Plan,,,1.5,300,450,Email,INV1,,,Billable\n"
           "Unbilled,03/02/2025,Jane Roe,C1,Acme,M1,Lease,,,A101,Plan,,,2,300,600,Call,,,,No Charge\n"
           ).encode()
    raw = read_upload("t.csv", csv)
    mapping = suggest_mapping("time_entries", list(raw.columns))
    assert mapping["timekeeper"] == "Firm User" and mapping["billable"] == "Billable Type"
    assert mapping["billed_status"] == "Time Entry Status"
    res = import_dataframe(db, 2025, "time_entries", raw, mapping, "t.csv", csv, "t")
    assert res.imported == 3  # identical rows without IDs are kept as separate entries
    again = import_dataframe(db, 2025, "time_entries", raw, mapping, "t.csv", csv, "t")
    assert again.imported == 0 and again.duplicates == 3
    te = db.load_data("time_entries", 2025)
    assert te["billable"].tolist() == [True, True, False]


def test_originator_import_matches_names_and_handles_zero_percent(db):
    load_demo(db, 2025, "tester")
    o = db.load_table("originators", 2025)
    m1003 = o[o["matter_id"] == "M-1003"].set_index("partner")["share_pct"]
    assert m1003["Margaret Chen"] == 60 and m1003["Robert Alvarez"] == 40
    single = o[o["matter_id"] == "M-1001"]
    assert single["share_pct"].tolist() == [100]
    split = o[o["matter_id"] == "M-1010"]["share_pct"].tolist()
    assert sorted(split) == [50, 50]
    _, res = run(db, 2025)
    assert (res.exceptions["Category"] == "Originator split assumed").any()


def test_demo_uses_derived_fees_and_reconciles(db):
    load_demo(db, 2025, "tester")
    _, res = run(db, 2025)
    cs = res.collection_status
    inc = cs[cs["Included"]]
    assert (inc["Fee source"].str.startswith("Derived")).all()
    assert res.reconciled
    for name in ("timesolv_invoices.csv", "timesolv_payment_allocation.csv", "timesolv_time.csv"):
        assert (SAMPLE_DIR / name).exists()
    assert import_file  # imported helper used by the demo loader
    assert io
