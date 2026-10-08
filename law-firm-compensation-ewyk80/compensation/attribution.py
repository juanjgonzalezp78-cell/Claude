"""Time-entry qualification and supervising-partner attribution.

Every non-partner time entry is attributed using this priority:

1. Explicit time-entry-level supervising-partner override
2. Associate-matter supervising-partner mapping (effective-dated, may be split)
3. Matter's compensation supervising partner
4. Matter's TimeSolv Responsible Professional (only if that person is a partner)
5. Timekeeper's default supervising partner
6. Unassigned exception requiring review (never silently allocated)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .models import (HUNDRED, ZERO, AttributionSource, Policy, TimekeeperCategory, WorkingBucket,
                     WorkingMethod, clean_str, norm_name, parse_date, to_decimal)


@dataclass
class Allocation:
    """One partner's slice of a time entry."""

    partner: str | None
    pct: Decimal
    source: str
    note: str = ""


@dataclass
class AttributedEntry:
    """A time entry with its qualifying measure and partner attribution."""

    entry_id: str
    work_date: date | None
    matter_id: str
    invoice_id: str
    timekeeper: str
    category: str
    bucket: str
    hours: Decimal
    measure: Decimal
    measure_note: str
    allocations: list[Allocation] = field(default_factory=list)
    excluded_reason: str = ""

    @property
    def unassigned(self) -> bool:
        """True if any part of a qualifying entry lacks a credited partner."""
        return self.measure > 0 and not self.excluded_reason and any(
            a.partner is None for a in self.allocations)


class Roster:
    """Partner roster lookups (case/whitespace-insensitive)."""

    def __init__(self, partners: list[dict[str, Any]]) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self._by_norm: dict[str, str] = {}
        for p in partners:
            name = clean_str(p.get("name"))
            if not name:
                continue
            self.records[name] = p
            self._by_norm[norm_name(name)] = name

    def resolve(self, name: Any) -> str | None:
        """Canonical partner name or None if not on the roster."""
        return self._by_norm.get(norm_name(name))

    def is_active(self, name: str) -> bool:
        """Whether a canonical partner is active."""
        rec = self.records.get(name)
        return bool(rec and rec.get("active"))

    def active_names(self) -> list[str]:
        """Active partners in roster order."""
        return [n for n, r in self.records.items() if r.get("active")]


def qualifying_measure(e: dict[str, Any], policy: Policy) -> tuple[Decimal, str, set[str]]:
    """Return (credited measure, explanation, flags) for one time entry.

    The measure is hours, except under the *Billed value* method where it is
    dollars.  Flags: ``billable_assumed``, ``hours_billed_missing``,
    ``billed_value_derived``.
    """
    flags: set[str] = set()
    hours = to_decimal(e.get("hours"))
    hours_billed = to_decimal(e.get("hours_billed"), default=None)
    rate = to_decimal(e.get("rate"))
    billed_value = to_decimal(e.get("billed_value"), default=None)
    status = norm_name(e.get("billed_status"))
    billable = e.get("billable")
    if billable is None or (isinstance(billable, float) and billable != billable):
        billable = "non" not in status and "no charge" not in status
        flags.add("billable_assumed")
    written_off = any(w in status for w in ("write", "written", "no charge")) or status == "wo"
    billed = (not written_off) and (
        ("billed" in status and "unbilled" not in status and "not" not in status)
        or "invoiced" in status or "paid" in status
        or (status == "" and bool(clean_str(e.get("invoice_id"))))
    )
    method = policy.working_method

    if not billable:
        if not policy.include_nonbillable:
            return ZERO, "Nonbillable - excluded by policy", flags
        value = hours * rate if method == WorkingMethod.BILLED_VALUE else hours
        return value, "Nonbillable - included by policy", flags
    if written_off:
        if not policy.include_written_off:
            return ZERO, "Written off - excluded by policy", flags
        value = hours * rate if method == WorkingMethod.BILLED_VALUE else hours
        return value, "Written off - included by policy", flags

    write_down = ZERO
    if billed and hours_billed is not None and hours_billed < hours:
        write_down = hours - hours_billed

    if method in (WorkingMethod.BILLED_HOURS, WorkingMethod.MANUAL):
        if not billed:
            return ZERO, "Not billed", flags
        if hours_billed is None:
            flags.add("hours_billed_missing")
            return hours, "Billed (hours billed missing - recorded hours used)", flags
        if policy.include_written_off and write_down > 0:
            return hours, "Billed incl. written-down hours (policy)", flags
        return hours_billed, "Billed hours", flags
    if method == WorkingMethod.RECORDED_BILLABLE:
        if write_down > 0 and not policy.include_written_off:
            return hours - write_down, "Recorded billable hours less write-down", flags
        return hours, "Recorded billable hours", flags
    if method == WorkingMethod.BILLED_VALUE:
        if not billed:
            return ZERO, "Not billed", flags
        if billed_value is None:
            flags.add("billed_value_derived")
            base = hours_billed if hours_billed is not None else hours
            billed_value = base * rate
        if policy.include_written_off and write_down > 0:
            billed_value += write_down * rate
            return billed_value, "Billed value incl. written-down value (policy)", flags
        return billed_value, "Billed value", flags
    raise ValueError(f"Unknown working method: {method}")


