"""Engine tests: pools, credits, attribution hierarchy, originator eligibility, inactive partners."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from compensation.calculations import METHOD_INVOICE, METHOD_MATTER, calculate
from compensation.models import AttributionSource, Policy, allocate_cents
from conftest import collection, entry, make_inputs, matter, roster


def D(x) -> Decimal:  # noqa: N802
    return Decimal(str(x))


def summary(result) -> dict[str, dict]:
    return {r["Partner"]: r for r in result.partner_summary.to_dict("records")}


def basic_inputs(**kw):
    return make_inputs(
        entries=[entry("e1", "A", "M1", 10), entry("e2", "Smith", "M1", 30),
                 entry("e3", "B", "M2", 5, invoice="INV2"), entry("e4", "C", "M2", 15, invoice="INV2")],
        collections=[collection("p1", "M1", 10000.00, expense=500.00),
                     collection("p2", "M2", 7333.33, invoice="INV2")],
        matters=[matter("M1"), matter("M2")],
        originators=[("M1", "MP", 100), ("M2", "B", 100)],
        **kw,
    )


# 1 ------------------------------------------------------------------------------
def test_three_pools_total_distributable_pool():
    policy = Policy(year=2025, distributable_pool=D("1000000.01"))
    res = calculate(basic_inputs(policy=policy))
    pools = res.pools
    assert pools["equal"] + pools["ewyk"] == D("1000000.01")
    assert pools["equal"] == D("200000.00") and pools["ewyk"] == D("800000.01")
    s = res.partner_summary
    assert sum(s["Equal compensation"]) == pools["equal"]
    assert sum(s["EWYK compensation"]) == pools["ewyk"]
    assert "Lockstep compensation" not in s.columns
    assert sum(s["Total compensation"]) == D("1000000.01")
    assert res.reconciled


def test_equal_and_ewyk_formulas():
    res = calculate(basic_inputs())
    s = summary(res)
    # equal = 1,000,000 x 20% / 11 (cent allocation keeps the exact total)
    assert abs(s["A"]["Equal compensation"] - D("200000") / 11) < D("0.01")
    # EWYK = 1,000,000 x 80% x share; total credit 17,333.33, A has 7,000.00
    assert abs(s["A"]["EWYK compensation"] - D("800000") * D("7000.00") / D("17333.33")) < D("0.01")
    assert sum(r["Total compensation"] for r in s.values()) == D("1000000.00")
    assert s["MP"]["Total compensation"] == s["MP"]["Equal compensation"] + s["MP"]["EWYK compensation"]


# 2 ------------------------------------------------------------------------------
def test_origination_and_working_credits_total_collected_fees():
    res = calculate(basic_inputs())
    orig = sum(res.origination_detail["Origination credit"])
    work = sum(res.working_detail["Working credit"])
    assert orig + work == D("10000.00") + D("7333.33")  # expenses never earn credit
    assert orig == D("3000.00") + D("2200.00")  # 30% (7333.33 x 0.3 = 2199.999 -> 2200.00)
    s = summary(res)
    assert sum(r["Total EWYK credit"] for r in s.values()) == D("17333.33")


def test_working_credit_split_by_hours():
    res = calculate(basic_inputs())
    s = summary(res)
    # M1: 7,000 working; A 10h own, Smith 30h -> default supervisor A => A gets all 7,000
    assert s["A"]["Own partner-hour working credit"] == D("1750.00")
    assert s["A"]["Supervised associate-hour working credit"] == D("5250.00")
    # M2: 5,133.33 working split B 5h / C 15h
    assert s["B"]["Own partner-hour working credit"] + s["C"]["Own partner-hour working credit"] \
        == D("5133.33")
    assert s["C"]["Own partner-hour working credit"] == D("3850.00")


# 3 ------------------------------------------------------------------------------
def test_matter_specific_assignment_overrides_default_supervisor():
    sup = [{"timekeeper": "Smith", "matter_id": "M1", "partner": "D", "start_date": None,
            "end_date": None, "allocation_pct": 100, "notes": "", "source": "memo"}]
    res = calculate(basic_inputs(supervision=sup))
    att = res.attribution_detail
    smith = att[att["Entry ID"] == "e2"].iloc[0]
    assert smith["Credited partner"] == "D"
    assert smith["Attribution source"] == AttributionSource.MATTER_MAPPING
    s = summary(res)
    assert s["D"]["Supervised associate-hour working credit"] == D("5250.00")
    assert s["A"]["Supervised associate-hour working credit"] == 0


def test_attribution_hierarchy_levels():
    entries = [entry("x1", "Jones", "M3", 1), entry("x2", "Jones", "M4", 1),
               entry("x3", "Smith", "M5", 1), entry("x4", "Jones", "M5", 1)]
    inputs = make_inputs(
        entries=entries, collections=[collection("p", "M3", 100)],
        matters=[matter("M3", comp="E"), matter("M4", responsible="F"),
                 matter("M5", responsible="Smith")],  # associate as responsible -> skipped
        originators=[("M3", "MP", 100), ("M4", "MP", 100), ("M5", "MP", 100)],
        entry_overrides=pd.DataFrame([{"entry_id": "x4", "partner": "G", "allocation_pct": 100,
                                       "reason": "special", "entered_by": "t", "entered_at": ""}]))
    att = calculate(inputs).attribution_detail.set_index("Entry ID")
    assert att.loc["x1", "Attribution source"] == AttributionSource.COMP_SUPERVISOR
    assert att.loc["x1", "Credited partner"] == "E"
    assert att.loc["x2", "Attribution source"] == AttributionSource.RESPONSIBLE
    assert att.loc["x3", "Attribution source"] == AttributionSource.DEFAULT_SUPERVISOR
    assert att.loc["x3", "Credited partner"] == "A"
    assert att.loc["x4", "Attribution source"] == AttributionSource.ENTRY_OVERRIDE
    assert att.loc["x4", "Credited partner"] == "G"


def test_effective_dated_mappings():
    sup = [{"timekeeper": "Smith", "matter_id": "M1", "partner": "B", "start_date": "2025-01-01",
            "end_date": "2025-06-30", "allocation_pct": 100, "notes": "", "source": ""},
           {"timekeeper": "Smith", "matter_id": "M1", "partner": "C", "start_date": "2025-07-01",
            "end_date": None, "allocation_pct": 100, "notes": "", "source": ""}]
    inputs = make_inputs(entries=[entry("h1", "Smith", "M1", 2, day="2025-03-01"),
                                  entry("h2", "Smith", "M1", 2, day="2025-09-01")],
                         collections=[collection("p", "M1", 1000)], matters=[matter("M1")],
                         originators=[("M1", "MP", 100)], supervision=sup)
    att = calculate(inputs).attribution_detail.set_index("Entry ID")
    assert att.loc["h1", "Credited partner"] == "B"
    assert att.loc["h2", "Credited partner"] == "C"


# 4 ------------------------------------------------------------------------------
def test_split_supervision_divides_associate_hours():
    sup = [{"timekeeper": "Jones", "matter_id": "M7", "partner": "C", "start_date": None,
            "end_date": None, "allocation_pct": 60, "notes": "", "source": ""},
           {"timekeeper": "Jones", "matter_id": "M7", "partner": "D", "start_date": None,
            "end_date": None, "allocation_pct": 40, "notes": "", "source": ""}]
    inputs = make_inputs(entries=[entry("j1", "Jones", "M7", 10)],
                         collections=[collection("p", "M7", 10000)], matters=[matter("M7")],
                         originators=[("M7", "MP", 100)], supervision=sup)
    res = calculate(inputs)
    att = res.attribution_detail
    hours = dict(zip(att["Credited partner"], att["Hours credited to partner"]))
    assert hours == {"C": 6.0, "D": 4.0}
    s = summary(res)
    assert s["C"]["Supervised associate-hour working credit"] == D("4200.00")  # 7,000 x 60%
    assert s["D"]["Supervised associate-hour working credit"] == D("2800.00")


def test_split_not_totaling_100_is_blocking():
    sup = [{"timekeeper": "Jones", "matter_id": "M7", "partner": "C", "start_date": None,
            "end_date": None, "allocation_pct": 60, "notes": "", "source": ""}]
    inputs = make_inputs(entries=[entry("j1", "Jones", "M7", 10)],
                         collections=[collection("p", "M7", 1000)], matters=[matter("M7")],
                         originators=[("M7", "MP", 100)], supervision=sup)
    ex = calculate(inputs).exceptions
    assert ((ex["Category"] == "Working allocations not totaling 100%")
            & (ex["Severity"] == "Blocking")).any()


# 5 ------------------------------------------------------------------------------
def test_originator_receives_origination_and_working_credit_when_eligible():
    res = calculate(basic_inputs())  # MP originates M1 but has no hours on M1
    s = summary(res)
    assert s["MP"]["Originating credit"] == D("3000.00")
    assert s["MP"]["Total working credit"] == 0  # origination alone gives no working credit
    # B originates M2 AND worked 5 of 20 hours there -> both credits
    assert s["B"]["Originating credit"] == D("2200.00")
    assert s["B"]["Own partner-hour working credit"] == D("1283.33")
    assert s["B"]["Total EWYK credit"] == D("3483.33")


# 6 ------------------------------------------------------------------------------
def test_disabling_originator_eligibility_reallocates_working_credit():
    policy = Policy(year=2025, distributable_pool=1_000_000, originator_working_eligible=False)
    res = calculate(basic_inputs(policy=policy))
    s = summary(res)
    assert s["B"]["Originating credit"] == D("2200.00")
    assert s["B"]["Total working credit"] == 0
    assert s["C"]["Own partner-hour working credit"] == D("5133.33")  # all of M2 working credit
    note = res.working_detail[res.working_detail["Matter ID"] == "M2"]["Note"].iloc[0]
    assert "originator measure excluded" in note
    assert sum(res.working_detail["Working credit"]) + sum(res.origination_detail[
        "Origination credit"]) == D("17333.33")


# 7 (engine-level duplicate detection; import-level test lives in test_imports.py) --------
def test_included_duplicate_payment_is_blocking():
    dup = collection("p1", "M1", 10000.00, expense=500.00)
    res = calculate(make_inputs(entries=[entry("e1", "A", "M1", 1)],
                                collections=[collection("p1", "M1", 10000.00, expense=500.00), dup],
                                matters=[matter("M1")], originators=[("M1", "MP", 100)]))
    ex = res.exceptions
    assert ((ex["Category"] == "Duplicate payments") & (ex["Severity"] == "Blocking")).any()


# 8 ------------------------------------------------------------------------------
def test_unassigned_associate_hours_are_flagged_not_allocated():
    inputs = make_inputs(entries=[entry("u1", "Jones", "M9", 8), entry("u2", "A", "M9", 2)],
                         collections=[collection("p", "M9", 1000)], matters=[matter("M9")],
                         originators=[("M9", "MP", 100)])
    res = calculate(inputs)
    ex = res.exceptions
    flagged = ex[ex["Category"] == "Unassigned associate hours"]
    assert len(flagged) == 1 and flagged.iloc[0]["Severity"] == "Blocking"
    assert flagged.iloc[0]["Hours / Measure"] == 8.0
    att = res.attribution_detail.set_index("Entry ID")
    assert att.loc["u1", "Credited partner"] == "UNASSIGNED"
    # Jones' hours are NOT silently given to anyone: A's 2 hours earn all working credit
    s = summary(res)
    assert s["A"]["Supervised associate-hour working credit"] == 0
    assert res.metrics["unassigned_hours"] == 8
    assert "PROVISIONAL" in res.working_detail["Note"].iloc[0]


def test_expressly_excluded_hours_clear_the_exception():
    inputs = make_inputs(entries=[entry("u1", "Jones", "M9", 8), entry("u2", "A", "M9", 2)],
                         collections=[collection("p", "M9", 1000)], matters=[matter("M9")],
                         originators=[("M9", "MP", 100)],
                         exclusions=pd.DataFrame([{"entry_id": "u1", "reason": "pro bono",
                                                   "entered_by": "t", "entered_at": ""}]))
    ex = calculate(inputs).exceptions
    assert not (ex["Category"] == "Unassigned associate hours").any()


# 13 -----------------------------------------------------------------------------
def test_inactive_partners_receive_no_compensation():
    r = roster(inactive=("J",))
    inputs = basic_inputs(partners=r)
    inputs.time_entries = pd.concat([inputs.time_entries,
                                     pd.DataFrame([entry("e9", "J", "M1", 10)])])
    res = calculate(inputs)
    s = summary(res)
    assert s["J"]["Total compensation"] == 0
    assert s["J"]["Equal compensation"] == s["J"]["EWYK compensation"] == 0
    assert s["J"]["Total EWYK credit"] > 0  # credit exists but earns nothing
    assert s["J"]["EWYK performance share %"] == 0
    assert sum(res.partner_summary["Total compensation"]) == D("1000000.00")
    assert abs(s["A"]["Equal compensation"] - D("20000.00")) <= D("0.01")  # 200,000 / 10 active
    assert res.reconciled


def test_split_origination_and_missing_originator():
    inputs = make_inputs(entries=[entry("e1", "A", "M1", 1), entry("e2", "A", "M2", 1, invoice="I2")],
                         collections=[collection("p1", "M1", 1000), collection("p2", "M2", 500,
                                                                                invoice="I2")],
                         matters=[matter("M1"), matter("M2")],
                         originators=[("M1", "MP", 60), ("M1", "E", 40)])
    res = calculate(inputs)
    s = summary(res)
    assert s["MP"]["Originating credit"] == D("180.00")
    assert s["E"]["Originating credit"] == D("120.00")
    ex = res.exceptions
    assert ((ex["Category"] == "Missing originating partners") & (ex["Matter ID"] == "M2")).any()
    assert res.metrics["unallocated_credit"] == D("150.00")


def test_originator_shares_not_100_is_blocking():
    inputs = make_inputs(entries=[entry("e1", "A", "M1", 1)],
                         collections=[collection("p1", "M1", 1000)], matters=[matter("M1")],
                         originators=[("M1", "MP", 60), ("M1", "E", 30)])
    ex = calculate(inputs).exceptions
    assert ((ex["Category"] == "Originator allocations not totaling 100%")).any()


def test_matter_level_fallback_is_labelled():
    inputs = make_inputs(entries=[entry("e1", "A", "M1", 3, invoice=""),
                                  entry("e2", "B", "M1", 1, invoice="")],
                         collections=[collection("p1", "M1", 1000, invoice="INV-X")],
                         matters=[matter("M1")], originators=[("M1", "MP", 100)])
    res = calculate(inputs)
    assert set(res.working_detail["Method"]) == {METHOD_MATTER}
    assert res.metrics["fallback_collections"] == 1
    s = summary(res)
    assert s["A"]["Own partner-hour working credit"] == D("525.00")


def test_invoice_level_uses_only_invoice_hours():
    inputs = make_inputs(entries=[entry("e1", "A", "M1", 1, invoice="I1"),
                                  entry("e2", "B", "M1", 9, invoice="I2")],
                         collections=[collection("p1", "M1", 1000, invoice="I1")],
                         matters=[matter("M1")], originators=[("M1", "MP", 100)])
    res = calculate(inputs)
    assert set(res.working_detail["Method"]) == {METHOD_INVOICE}
    assert summary(res)["A"]["Total working credit"] == D("700.00")
    assert summary(res)["B"]["Total working credit"] == 0


def test_collection_exclusions():
    rows = [collection("ok", "M1", 1000),
            collection("trust", "M1", 0, invoice="", payment_type="Trust Deposit"),
            collection("void", "M1", 500, payment_status="Void"),
            collection("late", "M1", 500, day="2026-01-15"),
            collection("disc", "M1", 50, payment_type="Discount")]
    res = calculate(make_inputs(entries=[entry("e1", "A", "M1", 1)], collections=rows,
                                matters=[matter("M1")], originators=[("M1", "MP", 100)]))
    assert res.metrics["total_fees_collected"] == D("1000.00")
    assert res.metrics["collections_excluded"] == 4


def test_nonbillable_and_staff_policy_switches():
    entries = [entry("e1", "A", "M1", 1), entry("e2", "Liu", "M1", 3)]
    nb = entry("e3", "B", "M1", 4)
    nb.update(billable=False, billed_status="Non-billable", hours_billed=0)
    entries.append(nb)
    base = dict(entries=entries, collections=[collection("p", "M1", 1000)],
                matters=[matter("M1")], originators=[("M1", "MP", 100)])
    s = summary(calculate(make_inputs(**base)))
    assert s["A"]["Total working credit"] == D("700.00")  # staff and nonbillable excluded
    pol = Policy(year=2025, distributable_pool=1_000_000, include_staff_hours=True,
                 include_nonbillable=True)
    s = summary(calculate(make_inputs(policy=pol, **base)))
    assert s["B"]["Other delegated-hour working credit"] == D("262.50")  # Liu 3/8 -> default B
    assert s["B"]["Own partner-hour working credit"] == D("350.00")  # nonbillable 4/8


def test_allocate_cents_is_exact():
    parts = allocate_cents(D("100.00"), [D(1), D(1), D(1)])
    assert sum(parts) == D("100.00") and sorted(parts) == [D("33.33"), D("33.33"), D("33.34")]
