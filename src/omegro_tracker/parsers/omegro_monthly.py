"""Parse the Omegro monthly NR / OPEX / EBITA reporting pack.

This is where the group's actual financials live — not in the BU Progress
Report folder, which is not yet populated. Finance produces one workbook per
period covering every Omegro VBU, grouped by portfolio and then by group. Our
three units sit under the row band "David Turner Group".

Sheet shape (all three sheets share it, EBITA has one extra column):

    A/B  label columns (group or VBU name)
    C..  QUARTER TO DATE:  Actuals | Forecast | Variance [| extra] | Variance % | FX Impact
         PROJECTION:       Updated Forecast | Forecast | Variance | Variance % | FX Impact
    then free-text commentary columns

"Updated Forecast" in the projection band is the business's current best
estimate for the full quarter — the outturn — and "Forecast" beside it is the
approved QSR. Naming them that way round is a quirk of the template, so the
column offsets below are anchored on the band headers rather than assumed.

Everything is reported in the group reporting currency (USD) in full units;
facts are stored in thousands to match the "$'000" convention the portfolio
templates use.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ..model import Commentary, Fact

# Sheet name -> metric key. The pack names them inconsistently across periods
# ("Opex", "OPEX Before Bonus"), so match on a prefix.
SHEET_METRICS = {
    "nr": "net_revenue",
    "netrevenue": "net_revenue",
    "opex": "opex",
    "ebita": "ebita",
    "ebitda": "ebita",
}

_NUM = re.compile(r"^\(?\s*-?[\d,]+(?:\.\d+)?\s*\)?$")


def _squash(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _number(value: Any) -> float | None:
    """Accounting-negative parentheses, thousands separators, blanks."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("£", "")
    if not text or text in {"-", "—", "N/A", "n/a"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _metric_for_sheet(name: str) -> str | None:
    squashed = _squash(name)
    for prefix, metric in SHEET_METRICS.items():
        if squashed.startswith(prefix):
            return metric
    return None


def _alias_index(units: Iterable[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for unit in units:
        for alias in [unit["name"], unit.get("short_name", ""), *unit.get("aliases", [])]:
            if alias:
                index[_squash(alias)] = unit["key"]
    return index


def _find_bands(sheet: Any, scan_rows: int = 20, scan_cols: int = 30) -> dict[str, int] | None:
    """Locate the first column of the QTD and projection bands by header text."""
    bands: dict[str, int] = {}
    for r in range(1, scan_rows + 1):
        for c in range(1, scan_cols + 1):
            head = _squash(sheet.cell(row=r, column=c).value)
            if not head:
                continue
            if "quartertodate" in head or head.endswith("qtd"):
                bands.setdefault("qtd", c)
            elif "projection" in head:
                bands.setdefault("projection", c)
        if len(bands) == 2:
            return bands
    return bands if "qtd" in bands else None


def _offsets(sheet: Any, start_col: int, scan_rows: int = 20) -> dict[str, int]:
    """Within a band, find which column holds actuals and which the forecast.

    The EBITA sheet carries an extra column between variance and variance %,
    so the forecast is not always at a fixed offset from the band start.
    """
    for r in range(1, scan_rows + 1):
        labels = {
            _squash(sheet.cell(row=r, column=start_col + i).value): start_col + i
            for i in range(0, 6)
        }
        actual = labels.get("actuals") or labels.get("actual") or labels.get("updatedforecast")
        forecast = labels.get("forecast")
        if actual and forecast:
            return {"actual": actual, "forecast": forecast}
    # Template default: actuals then forecast.
    return {"actual": start_col, "forecast": start_col + 1}


def _commentary_cells(sheet: Any, row: int, after_col: int, min_length: int = 60) -> list[str]:
    """Free-text variance explanations sit to the right of the numeric bands."""
    out: list[str] = []
    for c in range(after_col, after_col + 25):
        value = sheet.cell(row=row, column=c).value
        if isinstance(value, str) and len(value.strip()) >= min_length:
            out.append(re.sub(r"\s+", " ", value.strip()))
    return out


def parse(
    path: str | Path,
    *,
    units: list[dict[str, Any]],
    period: str,
    quarter: str,
    source: str = "omegro_monthly",
    scale: float = 1000.0,
) -> tuple[list[Fact], list[Commentary]]:
    """Returns (facts, commentary).

    `scale` divides the raw figures; the pack reports full currency units and
    the dashboard works in thousands.
    """
    workbook = load_workbook(path, data_only=True, read_only=True)
    aliases = _alias_index(units)

    facts: list[Fact] = []
    commentary: list[Commentary] = []

    for name in workbook.sheetnames:
        metric = _metric_for_sheet(name)
        if metric is None:
            continue
        sheet = workbook[name]
        bands = _find_bands(sheet)
        if not bands:
            continue

        qtd = _offsets(sheet, bands["qtd"])
        projection = _offsets(sheet, bands["projection"]) if "projection" in bands else None
        rightmost = max(
            [*qtd.values(), *(projection.values() if projection else [])]
        )

        for r in range(1, (sheet.max_row or 0) + 1):
            label = None
            for c in (1, 2, 3, 4, 5):
                candidate = sheet.cell(row=r, column=c).value
                if isinstance(candidate, str) and _squash(candidate) in aliases:
                    label = candidate
                    break
            if label is None:
                continue
            bu = aliases[_squash(label)]

            def emit(basis: str, fact_period: str, cols: dict[str, int], measure_for_actual: str) -> None:
                for key, measure in (("forecast", "forecast"), ("actual", measure_for_actual)):
                    raw = _number(sheet.cell(row=r, column=cols[key]).value)  # noqa: B023
                    facts.append(
                        Fact(
                            bu=bu,  # noqa: B023
                            metric=metric,  # noqa: B023
                            basis=basis,  # type: ignore[arg-type]
                            measure=measure,  # type: ignore[arg-type]
                            period=fact_period,
                            value=None if raw is None else raw / scale,
                            unit="k",
                            source=source,
                        )
                    )

            emit("qtd", period, qtd, "actual")
            if projection:
                emit("quarter", quarter, projection, "projection")

            for text in _commentary_cells(sheet, r, rightmost + 1):
                commentary.append(
                    Commentary(bu=bu, metric=metric, period=period, text=text, source=source)
                )

    workbook.close()
    return facts, commentary


def derive_margin(facts: list[Fact]) -> list[Fact]:
    """EBITA % is not reported in the pack; compute it from the pair.

    A margin is a ratio of two sums, so it is computed per (bu, basis, measure)
    rather than averaged from anywhere else.
    """
    index: dict[tuple[str, str, str, str], float] = {}
    for f in facts:
        if f.metric in {"net_revenue", "ebita"} and f.value is not None:
            index[(f.bu, f.metric, f.basis, f.measure)] = f.value

    out: list[Fact] = []
    seen: set[tuple[str, str, str]] = set()
    for (bu, metric, basis, measure) in index:
        if metric != "ebita" or (bu, basis, measure) in seen:
            continue
        nr = index.get((bu, "net_revenue", basis, measure))
        ebita = index.get((bu, "ebita", basis, measure))
        if not nr or ebita is None:
            continue
        seen.add((bu, basis, measure))
        period = next(
            f.period for f in facts
            if (f.bu, f.metric, f.basis, f.measure) == (bu, "ebita", basis, measure)
        )
        out.append(
            Fact(
                bu=bu,
                metric="ebita_pct",
                basis=basis,  # type: ignore[arg-type]
                measure=measure,  # type: ignore[arg-type]
                period=period,
                value=ebita / nr,
                unit="pct",
                source="derived",
            )
        )
    return out
