"""Parse a completed Nelson Portfolio Business Unit Progress Report.

Workbook shape (from "Monthly Review Template.xlsx"):

  "P&L Progress Report"
      row 2-3   header block: BU name, OG stage, expected exit, period, status
      row 5-6   executive summary
      row 11+   improvement plan / IM value driver rows
      row 20    band header: QUARTER TO DATE | THIS QUARTER | NEXT QUARTER
      row 21-28 metric rows, columns:
                  A metric  B fcst  C actual  D var  E var%
                  F fcst    G projection  H var  I var%   J next-qtr fcst

  "Working Capital Progress Report"
      row 21-27 metric rows, columns:
                  A metric  B-D previous month (target/actual/var)
                  E-G current month (target/actual/var)  H next-month target

  "P&L Progress Report (Quarterly)"
      as P&L, but the first band is THIS YEAR (baseline vs projection) and the
      sheet adds operating ratios and locked-in revenue blocks.

Variance columns are formulas in the source and are recomputed here rather than
read, so a stale cached calculation in the uploaded workbook cannot propagate.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ..model import Fact, Governance, Initiative, month_label, next_quarter, quarter_of

# Source label -> internal metric key. Matching is done on a squashed lowercase
# form so "EBITA %", "EBITA%" and "Ebita %" all land in the same place.
METRIC_ALIASES: dict[str, str] = {
    "netrevenue": "net_revenue",
    "nr": "net_revenue",
    "ebita": "ebita",
    "ebita%": "ebita_pct",
    "og%": "og_pct",
    "organicgrowth%": "og_pct",
    "bqr%": "bqr_pct",
    "workingcapital%": "wc_pct",
    "wc%": "wc_pct",
    "wc%(excl.cash)": "wc_pct_ex_cash",
    "wc%(excl.trappedcash)": "wc_pct_ex_trapped_cash",
    "bookings": "bookings",
    "pipeline": "pipeline",
    "overduear%": "overdue_ar_pct",
    "%ofwipover30days": "wip_over_30_pct",
    "wip>30days%": "wip_over_30_pct",
}

PCT_METRICS = {
    "ebita_pct",
    "og_pct",
    "bqr_pct",
    "wc_pct",
    "wc_pct_ex_cash",
    "wc_pct_ex_trapped_cash",
    "overdue_ar_pct",
    "wip_over_30_pct",
}

STATUS_ALIASES = {
    "on track": "on_track",
    "at risk": "at_risk",
    "off track": "off_track",
}

_PLACEHOLDER = re.compile(r"^\s*\[.*\]\s*$|^\s*(tbc|tbd|n/?a|-{1,2})\s*$", re.I)


def _squash(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _clean(value: Any) -> str:
    """Template placeholders like "[Enter Name]" are not content."""
    text = str(value or "").strip()
    return "" if not text or _PLACEHOLDER.match(text) else text


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("£", "").replace("$", "")
    if not text or _PLACEHOLDER.match(text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    pct = text.endswith("%")
    text = text.rstrip("%")
    try:
        number = float(text)
    except ValueError:
        return None
    if pct:
        number /= 100.0
    return -number if negative else number


def _status(value: Any) -> str:
    return STATUS_ALIASES.get(str(value or "").strip().lower(), "unknown")


def _date(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    text = _clean(value)
    if not text:
        return None
    # The template asks for DD/MM/YY.
    if m := re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", text):
        d, mo, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return text
    return text


def _find_metric_rows(sheet: Any, max_row: int = 60) -> list[tuple[int, str]]:
    """Locate the metric block by matching column A against the alias table,
    rather than trusting fixed row numbers — BUs add KPI rows."""
    rows: list[tuple[int, str]] = []
    for r in range(1, max_row + 1):
        key = METRIC_ALIASES.get(_squash(sheet.cell(row=r, column=1).value))
        if key:
            rows.append((r, key))
    return rows


def _unit_for(metric: str, currency: str) -> str:
    return "pct" if metric in PCT_METRICS else f"k{currency}"


def _header(sheet: Any) -> dict[str, str]:
    """Read the header block by label rather than position."""
    out: dict[str, str] = {}
    labels = {
        "businessunit:": "business_unit",
        "operationalgovernancestage:": "og_stage",
        "overallstatus:": "overall_status",
        "expecteddatetoexitstage:": "expected_exit",
        "expecteddatetotargetwc%:": "expected_exit",
        "period:": "period",
        "reportdate:": "report_date",
    }
    for r in range(1, 8):
        for c in range(1, 11):
            key = labels.get(_squash(sheet.cell(row=r, column=c).value))
            if not key:
                continue
            # The value sits in the next non-empty cell to the right.
            for offset in range(1, 4):
                raw = sheet.cell(row=r, column=c + offset).value
                if isinstance(raw, date):
                    out[key] = raw.isoformat()
                    break
                if _clean(raw):
                    out[key] = _clean(raw)
                    break
    return out


def _initiatives(sheet: Any, bu: str, kind: str, max_row: int = 20) -> list[Initiative]:
    """Rows between the plan header and the financials block."""
    out: list[Initiative] = []
    start = None
    for r in range(1, max_row + 1):
        head = _squash(sheet.cell(row=r, column=1).value)
        if head in {"initiative/valuedriver&owner", "balancesheetaccount&owner"}:
            start = r + 1
            break
    if start is None:
        return out

    for r in range(start, max_row + 1):
        title = _clean(sheet.cell(row=r, column=1).value)
        if not title or _squash(title) == "keyfinancialsvsforecast":
            break
        status = _status(sheet.cell(row=r, column=6).value)
        notes = _clean(sheet.cell(row=r, column=7).value)
        measure = _clean(sheet.cell(row=r, column=2).value)
        # Skip untouched template rows ("Initiative 3" with a [Description]).
        if not measure and not notes and re.match(r"^(initiative|action)\s*\d+$", title, re.I):
            continue
        owner = ""
        if m := re.search(r"\(([A-Z]{2,4})\)\s*$", title):
            owner, title = m.group(1), title[: m.start()].strip()
        out.append(
            Initiative(
                bu=bu,
                title=title,
                owner=owner,
                measure_of_success=measure,
                target_date=_date(sheet.cell(row=r, column=5).value),
                status=status,  # type: ignore[arg-type]
                notes=notes,
                kind=kind,  # type: ignore[arg-type]
            )
        )
    return out


def _pnl_facts(
    sheet: Any, bu: str, period: str, currency: str, source: str, quarterly: bool
) -> list[Fact]:
    """Metric block: QTD (or FY) | this quarter | next quarter."""
    quarter = quarter_of(period)
    facts: list[Fact] = []

    # First band is QTD on the monthly sheet and the full year on the quarterly
    # one, where the "forecast" column is the approved baseline.
    if quarterly:
        first_basis, first_period = "year", period.split("-")[0]
        first_measures = ("baseline", "projection")
    else:
        first_basis, first_period = "qtd", period
        first_measures = ("forecast", "actual")

    bands = (
        (first_basis, first_period, 2, 3, first_measures),
        ("quarter", quarter, 6, 7, ("forecast", "projection")),
        ("next_quarter", next_quarter(quarter), 10, None, ("forecast", None)),
    )

    for row, metric in _find_metric_rows(sheet):
        unit = _unit_for(metric, currency)
        for basis, band_period, col_a, col_b, measures in bands:
            for col, measure in ((col_a, measures[0]), (col_b, measures[1])):
                if col is None or measure is None:
                    continue
                facts.append(
                    Fact(
                        bu=bu,
                        metric=metric,
                        basis=basis,  # type: ignore[arg-type]
                        measure=measure,  # type: ignore[arg-type]
                        period=band_period,
                        value=_number(sheet.cell(row=row, column=col).value),
                        unit=unit,
                        source=source,
                    )
                )
    return facts


def _wc_facts(sheet: Any, bu: str, period: str, currency: str, source: str) -> list[Fact]:
    """Working capital block: previous month | current month | next month."""
    from ..model import prior_month

    year, month = (int(x) for x in period.split("-"))
    nxt = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"

    bands = (
        (prior_month(period), 2, 3),   # target, actual
        (period, 5, 6),
        (nxt, 8, None),
    )

    facts: list[Fact] = []
    for row, metric in _find_metric_rows(sheet):
        unit = _unit_for(metric, currency)
        for band_period, target_col, actual_col in bands:
            pairs = [(target_col, "target")]
            if actual_col:
                pairs.append((actual_col, "actual"))
            for col, measure in pairs:
                facts.append(
                    Fact(
                        bu=bu,
                        metric=metric,
                        basis="month",
                        measure=measure,  # type: ignore[arg-type]
                        period=band_period,
                        value=_number(sheet.cell(row=row, column=col).value),
                        unit=unit,
                        source=source,
                    )
                )
    return facts


def parse(
    path: str | Path,
    *,
    bu: str,
    period: str,
    currency: str = "GBP",
    source: str = "bu_monthly_submissions",
) -> tuple[list[Fact], list[Initiative], Governance | None]:
    """Parse one submitted workbook.

    `data_only=True` reads the cached values rather than the formulas, which is
    what we want for actuals — but it means a workbook saved by a tool that
    does not write a formula cache comes back empty, so the variance columns are
    recomputed downstream instead of trusted.
    """
    workbook = load_workbook(path, data_only=True, read_only=True)

    facts: list[Fact] = []
    initiatives: list[Initiative] = []
    governance: Governance | None = None

    for name in workbook.sheetnames:
        sheet = workbook[name]
        squashed = _squash(name)

        if "p&lprogressreport" in squashed or "plprogressreport" in squashed:
            quarterly = "quarterly" in squashed
            facts += _pnl_facts(sheet, bu, period, currency, source, quarterly)
            if not quarterly:
                initiatives += _initiatives(sheet, bu, "improvement")
                head = _header(sheet)
                governance = Governance(
                    bu=bu,
                    period=quarter_of(period),
                    stage=head.get("og_stage"),
                    expected_exit=head.get("expected_exit"),
                    overall_status=_status(head.get("overall_status")),  # type: ignore[arg-type]
                )
        elif "workingcapital" in squashed:
            facts += _wc_facts(sheet, bu, period, currency, source)
            initiatives += _initiatives(sheet, bu, "working_capital")

    workbook.close()
    return facts, initiatives, governance


def expected_filename(bu_alias: str, period: str) -> str:
    """The naming the portfolio asks for, e.g. "TBL - Aug-26 - Monthly Review"."""
    return f"{bu_alias} - {month_label(period)} - Monthly Review.xlsx"


def match_period(name: str, period: str) -> bool:
    """Does a filename refer to this reporting month? Accepts "Aug-26",
    "Aug 26", "2026-08" and "Aug2026"."""
    label = month_label(period)          # Aug-26
    mon, yy = label.split("-")
    year = period.split("-")[0]
    squashed = _squash(name)
    return any(
        _squash(c) in squashed
        for c in (label, f"{mon} {yy}", f"{mon}{year}", f"{mon} {year}", period)
    )


def pick_latest(names: Iterable[str], period: str) -> str | None:
    matches = [n for n in names if match_period(n, period)]
    return sorted(matches)[-1] if matches else None
