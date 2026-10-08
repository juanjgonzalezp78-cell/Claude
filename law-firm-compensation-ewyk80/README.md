# Law-Firm Partner Compensation — Alternate Model (20% Equal / 80% EWYK)

This is the **alternate version** of the partner compensation program. It
calculates annual compensation for an 11-partner law firm from TimeSolv exports
using two components instead of three:

| Component | Share of the distributable pool | Basis |
|---|---|---|
| Equal share | 20% | Split equally among active partners |
| EWYK ("eat what you kill") | 80% | Origination credit (30%) + working credit (70%) from collected professional fees |

There is **no lockstep component** in this version. The roster carries no
lockstep weights, and there is no Lockstep Update page. The original
20/40/40 version is in the sibling folder `law-firm-compensation/` and is
unchanged. Each version keeps its own database (`data/compensation.db` inside
its own folder), so the two can be run side by side for comparison.

The app imports TimeSolv files, credits origination and working time, attributes
associate hours to supervising partners matter by matter, calculates
compensation to the cent, lists every exception, exports a 12-sheet Excel report,
and stores finalized years in SQLite.

---

## 1. Installation

You need Python 3.12 (3.13 also works).

```bash
cd law-firm-compensation-ewyk80
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Running the application

```bash
# Optional: set an administrator password. You need it to reopen a finalized year.
export COMP_ADMIN_PASSWORD='choose-a-strong-password'    # Windows: set COMP_ADMIN_PASSWORD=...

