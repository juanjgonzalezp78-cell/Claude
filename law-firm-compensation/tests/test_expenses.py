"""Partner-expense allocation, net compensation and carry-forward tests."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from compensation.calculations import calculate
from compensation.demo import SAMPLE_DIR, load_demo
from compensation.expenses import ExpenseRule, NegativeNet
from compensation.imports import import_file
from compensation.models import Policy
from compensation.service import load_carryforwards, run
from conftest import PARTNERS, collection, entry, make_inputs, matter, roster


def D(x) -> Decimal:  # noqa: N802
    return Decimal(str(x))


def cats(*rows):
    return pd.DataFrame([{"category": c, "rule": r, "associate": a, "notes": ""}
                         for c, r, a in rows])


def splits(*rows):
    return pd.DataFrame([{"category": c, "partner": p, "share_pct": s, "notes": ""}
                         for c, p, s in rows])


def expenses(*rows):
    return pd.DataFrame([{"expense_id": f"X{i}", "expense_date": "2025-06-30", "category": c,
                          "description": "", "amount": a, "vendor": "", "reference": ""}
                         for i, (c, a) in enumerate(rows, start=1)])


def scenario(**kw):
    base = dict(entries=[entry("e1", "A", "M1", 10), entry("e2", "Smith", "M1", 30),
                         entry("e3", "B", "M1", 10)],
                collections=[collection("p1", "M1", 10000)], matters=[matter("M1")],
                originators=[("M1", "MP", 100)])
    base.update(kw)
    return calculate(make_inputs(**base))


def charges(res) -> dict[str, Decimal]:
    return {r["Partner"]: r["Allocated expenses"] for r in res.partner_summary.to_dict("records")}


def test_direct_and_fixed_split():
    res = scenario(expense_categories=cats(("Salary Smith", ExpenseRule.DIRECT, ""),
                                           ("Salary Jones", ExpenseRule.FIXED, "")),
                   category_splits=splits(("Salary Smith", "A", 100), ("Salary Jones", "B", 60),
                                          ("Salary Jones", "C", 40)),
                   expenses=expenses(("Salary Smith", 1000), ("Salary Jones", 1000.01)))
    c = charges(res)
    assert c["A"] == D("1000.00")
    assert c["B"] == D("600.01") and c["C"] == D("400.00")
    assert res.expenses.total_allocated == res.expenses.total_expenses == D("2000.01")


def test_equal_split_is_prorated_by_time_as_partner():
    r = roster()
    r["start_date"] = None
    r["end_date"] = None
    r.loc[r["name"] == "J", "start_date"] = "2025-07-02"  # second half of the year: 183/365
    res = scenario(partners=r, expense_categories=cats(("Rent", ExpenseRule.EQUAL, "")),
                   expenses=expenses(("Rent", 100000)))
    c = charges(res)
    full = D(365)
    total_w = 10 * full + D(183)
    assert abs(c["J"] - D(100000) * D(183) / total_w) <= D("0.01")
    assert abs(c["A"] - D(100000) * full / total_w) <= D("0.01")
    assert sum(c.values()) == D("100000.00")
    # proration off -> equal shares
    pol = Policy(year=2025, distributable_pool=1_000_000, prorate_equal_expenses=False)
    c2 = charges(scenario(partners=r, policy=pol,
                          expense_categories=cats(("Rent", ExpenseRule.EQUAL, "")),
                          expenses=expenses(("Rent", 110000))))
    assert c2["J"] == c2["A"] == D("10000.00")


def test_inactive_partners_excluded_from_equal_split():
    r = roster(inactive=("J",))
    r.loc[r["name"] == "A", "lockstep_weight"] = 16.4
    c = charges(scenario(partners=r, expense_categories=cats(("Utilities", ExpenseRule.EQUAL, "")),
                         expenses=expenses(("Utilities", 1000))))
    assert c["J"] == 0 and c["A"] == D("100.00")


def test_by_associate_hours_follows_attribution():
    sup = [{"timekeeper": "Jones", "matter_id": "M1", "partner": "C", "start_date": None,
            "end_date": None, "allocation_pct": 75, "notes": "", "source": ""},
           {"timekeeper": "Jones", "matter_id": "M1", "partner": "D", "start_date": None,
            "end_date": None, "allocation_pct": 25, "notes": "", "source": ""}]
    res = scenario(entries=[entry("e1", "A", "M1", 10), entry("j1", "Jones", "M1", 8)],
                   supervision=sup,
                   expense_categories=cats(("Salary Jones", ExpenseRule.ASSOCIATE_HOURS, "Jones")),
                   expenses=expenses(("Salary Jones", 8000)))
    c = charges(res)
    assert c["C"] == D("6000.00") and c["D"] == D("2000.00")


def test_proportional_to_gross_and_ewyk():
    res = scenario(expense_categories=cats(("Insurance", ExpenseRule.GROSS, ""),
                                           ("Marketing", ExpenseRule.EWYK, "")),
                   expenses=expenses(("Insurance", 10000), ("Marketing", 700)))
    det = res.expenses.detail
    ins = det[det["Category"] == "Insurance"].set_index("Partner")["Allocated amount"]
    gross = res.partner_summary.set_index("Partner")["Total compensation"]
    assert abs(ins["MP"] - D(10000) * gross["MP"] / gross.sum()) <= D("0.01")
    mk = det[det["Category"] == "Marketing"].set_index("Partner")["Allocated amount"]
    # EWYK credit: MP 3,000 origination; A and B share 7,000 working by credited hours
    assert mk["MP"] == D("210.00")


def test_entry_override_and_unallocated_expense_is_blocking():
    res = scenario(expense_categories=cats(("Dues", ExpenseRule.FIXED, "")),
                   category_splits=splits(("Dues", "A", 100)),
                   expenses=expenses(("Dues", 500), ("Undefined", 300)),
                   expense_overrides=pd.DataFrame([{"expense_id": "X1", "partner": "E",
                                                    "share_pct": 100, "reason": "personal",
                                                    "entered_by": "t", "entered_at": ""}]))
    c = charges(res)
    assert c["E"] == D("500.00") and c["A"] == 0
    ex = res.exceptions
    assert ((ex["Category"] == "Expenses not allocated") & (ex["Severity"] == "Blocking")).any()
    assert res.expenses.total_expenses - res.expenses.total_allocated == D("300.00")
    assert not res.reconciled


def test_negative_net_carry_forward_and_owed():
    big = expenses(("Salary", 2_000_000))
    common = dict(expense_categories=cats(("Salary", ExpenseRule.DIRECT, "")),
                  category_splits=splits(("Salary", "A", 100)), expenses=big)
    res = scenario(**common)
    a = res.partner_summary.set_index("Partner").loc["A"]
    assert a["Net compensation"] < 0 and a["Net payable"] == 0
    assert a["Carry forward to next year"] == -a["Net compensation"]
    assert a["Owed to the firm"] == 0
    assert res.reconciled
    pol = Policy(year=2025, distributable_pool=1_000_000, negative_net_treatment=NegativeNet.OWED_NOW)
    a2 = scenario(policy=pol, **common).partner_summary.set_index("Partner").loc["A"]
    assert a2["Owed to the firm"] == -a2["Net compensation"] and a2["Carry forward to next year"] == 0


def test_carry_forward_is_deducted_next_year(db):
    load_demo(db, 2025, "tester")
    db.save_table("expenses", 2025, pd.concat([db.load_table("expenses", 2025), pd.DataFrame([{
        "expense_id": "BIG", "expense_date": "2025-12-31", "category": "Associate salary - Daniel Smith",
        "description": "test", "amount": 2_000_000, "vendor": "", "reference": ""}])]), "tester")
    _, r25 = run(db, 2025)
    short = r25.partner_summary.set_index("Partner").loc["David Okafor", "Carry forward to next year"]
    assert short > 0
    load_demo(db, 2026, "tester")  # a second year with the same demo activity
    notes = load_carryforwards(db, 2025, 2026, "tester")
    assert "loaded" in notes[0]
    _, r26 = run(db, 2026)
    okafor = r26.partner_summary.set_index("Partner").loc["David Okafor"]
    assert okafor["Prior-year carry-forward"] == short
    assert okafor["Net compensation"] == (okafor["Total compensation"] - okafor["Allocated expenses"]
                                          - short)


def test_expense_import_maps_accounting_export_and_skips_duplicates(db):
    path = str(SAMPLE_DIR / "partner_expenses.csv")
    first = import_file(db, 2025, "expenses", path, "tester")
    assert first.imported == len(pd.read_csv(path)) and first.imported > 100
    again = import_file(db, 2025, "expenses", path, "tester")
    assert again.imported == 0 and again.duplicates == first.imported
    stored = db.load_table("expenses", 2025)
    assert set(stored["category"]) >= {"Rent", "Utilities"}
    assert len(PARTNERS) == 11