class Attributor:
    """Applies the six-level attribution hierarchy to time entries."""

    def __init__(self, policy: Policy, roster: Roster, timekeepers: list[dict[str, Any]],
                 matters: list[dict[str, Any]], supervision: list[dict[str, Any]],
                 entry_overrides: list[dict[str, Any]], exclusions: list[dict[str, Any]]) -> None:
        self.policy = policy
        self.roster = roster
        self.tk_by_id: dict[str, dict[str, Any]] = {}
        self.tk_by_name: dict[str, dict[str, Any]] = {}
        for tk in timekeepers:
            if norm_name(tk.get("timekeeper_id")):
                self.tk_by_id[norm_name(tk.get("timekeeper_id"))] = tk
            if norm_name(tk.get("name")):
                self.tk_by_name[norm_name(tk.get("name"))] = tk
        self.matters = {norm_name(m.get("matter_id")): m for m in matters
                        if norm_name(m.get("matter_id"))}
        self.mappings: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for m in supervision:
            tk = self.lookup_timekeeper(m.get("timekeeper"), m.get("timekeeper"))
            key_tk = norm_name(tk.get("name")) if tk else norm_name(m.get("timekeeper"))
            self.mappings[(key_tk, norm_name(m.get("matter_id")))].append(m)
        self.overrides: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in entry_overrides:
            self.overrides[norm_name(o.get("entry_id"))].append(o)
        self.exclusions = {norm_name(x.get("entry_id")): clean_str(x.get("reason"))
                           for x in exclusions if norm_name(x.get("entry_id"))}

    def lookup_timekeeper(self, tk_id: Any, name: Any) -> dict[str, Any] | None:
        """Find a timekeeper by ID first, then by name."""
        if norm_name(tk_id) and norm_name(tk_id) in self.tk_by_id:
            return self.tk_by_id[norm_name(tk_id)]
        return self.tk_by_name.get(norm_name(name))

    # ------------------------------------------------------------------
    def _partner_allocs(self, rows: list[dict[str, Any]], pct_field: str, source: str,
                        label: str) -> list[Allocation] | str:
        """Convert mapping/override rows to allocations, or an error string."""
        total = sum((to_decimal(r.get(pct_field)) for r in rows), ZERO)
        if total != HUNDRED:
            return f"{label} percentages total {total}% (must be 100%)"
        allocs = []
        for r in rows:
            partner = self.roster.resolve(r.get("partner"))
            if partner is None:
                return f"{label} names '{clean_str(r.get('partner'))}', who is not on the partner roster"
            allocs.append(Allocation(partner, to_decimal(r.get(pct_field)), source,
                                     clean_str(r.get("notes") or r.get("reason"))))
        return allocs

    def _mapping_rows(self, tk_name: str, matter_id: str, work_date: date | None) -> list[dict[str, Any]]:
        rows = self.mappings.get((norm_name(tk_name), norm_name(matter_id)), [])
        out = []
        for r in rows:
            start = parse_date(r.get("start_date"))
            end = parse_date(r.get("end_date"))
            if work_date is None:
                if start is None and end is None:
                    out.append(r)
                continue
            if (start is None or start <= work_date) and (end is None or work_date <= end):
                out.append(r)
        return out

    def attribute(self, e: dict[str, Any]) -> tuple[AttributedEntry, list[str], set[str]]:
        """Attribute one normalized time entry. Returns (entry, issues, flags)."""
        issues: list[str] = []
        entry_id = clean_str(e.get("entry_id")) or clean_str(e.get("dedup_key"))
        tk_name = clean_str(e.get("timekeeper"))
        tk = self.lookup_timekeeper(e.get("timekeeper_id"), tk_name)
        matter_id = clean_str(e.get("matter_id"))
        work_date = parse_date(e.get("work_date"))
        measure, note, flags = qualifying_measure(e, self.policy)

        partner_self: str | None = None
        if tk is not None:
            category = clean_str(tk.get("category")) or TimekeeperCategory.OTHER
            tk_name = clean_str(tk.get("name")) or tk_name
            if category == TimekeeperCategory.PARTNER:
                partner_self = self.roster.resolve(tk.get("linked_partner") or tk.get("name"))
        else:
            partner_self = self.roster.resolve(tk_name)
            category = TimekeeperCategory.PARTNER if partner_self else "Unknown"

        if category == TimekeeperCategory.PARTNER:
            bucket = WorkingBucket.OWN
        elif category == TimekeeperCategory.ASSOCIATE:
            bucket = WorkingBucket.ASSOCIATE
        else:
            bucket = WorkingBucket.DELEGATED

        ae = AttributedEntry(entry_id=entry_id, work_date=work_date, matter_id=matter_id,
                             invoice_id=clean_str(e.get("invoice_id")), timekeeper=tk_name,
                             category=category, bucket=bucket, hours=to_decimal(e.get("hours")),
                             measure=measure, measure_note=note)

        if norm_name(entry_id) in self.exclusions:
            ae.excluded_reason = "Expressly excluded: " + (self.exclusions[norm_name(entry_id)] or "")
            ae.measure = ZERO
            return ae, issues, flags
        if category in (TimekeeperCategory.PARALEGAL, TimekeeperCategory.OTHER) \
                and not self.policy.include_staff_hours:
            ae.measure = ZERO
            ae.measure_note = "Paralegal/staff hours excluded by policy"
            return ae, issues, flags

        if category == TimekeeperCategory.PARTNER:
            if partner_self is None:
                ae.allocations = [Allocation(None, HUNDRED, AttributionSource.UNASSIGNED,
                                             "Partner timekeeper is not on the partner roster")]
                issues.append("partner_not_on_roster")
            else:
                ae.allocations = [Allocation(partner_self, HUNDRED, AttributionSource.OWN)]
            return ae, issues, flags
        if category == "Unknown":
            ae.allocations = [Allocation(None, HUNDRED, AttributionSource.UNASSIGNED,
                                         "Unknown timekeeper - add to Firm Setup > Timekeepers")]
            issues.append("unknown_timekeeper")
            return ae, issues, flags

        # 1. Entry-level override
        if norm_name(entry_id) in self.overrides:
            res = self._partner_allocs(self.overrides[norm_name(entry_id)], "allocation_pct",
                                       AttributionSource.ENTRY_OVERRIDE, "Time-entry override")
            if isinstance(res, str):
                ae.allocations = [Allocation(None, HUNDRED, AttributionSource.UNASSIGNED, res)]
                issues.append("override_invalid")
            else:
                ae.allocations = res
            return ae, issues, flags
        # 2. Associate-matter mapping
        rows = self._mapping_rows(tk_name, matter_id, work_date)
        if rows:
            res = self._partner_allocs(rows, "allocation_pct", AttributionSource.MATTER_MAPPING,
                                       "Associate-matter mapping")
            if isinstance(res, str):
                ae.allocations = [Allocation(None, HUNDRED, AttributionSource.UNASSIGNED, res)]
                issues.append("mapping_invalid")
            else:
                ae.allocations = res
            return ae, issues, flags
        matter = self.matters.get(norm_name(matter_id), {})
        # 3-5. Fallback levels
        for source, name in (
            (AttributionSource.COMP_SUPERVISOR, matter.get("comp_supervising_partner")),
            (AttributionSource.RESPONSIBLE, matter.get("responsible_professional")),
            (AttributionSource.DEFAULT_SUPERVISOR, tk.get("default_supervisor") if tk else None),
        ):
            partner = self.roster.resolve(name)
            if partner:
                ae.allocations = [Allocation(partner, HUNDRED, source)]
                return ae, issues, flags
        # 6. Unassigned
        ae.allocations = [Allocation(None, HUNDRED, AttributionSource.UNASSIGNED,
                                     "No supervising partner could be determined")]
        issues.append("unassigned")
        return ae, issues, flags