streamlit run app.py
```

Streamlit opens http://localhost:8501. Before you change anything:

1. Enter **your name** in the sidebar. Every change is written to the audit log
   under that name.
2. Pick the **compensation year** in the sidebar.
3. To explore the app, open **Dashboard → Load Demo Data**. This loads the 2025
   sample firm through the real import pipeline. It replaces any 2025 data.

Optional environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `COMP_ADMIN_PASSWORD` | Password for reopening finalized years. Never hard-coded. If it is not set, reopening is disabled. | unset |
| `COMP_DB_PATH` | Location of the SQLite database | `data/compensation.db` |

Run the tests with:

```bash
pytest
```

## 3. Project layout

```
law-firm-compensation-ewyk80/
├── app.py                     Streamlit entry point (navigation + sidebar)
├── requirements.txt
├── compensation/              UI-independent engine
│   ├── models.py              Policy, Decimal helpers, cent-exact allocation
│   ├── imports.py             TimeSolv import wizard backend (mapping, validation, dedup)
│   ├── attribution.py         Hours qualification + 6-level supervising-partner hierarchy
│   ├── calculations.py        Origination/working credits, EWYK, equal share, reconciliation
│   ├── validation.py          Policy/roster/mapping validation, finalization blockers
│   ├── exports.py             12-sheet formatted Excel report
│   ├── database.py            SQLite storage, audit log, snapshots, read-only years
│   ├── service.py             DB ↔ engine glue, finalize, copy year, rename partner
│   └── demo.py                "Load Demo Data"
├── ui/common.py               Shared Streamlit helpers
├── pages/                     dashboard, setup, imports, mappings, results,
│                              audit, history
├── sample_data/               Demo TimeSolv-style CSVs + generator script
├── data/                      SQLite database (created at first run)
└── tests/                     pytest suite (unit, integration, page smoke tests)
```

## 4. Required TimeSolv exports

| Dataset (Imports page) | Recommended TimeSolv source | Required logical fields |
|---|---|---|
| Partner roster | Your own CSV (`sample_data/partners.csv` shows the format) | Partner name |
| Professionals / timekeepers | Professionals list | Name |
| Matters / projects | Projects list (add an "Originating Attorney" custom field if you track one) | Matter/Project ID |
| Time entries | Time-entry or timeslip export, including **Invoice Number**, **Billed Hours** and **Billed Amount** | Work date, Matter ID, Timekeeper, Hours |
| Payment & collection allocations | **Invoice Summary with Payment Allocations**, with payment allocations turned on | Collection date, Matter ID, Amount allocated to professional fees |

Import them in this order: roster → professionals → matters → time entries → payments.

Only the **professional-fee allocation** of a payment earns credit. The engine
leaves out the following and lists each one under *Audit & Exceptions → Excluded
collections*:

- expense allocations and taxes (they are never credited);
- unapplied trust or retainer deposits (type contains trust/retainer/unapplied and no invoice);
- write-offs, discounts, credit memos and adjustments;
- void, pending, reversed, NSF or outstanding payments (status or type text);
- payments dated outside the compensation period;
- rows with a zero fee allocation, or a negative one (negatives need a policy decision, see §10);
- rows you excluded yourself, such as confirmed duplicates.

## 5. Mapping TimeSolv columns

Column names differ between TimeSolv reports, so the app never assumes them. The
import wizard works like this:

1. Upload a CSV or XLSX file. For XLSX, pick the worksheet. If the report has title
   lines above the headings, set the **header row**.
2. Check the **preview**.
3. Review the **suggested mapping**. Each logical field (marked `*` if required) is
   matched to a source column using synonyms and fuzzy matching. Correct any field
   with its dropdown.
4. Read the **validation** results. Bad dates, non-numeric amounts, blank required
   values and allocations that exceed the payment are reported by spreadsheet row
   number. The import is blocked unless you tick *Skip invalid rows*.
5. Click **Import**. The mapping is saved under the file's header signature, so the
   next export with the same headings is mapped automatically.

Audit and de-duplication:

- The original file is stored in SQLite with its SHA-256 hash. You can download it
  again from *Import history*.
- Each row's original values are kept as JSON.
- Re-importing a row that already exists (same payment ID + invoice + matter +
  amounts, or same time-entry ID) is skipped. Overlapping exports never
  double-count.
- A payment row that repeats **inside the same file** is imported as *excluded*
  and flagged as a possible duplicate. If it really is a separate payment, include
  it on *Imports → Review collections* and record a reason.

## 6. How associate hours are attributed

Associates and staff never receive compensation directly. Each non-partner time
entry is credited to one or more partners. The first level that applies wins:

1. **Time-entry override** (*Supervisory Mappings → Time-entry overrides*). Can be
   split. A reason is required.
2. **Associate–matter mapping**: timekeeper, matter, credited partner, effective
   start and end dates, allocation %, notes and source. Several rows for the same
   timekeeper, matter and dates split the hours, and their percentages must total
   100%.
3. The matter's **compensation supervising partner** (*Firm Setup → Matters*).
4. The matter's TimeSolv **Responsible Professional**, but only if that person is a
   partner.
5. The timekeeper's **default supervising partner**.
6. **Unassigned.** The hours are not given to anyone. They create a *blocking*
   exception until you add a mapping or exclude them (*Supervisory Mappings →
   Unassigned hours*).

Example (as in the demo data):

```
Daniel Smith   | M-1001 | David Okafor    | 100%
Daniel Smith   | M-1004 | Elena Petrova   | 100%   (overrides his default supervisor, Okafor)
Rachel Jones   | M-1007 | Michael Brennan |  60%
Rachel Jones   | M-1007 | Aisha Mohammed  |  40%
```

A partner's own hours always credit that partner. Paralegal and other-staff hours
are attributed the same way, but only when *Include paralegal / other staff hours*
is on. The *Attribution explorer* shows, for every entry, the partner it credits
and the hierarchy level that decided it.

## 7. Compensation formulas

All money is calculated with Python `Decimal`. Whenever a dollar amount is
divided (pools, partner shares, per-collection credits), it is split with the
**largest-remainder method**: every part is rounded down to the cent, and the
leftover cents go to the largest remainders. The parts therefore always add up
exactly to the whole, with no floating-point drift.

**Qualifying measure per time entry**, which depends on the methodology:

| Methodology | Measure |
|---|---|
| Billed hours (default) | Hours billed on billed entries (unbilled = 0) |
| Recorded billable hours | Recorded billable hours, less write-downs unless written-off hours are included |
| Billed value | Billed amount (or hours × rate if the amount is missing) |
| Manual percentages | Per-matter partner percentages entered in *Firm Setup → Manual working shares* |

Nonbillable and written-off time counts only when the matching policy switch is on.

**Per collection** (fees = the amount allocated to professional fees):

```
Origination portion          = Fees × 30%
Partner origination credit   = Origination portion × originator share      (shares per matter total 100%)
Working portion              = Fees × 70%
Partner working share        = partner credited measure ÷ total credited measure
Partner working credit       = Working portion × partner working share
```

The working-share basis is chosen in this order:

1. **Invoice-level.** Use the time entries on the paid invoice (same matter and
   invoice ID).
2. **Matter-level fallback.** If no entries match the invoice, use the qualifying
   entries on the matter that fall in the compensation period. Entries already
   tied to another invoice that was matched at invoice level are left out, so
   hours are not counted twice.

Every working-credit row is labelled with its method. The dashboard counts the
fallback collections.

Origination and working overlap:

- When *Originator eligible for working credit* is **Yes** (the default), the
  originator also shares in the 70% working portion, but only through hours they
  worked or supervised. Origination alone earns no working credit.
- When it is **No**, every measure attributed to the matter's originators is
  removed from the denominator, and the 70% is reallocated among the other
  eligible partners.

**EWYK:**

```
Total EWYK credit       = Origination credit + Working credit
                          (columns: own-hour, supervised-associate-hour, other-delegated-hour working credit)
