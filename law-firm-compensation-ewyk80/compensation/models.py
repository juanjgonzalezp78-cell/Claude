"""Core domain types, policy settings and exact-decimal helpers.

All monetary arithmetic in the application uses :class:`decimal.Decimal`.
Percentages are stored and displayed as *percent numbers* (``18.0`` means 18%)
and converted to fractions only inside calculations.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from typing import Any, Iterable, Sequence

getcontext().prec = 40

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")



class WorkingMethod:
    """Allowed working-share methodologies."""

    BILLED_HOURS = "Billed hours"
    RECORDED_BILLABLE = "Recorded billable hours"
    BILLED_VALUE = "Billed value"
    MANUAL = "Manual percentages"

    ALL = (BILLED_HOURS, RECORDED_BILLABLE, BILLED_VALUE, MANUAL)


class TimekeeperCategory:
    """Timekeeper categories used for attribution."""

    PARTNER = "Partner"
    ASSOCIATE = "Associate"
    PARALEGAL = "Paralegal"
    OTHER = "Other staff"

    ALL = (PARTNER, ASSOCIATE, PARALEGAL, OTHER)


class Severity:
    """Exception severities. Blocking items prevent finalization."""

    BLOCKING = "Blocking"
    WARNING = "Warning"
    INFO = "Info"


class AttributionSource:
    """Where a credited-hour attribution came from (hierarchy levels 0-6)."""

    OWN = "0. Partner's own hours"
    ENTRY_OVERRIDE = "1. Time-entry override"
    MATTER_MAPPING = "2. Associate-matter mapping"
    COMP_SUPERVISOR = "3. Matter compensation supervising partner"
    RESPONSIBLE = "4. Matter Responsible Professional"
    DEFAULT_SUPERVISOR = "5. Timekeeper default supervisor"
    UNASSIGNED = "6. UNASSIGNED - review required"


class WorkingBucket:
    """Working-credit components reported per partner."""

    OWN = "Own partner hours"
    ASSOCIATE = "Supervised associate hours"
    DELEGATED = "Other delegated hours"


@dataclass
class Policy:
    """Editable compensation policy for one compensation year.

    Every assumption the engine relies on lives here so that it is visible and
    editable in the Firm Setup page and preserved in annual snapshots.
    """

    year: int = 2025
    distributable_pool: Decimal = Decimal("9500000.00")
    equal_pct: Decimal = Decimal("20")
    ewyk_pct: Decimal = Decimal("80")
    origination_pct: Decimal = Decimal("30")
    working_pct: Decimal = Decimal("70")
    originator_working_eligible: bool = True
    include_nonbillable: bool = False
    include_written_off: bool = False
    include_staff_hours: bool = False
    working_method: str = WorkingMethod.BILLED_HOURS
    prorate_equal_expenses: bool = True
    negative_net_treatment: str = "Carry forward to next year"
    period_start: date | None = None
    period_end: date | None = None

    def __post_init__(self) -> None:
        for f in fields(self):
            if f.type in ("Decimal",) or isinstance(getattr(self, f.name), float):
                value = getattr(self, f.name)
                if value is not None and not isinstance(value, Decimal):
                    setattr(self, f.name, to_decimal(value))
        if self.period_start is None:
            self.period_start = date(int(self.year), 1, 1)
        if self.period_end is None:
            self.period_end = date(int(self.year), 12, 31)

    # ------------------------------------------------------------------ helpers
    def frac(self, name: str) -> Decimal:
        """Return a percent field as a fraction (e.g. ``equal_pct`` 20 -> 0.20)."""
        return to_decimal(getattr(self, name)) / HUNDRED

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-friendly primitives."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if isinstance(value, Decimal):
                out[key] = str(value)
            elif isinstance(value, date):
                out[key] = value.isoformat()
            else:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        """Build a policy from :meth:`to_dict` output, ignoring unknown keys."""
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("period_start", "period_end"):
                kwargs[key] = parse_date(value)
            elif key == "year":
                kwargs[key] = int(value)
            elif key in ("originator_working_eligible", "include_nonbillable",
                         "include_written_off", "include_staff_hours",
                         "prorate_equal_expenses"):
                kwargs[key] = bool(value)
            elif key in ("working_method", "negative_net_treatment"):
                kwargs[key] = str(value)
            else:
                kwargs[key] = to_decimal(value)
        return cls(**kwargs)

    def describe(self) -> list[tuple[str, str]]:
        """Human-readable list of every policy assumption (for reports)."""
        return [
            ("Compensation year", str(self.year)),
            ("Compensation period", f"{self.period_start} to {self.period_end}"),
            ("Distributable partner-compensation pool", fmt_money(self.distributable_pool)),
            ("Equal-share percentage", fmt_pct(self.equal_pct)),
            ("EWYK percentage", fmt_pct(self.ewyk_pct)),
            ("Originating-partner portion of EWYK credit", fmt_pct(self.origination_pct)),
            ("Working-partner portion of EWYK credit", fmt_pct(self.working_pct)),
            ("Originator eligible for working credit", yes_no(self.originator_working_eligible)),
            ("Include nonbillable hours", yes_no(self.include_nonbillable)),
            ("Include written-off hours", yes_no(self.include_written_off)),
            ("Include paralegal / other staff hours", yes_no(self.include_staff_hours)),
            ("Working-share methodology", self.working_method),
            ("Prorate equal expense splits by time as partner", yes_no(self.prorate_equal_expenses)),
            ("Negative net compensation", self.negative_net_treatment),
        ]


# --------------------------------------------------------------------------
# Conversion helpers
# --------------------------------------------------------------------------

def to_decimal(value: Any, default: Decimal | None = ZERO) -> Decimal | None:
    """Convert numbers / strings (``$1,234.50``, ``(12.00)``, ``45%``) to Decimal.

    Floats are converted through ``repr`` rounded to 10 decimals so that values
    coming back from UI widgets (e.g. ``8.200000000001``) do not leak binary
    floating-point noise into the calculations.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return default if value.is_nan() else value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return default
        return Decimal(repr(round(value, 10))).normalize() if value != 0 else ZERO
    text = str(value).strip()
    if text == "" or text.lower() in ("nan", "none", "null", "nat", "-", "--"):
        return default
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"'{value}' is not a valid number")
    return -result if negative else result


