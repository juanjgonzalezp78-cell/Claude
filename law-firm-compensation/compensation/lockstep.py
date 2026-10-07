"""Annual lockstep adjustment: proposal, balancing, rounding and policy checks.

All weights are percent numbers held as :class:`~decimal.Decimal` and rounded
to :data:`~compensation.models.WEIGHT_PCT_QUANTUM` (0.001 percentage points).

Algorithm (see README "Annual lockstep adjustment"):

1. ``gap = EWYK share - current weight``
2. ``desired = clamp(gap, -cap, +cap)``; for the Managing Partner the lower
   bound is also ``floor - current`` so the proposal cannot go below the floor;
   no weight may go below zero.
3. Total desired increases and decreases are compared and the larger side is
   scaled down proportionally so that increases equal decreases (net zero).
   Scaling only shrinks changes, so caps and floors remain satisfied.  (If a
   bound would still be breached - only possible when a current weight already
   violates the Managing-Partner floor - the residual is spread across the other
   side within their bounds.)
4. Proposed weights are rounded to 0.001% with a largest-remainder method so
   they total exactly 100.000%; because caps/floors are whole quanta, rounding
   can never breach them.
5. If no EWYK data exists, every weight is left unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd

from .models import HUNDRED, WEIGHT_PCT_QUANTUM, ZERO, to_decimal

EPS = Decimal("1e-24")


@dataclass
class LockstepPartner:
    """Input row for the proposal."""

    name: str
    is_managing_partner: bool
    current_weight: Decimal
    ewyk_share: Decimal | None = None  # percent number; None = no EWYK data


@dataclass
class LockstepProposal:
    """Result of :func:`propose_lockstep`."""

    table: pd.DataFrame
    unchanged_no_data: bool
    messages: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def proposed(self) -> dict[str, Decimal]:
        """Partner -> proposed weight (percent)."""
        return {r["Partner"]: Decimal(str(r["Proposed next-year weight"]))
                for r in self.table.to_dict("records")} if not self.table.empty else {}


def quantize_weight(value: Decimal) -> Decimal:
    """Round a percent weight to the lockstep precision."""
    return to_decimal(value).quantize(WEIGHT_PCT_QUANTUM, rounding=ROUND_HALF_UP)


def _balance(desired: list[Decimal], lo: list[Decimal], hi: list[Decimal]) -> list[Decimal]:
    """Adjust ``desired`` changes so they sum to zero, scaling the larger side."""
    c = list(desired)
    total = sum(c, ZERO)
    if abs(total) <= EPS:
        return c
    n = len(c)
    if total > 0:
        # Phase A: shrink increases toward max(lo, 0) proportionally (= scaling increases).
        room = [max(c[i] - max(lo[i], ZERO), ZERO) if c[i] > 0 else ZERO for i in range(n)]
        c = _take(c, room, total, sign=-1)
        total = sum(c, ZERO)
        if total > EPS:
            # Phase B (pathological): deepen decreases within their lower bounds.
            room = [max(c[i] - lo[i], ZERO) for i in range(n)]
            c = _take(c, room, total, sign=-1)
    else:
        need = -total
        room = [max(min(hi[i], ZERO) - c[i], ZERO) if c[i] < 0 else ZERO for i in range(n)]
        c = _take(c, room, need, sign=1)
        total = sum(c, ZERO)
        if total < -EPS:
            room = [max(hi[i] - c[i], ZERO) for i in range(n)]
            c = _take(c, room, -total, sign=1)
    return c


def _take(c: list[Decimal], room: list[Decimal], amount: Decimal, sign: int) -> list[Decimal]:
    total_room = sum(room, ZERO)
    if total_room <= 0:
        return c
    factor = min(Decimal(1), amount / total_room)
    return [c[i] + sign * room[i] * factor for i in range(len(c))]


def _round_to_total(values: list[Decimal], target_total: Decimal) -> list[Decimal]:
    """Round to the weight quantum keeping an exact total (largest remainder)."""
    units = [v / WEIGHT_PCT_QUANTUM for v in values]
    floors = [u.to_integral_value(rounding=ROUND_FLOOR) for u in units]
    target_units = (target_total / WEIGHT_PCT_QUANTUM).to_integral_value(rounding=ROUND_HALF_UP)
    leftover = int(target_units - sum(floors, ZERO))
    order = sorted(range(len(values)), key=lambda i: (-(units[i] - floors[i]), i))
    candidates = [i for i in order if units[i] - floors[i] > EPS]
    for i in candidates[:max(leftover, 0)]:
        floors[i] += 1
    return [f * WEIGHT_PCT_QUANTUM for f in floors]


def propose_lockstep(partners: list[LockstepPartner], max_change: Decimal,
                     mp_floor: Decimal) -> LockstepProposal:
    """Compute proposed next-year weights for active partners."""
    max_change = to_decimal(max_change)
    mp_floor = to_decimal(mp_floor)
    messages: list[str] = []
    problems: list[str] = []
    if not partners:
        return LockstepProposal(pd.DataFrame(), True, ["No active partners."], [])
    current = [quantize_weight(p.current_weight) for p in partners]
    for p, q in zip(partners, current):
        if to_decimal(p.current_weight) != q:
            messages.append(f"{p.name}: current weight rounded to {q}% (precision 0.001%).")
    total_current = sum(current, ZERO)
    if total_current != HUNDRED:
        problems.append(f"Current active lockstep weights total {total_current}%, not 100%.")

    shares = [p.ewyk_share for p in partners]
    no_data = all(s is None for s in shares) or sum((s or ZERO for s in shares), ZERO) == 0
    rows: list[dict[str, Any]] = []
    if no_data:
        messages.append("No EWYK data exists, so all lockstep weights are left unchanged.")
        final_changes = [ZERO] * len(partners)
        desired = [ZERO] * len(partners)
        gaps: list[Decimal | None] = [None] * len(partners)
        balanced = [ZERO] * len(partners)
    else:
        gaps = []
        desired = []
        lo: list[Decimal] = []
        hi: list[Decimal] = []
        for p, w in zip(partners, current):
            share = to_decimal(p.ewyk_share or ZERO)
            gap = share - w
            low = max(-max_change, -w)
            if p.is_managing_partner:
                low = max(low, mp_floor - w)
            high = max_change
            if low > high:
                problems.append(
                    f"{p.name}: the Managing Partner floor cannot be reached within the "
                    f"{max_change}-point cap from a current weight of {w}%."
                )
                low = high
            gaps.append(gap)
            desired.append(min(max(gap, low), high))
            lo.append(low)
            hi.append(high)
        balanced = _balance(desired, lo, hi)
        proposed_raw = [w + c for w, c in zip(current, balanced)]
        proposed = _round_to_total(proposed_raw, total_current)
        final_changes = [pw - w for pw, w in zip(proposed, current)]
        inc = sum((d for d in desired if d > 0), ZERO)
        dec = -sum((d for d in desired if d < 0), ZERO)
        if inc > dec:
            messages.append(f"Desired increases ({inc:.3f} pts) exceeded desired decreases "
                            f"({dec:.3f} pts); increases were scaled by "
                            f"{(dec / inc * 100) if inc else 0:.2f}%.")
        elif dec > inc:
            messages.append(f"Desired decreases ({dec:.3f} pts) exceeded desired increases "
                            f"({inc:.3f} pts); decreases were scaled by "
                            f"{(inc / dec * 100) if dec else 0:.2f}%.")

    for i, p in enumerate(partners):
        w = current[i]
        change = final_changes[i]
        new = w + change
        cap_ok = abs(change) <= max_change
        floor_ok = (new >= mp_floor) if p.is_managing_partner else True
        rows.append({
            "Partner": p.name,
            "Managing Partner": p.is_managing_partner,
            "Current weight": float(w),
            "EWYK performance share": None if p.ewyk_share is None else float(p.ewyk_share),
            "Performance gap": None if gaps[i] is None else float(gaps[i]),
            "Desired change": float(desired[i]),
            "Balancing adjustment": float(change - desired[i]),
            "Final proposed change": float(change),
            "Proposed next-year weight": float(new),
            "Cap check": "OK" if cap_ok else f"EXCEEDS {max_change} pts",
            "Managing Partner floor check": ("OK" if floor_ok else f"BELOW {mp_floor}%")
            if p.is_managing_partner else "n/a",
        })
        if not cap_ok:
            problems.append(f"{p.name}: change {change}% exceeds the {max_change}-point cap.")
        if not floor_ok:
            problems.append(f"{p.name}: proposed weight {new}% is below the {mp_floor}% floor.")
    table = pd.DataFrame(rows)
    proposed_total = sum((current[i] + final_changes[i] for i in range(len(partners))), ZERO)
    if proposed_total != HUNDRED:
        problems.append(f"Proposed weights total {proposed_total}%, not 100%.")
    return LockstepProposal(table, no_data, messages, problems)


def check_final_weights(rows: list[dict[str, Any]], max_change: Decimal,
                        mp_floor: Decimal) -> list[str]:
    """Validate final (possibly overridden) weights.

    ``rows`` items need ``name``, ``is_managing_partner``, ``current`` and ``final``
    (percent numbers).  Returns a list of human-readable problems (empty = OK).
    """
    max_change = to_decimal(max_change)
    mp_floor = to_decimal(mp_floor)
    problems: list[str] = []
    total = ZERO
    mps = 0
    for r in rows:
        cur = quantize_weight(to_decimal(r["current"]))
        fin = to_decimal(r["final"])
        if fin != quantize_weight(fin):
            problems.append(f"{r['name']}: weight {fin}% has more than 3 decimal places.")
        total += fin
        if fin < 0:
            problems.append(f"{r['name']}: weight cannot be negative.")
        if abs(fin - cur) > max_change:
            problems.append(f"{r['name']}: change of {fin - cur:+}% exceeds the "
                            f"{max_change}-point cap.")
        if r.get("is_managing_partner"):
            mps += 1
            if fin < mp_floor:
                problems.append(f"{r['name']} (Managing Partner): {fin}% is below the "
                                f"{mp_floor}% floor.")
    if mps != 1:
        problems.append(f"Exactly one active Managing Partner is required (found {mps}).")
    if total != HUNDRED:
        problems.append(f"Final weights total {total}%, not 100%.")
    return problems