EWYK performance share  = Partner total EWYK credit ÷ Total EWYK credit of ACTIVE partners
EWYK compensation       = Pool × 80% × EWYK performance share
```

**Equal share:** `Equal compensation = Pool × 20% ÷ number of active partners`

**Total:** `Total compensation = Equal + EWYK`. The total paid equals
the pool to the cent, and the *Reconciliation* table proves it. Inactive partners
receive $0. Any credit attributed to them is shown, but it is excluded from the
EWYK denominator.

## 8. Resolving exceptions

*Audit & Exceptions* lists every item with its severity and a "How to resolve"
column. **Blocking** items prevent finalization.

| Exception | Where to fix it |
|---|---|
| Unassigned associate hours | Supervisory Mappings → Unassigned hours: assign partner(s) or expressly exclude |
| Missing originating partners / originator allocations not totaling 100% | Firm Setup → Matters & originators |
| Working allocations not totaling 100% | Supervisory Mappings → mappings / overrides |
| Unknown timekeepers | Firm Setup → Timekeepers (add the person and their category) |
| Duplicate payments | Imports → Review collections |
| Missing matter IDs | Correct the export and re-import, or exclude the row |
| Roster problems (blank or duplicate names, no active partners) | Firm Setup → Partner roster |
| Compensation reconciliation differences | Usually caused by one of the items above |
| Collections without qualifying hours (warning) | Check invoice IDs and time import, or use manual percentages |
| Hours without corresponding matters (warning) | Import or add the matter |

The demo data deliberately leaves **Owen Fletcher's hours on M-1011 unassigned**.
That matter has no supervising partner, its Responsible Professional is an
associate, and Fletcher has no default supervisor. Resolve it on *Supervisory
Mappings* to see finalization become available.

## 9. Finalizing a year and exporting

**Excel report.** Use *Compensation Results → Download Excel report*. The workbook
has 12 sheets:

1. Executive Summary
2. Policy Inputs
3. Partner Compensation
4. EWYK Detail
5. Origination Detail
6. Working Credit Detail
7. Associate-Matter Mappings
8. TimeSolv Collections
9. TimeSolv Time Entries
10. Exceptions
11. Reconciliation
12. Audit Log

How the workbook is built:

- Inputs are blue text on yellow fill. Formulas are on a green fill. Values
  calculated by the app have no fill.
- The policy cells are named (`Pool`, `EqualPct`, `EWYKPct`, `OrigPct`,
  `WorkPct`). The formula columns (shares, compensation, totals,
  reconciliation) recalculate in Excel when an input changes.
- Each formula column sits next to the app's cent-exact value, with a
  rounding-difference column.
- Headers are frozen, every table has filters, money and percentage formats are
  applied, column widths fit their contents, and exceptions and failed checks
  are colour-highlighted.

**Finalizing.** On *Finalize & History*, once no blocking exceptions remain,
confirm and click **Finalize year**. The snapshot stores:

- imported file hashes and their column mappings;
- policy assumptions;
- the roster, timekeepers and matters;
- matter originators;
- supervisory mappings, entry overrides and exclusions;
- the calculated results;
- the audit log;
- the finalization timestamp.

The original files stay in the database. A finalized year is **read-only**. The
data layer refuses every write, not just the UI. To reopen it, you need
`COMP_ADMIN_PASSWORD` and a written reason, and the reopen is audited. You can
download snapshots as JSON and compare them year over year.

## 10. Policy decisions the firm must formally approve

The app makes every one of these visible and editable, but the firm should adopt
each one in writing:

1. **The distributable pool** and the 20/80 split. Also, what is deducted
   before the pool (draws, reserves, benefits).
2. **The 30/70 origination/working split**, and whether originators may also earn
   working credit (default Yes).
3. **Who is the originator of each matter.** This includes how split origination
   is documented and approved. The Responsible Professional is *not* assumed to
   be the originator. The one-click "use Responsible Professional" action is
   optional and audited.
4. **Working-share methodology.** Billed hours is the default. The alternatives
   are recorded billable hours, billed value and manual percentages. The firm also
   decides whether nonbillable, written-off and paralegal/staff hours count.
5. **The supervisory-attribution hierarchy**, and who may enter matter mappings
   and entry-level overrides.
6. **Treatment of unassigned hours.** They block finalization until they are
   assigned or expressly excluded.
7. **Collections with no qualifying hours.** Their 70% working credit stays
   unallocated (a warning). Since EWYK is a relative share, unallocated credit
   simply drops out. The firm may prefer another rule, such as crediting the
   originator.
8. **Refunds, reversals and negative fee allocations.** These are currently
   excluded for review rather than netted against credit.
9. **Payments received after year-end for prior-year work.** The default credits
   a payment in the year it is *collected*, based on the collection date and the
   compensation period.
10. **Credit attributed to inactive (departed) partners.** It is reported but
    earns nothing. It is *not* reallocated to the remaining partners.
11. **If no EWYK credit exists at all,** the EWYK pool cannot be allocated, and the
    year is blocked until data is available.
12. **Cent rounding.** The largest-remainder method, with ties going to the
    partner listed first in the roster.
13. **Partners who join or leave mid-year.** The equal share is not prorated;
    each active partner receives a full equal share.
14. **Who holds the administrator password** to reopen finalized years.

## 11. Limitations of this local version

- One shared SQLite file. User names are typed in, not authenticated, so this is
  meant for use by trusted staff on one machine.
- The app does not prorate partial-year partners or partners who change status
  mid-year.
- TimeSolv data must be exported by hand. The app does not call the TimeSolv API.