def parse_date(value: Any) -> date | None:
    """Parse common date representations; return ``None`` for blanks."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        import pandas as pd

        if value is pd.NaT or (isinstance(value, float) and math.isnan(value)):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date()
    except ImportError:  # pragma: no cover
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "null"):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y",
                "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a recognizable date")


TRUE_WORDS = {"y", "yes", "true", "t", "1", "billable", "billed", "active", "x", "on"}
FALSE_WORDS = {"n", "no", "false", "f", "0", "non-billable", "nonbillable", "non billable",
               "unbilled", "inactive", "off", "not billable", ""}


def parse_bool(value: Any, default: bool | None = None) -> bool | None:
    """Parse yes/no style values. Returns ``default`` for blanks/unknown."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("nan", "none"):
        return default
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False if text else default
    return default


def norm_name(value: Any) -> str:
    """Normalize a person / ID string for case- and whitespace-insensitive matching."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = " ".join(str(value).strip().split()).lower()
    return "" if text in ("nan", "none") else text


def clean_str(value: Any) -> str:
    """Return a trimmed string, mapping NaN/None to empty string."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


def money(value: Decimal) -> Decimal:
    """Round half-up to cents."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate_cents(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``total`` (rounded to cents) across ``weights`` so parts sum exactly.

    Uses the largest-remainder method: every share is floored to the cent, then
    the leftover cents go one at a time to the largest fractional remainders
    (ties broken by list order, which is deterministic).  Negative or zero
    weights receive nothing.  If all weights are zero an all-zero list is
    returned and the caller must report the unallocated amount.
    """
    total = money(total)
    w = [to_decimal(x) if to_decimal(x) > 0 else ZERO for x in weights]
    wsum = sum(w, ZERO)
    if not w or wsum == 0:
        return [ZERO.quantize(CENT) for _ in w]
    sign = Decimal(-1) if total < 0 else ONE
    cents_total = int((abs(total) * HUNDRED).to_integral_value())
    raw = [Decimal(cents_total) * x / wsum for x in w]
    floors = [int(r.to_integral_value(rounding=ROUND_FLOOR)) for r in raw]
    leftover = cents_total - sum(floors)
    order = sorted(range(len(w)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [sign * Decimal(c) / HUNDRED for c in floors]


def dsum(values: Iterable[Any]) -> Decimal:
    """Exact Decimal sum that tolerates None."""
    total = ZERO
    for v in values:
        if v is not None:
            total += to_decimal(v)
    return total


def fmt_money(value: Any) -> str:
    """Format a value as ``$1,234.56``."""
    v = money(to_decimal(value))
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def fmt_pct(value: Any, places: int = 3) -> str:
    """Format a percent number (``18`` -> ``18.000%``)."""
    v = to_decimal(value)
    return f"{v:.{places}f}%"


def yes_no(flag: bool) -> str:
    """Return ``Yes`` / ``No``."""
    return "Yes" if flag else "No"


@dataclass
class ExceptionItem:
    """A single audit/exception record produced by validation or calculation."""

    severity: str
    category: str
    description: str
    reference: str = ""
    matter_id: str = ""
    amount: Decimal | None = None
    hours: Decimal | None = None
    resolution: str = ""

    def as_row(self) -> dict[str, Any]:
        """Flat dict for DataFrames / Excel."""
        return {
            "Severity": self.severity,
            "Category": self.category,
            "Description": self.description,
            "Reference": self.reference,
            "Matter ID": self.matter_id,
            "Amount": float(self.amount) if self.amount is not None else None,
            "Hours / Measure": float(self.hours) if self.hours is not None else None,
            "How to resolve": self.resolution,
        }


@dataclass
class AuditContext:
    """Who is making a change (recorded in the audit log)."""

    user: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)
