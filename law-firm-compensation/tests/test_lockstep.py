"""Lockstep proposal tests (cap, Managing-Partner floor, total, no-data, overrides)."""

from __future__ import annotations

import random
from decimal import Decimal

import pandas as pd

from compensation.calculations import calculate
from compensation.lockstep import LockstepPartner, check_final_weights, propose_lockstep
from conftest import collection, entry, make_inputs, matter

CAP = Decimal("2")
FLOOR = Decimal("18")


def firm(shares: list[float] | None) -> list[LockstepPartner]:
    names = ["MP"] + [f"P{i}" for i in range(1, 11)]
    weights = [Decimal("18")] + [Decimal("8.2")] * 10
    return [LockstepPartner(n, n == "MP", w, None if shares is None else Decimal(str(s)))
            for n, w, s in zip(names, weights, shares or [0] * 11)]


def random_shares(rng: random.Random) -> list[float]:
    raw = [rng.random() ** 2 for _ in range(11)]
    tot = sum(raw)
    return [r / tot * 100 for r in raw]


def proposed(prop) -> dict[str, Decimal]:
    return prop.proposed


# 9 ------------------------------------------------------------------------------
def test_changes_never_exceed_cap():
    rng = random.Random(7)
    for _ in range(300):
        prop = propose_lockstep(firm(random_shares(rng)), CAP, FLOOR)
        assert not prop.problems, prop.problems
        for r in prop.table.to_dict("records"):
            assert abs(Decimal(str(r["Final proposed change"]))) <= CAP


def test_extreme_gaps_are_capped():
    shares = [1, 40, 1, 1, 1, 1, 1, 1, 1, 1, 51]
    prop = propose_lockstep(firm(shares), CAP, FLOOR)
    t = prop.table.set_index("Partner")
    assert t.loc["P1", "Desired change"] == 2.0
    assert all(abs(c) <= 2 for c in t["Final proposed change"])


# 10 -----------------------------------------------------------------------------
def test_managing_partner_never_below_floor():
    rng = random.Random(11)
    for _ in range(300):
        shares = random_shares(rng)
        shares[0] = rng.uniform(0, 5)  # MP with very low EWYK share
        prop = propose_lockstep(firm(shares), CAP, FLOOR)
        assert proposed(prop)["MP"] >= FLOOR
        assert prop.table.iloc[0]["Managing Partner floor check"] == "OK"


def test_mp_above_floor_can_decrease_only_to_floor():
    partners = firm([2] + [9.8] * 10)
    partners[0].current_weight = Decimal("19")
    for p in partners[1:]:
        p.current_weight = Decimal("8.1")
    prop = propose_lockstep(partners, CAP, FLOOR)
    assert proposed(prop)["MP"] == Decimal("18.000")


# 11 -----------------------------------------------------------------------------
def test_proposed_weights_total_exactly_100():
    rng = random.Random(3)
    for _ in range(300):
        prop = propose_lockstep(firm(random_shares(rng)), CAP, FLOOR)
        assert sum(proposed(prop).values()) == Decimal("100")


def test_larger_side_is_scaled_to_balance():
    # Two partners want +2 (capped), one wants -1: increases scaled to 0.5 each.
    shares = [18, 12, 12, 7.2] + [8.2] * 7
    prop = propose_lockstep(firm(shares), CAP, FLOOR)
    p = proposed(prop)
    assert p["P1"] == Decimal("8.700") and p["P2"] == Decimal("8.700")
    assert p["P3"] == Decimal("7.200")
    assert p["MP"] == Decimal("18.000")


# 12 -----------------------------------------------------------------------------
def test_no_ewyk_data_leaves_weights_unchanged():
    prop = propose_lockstep(firm(None), CAP, FLOOR)
    assert prop.unchanged_no_data
    assert proposed(prop) == {p.name: p.current_weight.quantize(Decimal("0.001"))
                              for p in firm(None)}
    # also through the engine: no collections at all
    res = calculate(make_inputs(entries=[], collections=[], matters=[], originators=[]))
    assert res.lockstep.unchanged_no_data
    assert list(res.final_lockstep["Final change"]) == [0.0] * 11


def test_override_checks():
    rows = [{"name": "MP", "is_managing_partner": True, "current": 18, "final": Decimal("17.5")},
            {"name": "A", "is_managing_partner": False, "current": Decimal("8.2"),
             "final": Decimal("11")}]
    problems = check_final_weights(rows, CAP, FLOOR)
    assert any("floor" in p for p in problems)
    assert any("cap" in p for p in problems)
    assert any("total" in p for p in problems)


def test_engine_applies_valid_override():
    inputs = make_inputs(entries=[entry("e1", "A", "M1", 1)],
                         collections=[collection("p", "M1", 1000)], matters=[matter("M1")],
                         originators=[("M1", "A", 100)])
    base = calculate(inputs).final_lockstep.set_index("Partner")
    a, b = base.loc["A", "Proposed weight"], base.loc["B", "Proposed weight"]
    inputs.lockstep_overrides = pd.DataFrame([
        {"partner": "A", "override_weight": round(a - 0.5, 3), "reason": "committee",
         "entered_by": "x", "entered_at": "now"},
        {"partner": "B", "override_weight": round(b + 0.5, 3), "reason": "committee",
         "entered_by": "x", "entered_at": "now"}])
    res = calculate(inputs)
    fl = res.final_lockstep.set_index("Partner")
    assert fl.loc["A", "Overridden"] and fl.loc["A", "Override reason"] == "committee"
    assert not (res.exceptions["Category"] == "Lockstep proposal").any()
