"""Build the HTML dashboard from a Snapshot.

`render()` takes a Snapshot and the config and returns a complete HTML page.
All geometry for the inline SVG charts is computed here so the template stays
declarative — the template never does arithmetic.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import derive
from .derive import Variance
from .model import (
    STATUS_LABELS,
    Snapshot,
    month_label,
    months_in_quarter,
    next_quarter,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

# Status -> the token name used in the stylesheet. Kept out of the template so
# a palette change is a one-line edit here.
STATUS_TOKEN = {
    "on_track": "good",
    "at_risk": "warning",
    "off_track": "critical",
    "unknown": "muted",
}

# A glyph accompanies every status colour so state never rests on hue alone.
STATUS_GLYPH = {"on_track": "●", "at_risk": "▲", "off_track": "■", "unknown": "–"}

POSTURE_TOKEN = {
    "Strong": "good",
    "Moderate": "warning",
    "Needs Improvement": "serious",
    "Critical": "critical",
}

KEY_AREA_GLYPH = {"good": "●", "warning": "▲", "critical": "■", "unknown": "–"}


# -- formatting -------------------------------------------------------------


def fmt_money(value: float | None, currency: str = "GBP") -> str:
    if value is None:
        return "—"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    if abs(value) >= 1000:
        return f"{symbol}{value / 1000:,.2f}m"
    return f"{symbol}{value:,.0f}k"


def fmt_pct(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{places}f}%"


def fmt_value(value: float | None, unit: str, currency: str = "GBP") -> str:
    return fmt_pct(value) if unit == "pct" else fmt_money(value, currency)


def fmt_delta(v: Variance, currency: str = "GBP") -> str:
    """Absolute movement. Percentage-point metrics read in points, not as a
    percentage of a percentage."""
    if v.delta is None:
        return "—"
    if v.unit == "pct":
        return f"{v.delta * 100:+.1f}pt"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{v.delta:+,.0f}k" if abs(v.delta) < 1000 else f"{symbol}{v.delta / 1000:+,.2f}m"


def fmt_delta_pct(v: Variance) -> str:
    return "—" if v.delta_pct is None else f"{v.delta_pct * 100:+.1f}%"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return iso


def fmt_stamp(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return iso


# -- chart geometry ---------------------------------------------------------


@dataclass
class Bar:
    x: float
    y: float
    width: float
    height: float
    label: str
    value_label: str
    token: str


@dataclass
class TrajectoryChart:
    """Forecast vs actual by month across the quarter.

    Forecast is drawn as an open track and actual as a solid bar inside it, so
    the gap between them *is* the variance — no second scale, no second axis.
    """

    width: int
    height: int
    bars: list[Bar]
    ticks: list[dict[str, Any]]
    baseline_y: float
    unit_label: str


def trajectory(
    snapshot: Snapshot,
    bu: str,
    metric: str,
    *,
    currency: str = "GBP",
    width: int = 268,
    height: int = 128,
) -> TrajectoryChart | None:
    months = months_in_quarter(snapshot.quarter)
    rows = []
    for m in months:
        forecast = snapshot.value(bu, metric, "month", "forecast", m)
        actual = snapshot.value(bu, metric, "month", "actual", m)
        if forecast is None and actual is None:
            continue
        rows.append((m, forecast, actual))
    if not rows:
        return None

    pad_left, pad_right, pad_top, pad_bottom = 4, 4, 18, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    baseline_y = pad_top + plot_h

    peak = max(v for _, f, a in rows for v in (f, a) if v is not None)
    scale = (plot_h / peak) if peak else 0.0

    slot = plot_w / len(rows)
    bar_w = min(30.0, slot * 0.34)
    gap = 3.0

    bars: list[Bar] = []
    ticks: list[dict[str, Any]] = []
    for index, (month, forecast, actual) in enumerate(rows):
        centre = pad_left + slot * (index + 0.5)
        ticks.append({"x": centre, "y": height - 7, "label": month_label(month)})

        pair = [("forecast", forecast), ("actual", actual)]
        present = [p for p in pair if p[1] is not None]
        total_w = len(present) * bar_w + (len(present) - 1) * gap
        start = centre - total_w / 2

        for slot_index, (kind, value) in enumerate(present):
            h = max(1.5, (value or 0) * scale)
            bars.append(
                Bar(
                    x=start + slot_index * (bar_w + gap),
                    y=baseline_y - h,
                    width=bar_w,
                    height=h,
                    label=f"{month_label(month)} {kind}",
                    value_label=fmt_value(value, "pct" if metric.endswith("_pct") else "k", currency),
                    token="forecast" if kind == "forecast" else "actual",
                )
            )

    return TrajectoryChart(
        width=width,
        height=height,
        bars=bars,
        ticks=ticks,
        baseline_y=baseline_y,
        unit_label=f"k{currency}",
    )


@dataclass
class RiskRail:
    """ITDS residual risk on the fixed 0-400 scale.

    Absolute, not relative: the whole point of the measure is that 400 is the
    ceiling for every business, so the track is always full width and the bars
    are directly comparable between units.
    """

    width: int
    height: int
    track_x: float
    track_w: float
    fill_w: float
    initial_x: float | None
    initial_label: str
    token: str
    gridlines: list[dict[str, Any]]


def risk_rail(itds: Any, *, width: int = 300, height: int = 44) -> RiskRail | None:
    if itds is None or itds.risk_current is None:
        return None

    track_x, track_w = 0.0, float(width)
    scale = track_w / (itds.risk_max or 400)

    gridlines = [
        {"x": track_x + step * scale, "label": str(step)}
        for step in (0, 100, 200, 300, 400)
        if (itds.risk_max or 400) >= step
    ]

    initial_x = None
    if itds.risk_initial is not None:
        initial_x = track_x + itds.risk_initial * scale

    return RiskRail(
        width=width,
        height=height,
        track_x=track_x,
        track_w=track_w,
        fill_w=max(2.0, itds.risk_current * scale),
        initial_x=initial_x,
        initial_label=f"baseline {itds.risk_initial:.0f}" if itds.risk_initial is not None else "",
        token=POSTURE_TOKEN.get(itds.posture or "", "muted"),
        gridlines=gridlines,
    )


@dataclass
class ControlBar:
    segments: list[dict[str, Any]]
    total: int


def control_bar(itds: Any, *, width: int = 300) -> ControlBar | None:
    """Controls at 7+ / below 7 / of which 0-3.

    The three counts are not disjoint in the source — "low" is a subset of
    "focus" — so the bar is drawn as strong + (focus - low) + low, which does
    sum to the assessed population.
    """
    if itds is None or not itds.controls_assessed:
        return None
    strong = itds.controls_strong or 0
    low = itds.controls_low or 0
    focus = max((itds.controls_focus or 0) - low, 0)
    total = strong + focus + low
    if not total:
        return None

    segments = []
    x = 0.0
    for count, token, label in (
        (strong, "good", "at 7 or above"),
        (focus, "warning", "below 7"),
        (low, "critical", "in the 0–3 range"),
    ):
        if not count:
            continue
        w = width * count / total
        segments.append({"x": x, "width": w, "token": token, "count": count, "label": label})
        x += w
    return ControlBar(segments=segments, total=total)


# -- view model -------------------------------------------------------------


def _metric_row(
    snapshot: Snapshot,
    bu: str,
    metric: str,
    config: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    cfg = config.get("metrics", {}).get(metric, {})
    unit = "pct" if cfg.get("unit") == "pct" else "k"
    band = derive.bands(config)

    qtd = derive.variance(snapshot, bu, metric, "qtd", config=config)
    ytd = derive.variance(snapshot, bu, metric, "ytd", config=config)
    quarter = derive.variance(
        snapshot, bu, metric, "quarter",
        period=snapshot.quarter, value="projection", config=config,
    )
    nxt = snapshot.value(bu, metric, "next_quarter", "forecast", next_quarter(snapshot.quarter))
    month = derive.variance(snapshot, bu, metric, "month", period=snapshot.period, config=config)

    def cell(v: Variance) -> dict[str, Any]:
        status = v.status(band)
        return {
            "reference": fmt_value(v.reference, unit, currency),
            "value": fmt_value(v.value, unit, currency),
            "delta": fmt_delta(v, currency),
            "delta_pct": fmt_delta_pct(v),
            # A margin that moves from 23.1% to 20.4% has not fallen by 12%;
            # it has fallen 2.7 points. Percentage-point metrics show the point
            # movement, money metrics show the relative variance.
            "display": fmt_delta(v, currency) if unit == "pct" else fmt_delta_pct(v),
            "status": status,
            "token": STATUS_TOKEN[status],
            "glyph": STATUS_GLYPH[status],
            "reported": v.reported,
        }

    return {
        "key": metric,
        "label": SHORT_LABELS.get(metric, cfg.get("label", metric.replace("_", " ").title())),
        "unit": unit,
        "month": cell(month),
        "qtd": cell(qtd),
        "ytd": cell(ytd),
        "outturn": cell(quarter),
        "next": fmt_value(nxt, unit, currency),
    }


def _wc_row(
    snapshot: Snapshot, bu: str, metric: str, config: dict[str, Any], currency: str
) -> dict[str, Any]:
    from .model import prior_month

    cfg = config.get("metrics", {}).get(metric, {})
    unit = "pct" if cfg.get("unit") == "pct" else "k"
    band = derive.bands(config)
    nxt_month = months_in_quarter(snapshot.quarter)
    following = next(
        (m for m in nxt_month if m > snapshot.period),
        None,
    )

    def cell(period: str) -> dict[str, Any]:
        v = derive.variance(
            snapshot, bu, metric, "month", period=period,
            reference="target", value="actual", config=config,
        )
        status = v.status(band)
        return {
            "period": month_label(period),
            "reference": fmt_value(v.reference, unit, currency),
            "value": fmt_value(v.value, unit, currency),
            "delta": fmt_delta(v, currency),
            "status": status,
            "token": STATUS_TOKEN[status],
            "glyph": STATUS_GLYPH[status],
            "reported": v.reported,
        }

    target_next = snapshot.value(bu, metric, "month", "target", following) if following else None

    return {
        "key": metric,
        "label": SHORT_LABELS.get(metric, cfg.get("label", metric.replace("_", " ").title())),
        "previous": cell(prior_month(snapshot.period)),
        "current": cell(snapshot.period),
        "next_label": month_label(following) if following else "—",
        "next": fmt_value(target_next, unit, currency),
    }


PNL_METRICS = ["net_revenue", "ebita", "ebita_pct", "og_pct", "bqr_pct"]
WC_METRICS = ["wc_pct", "wc_pct_ex_cash", "overdue_ar_pct", "wip_over_30_pct"]

# The config labels are the ones the portfolio templates use and are kept for
# reporting; the table needs names that survive a narrow first column.
SHORT_LABELS = {
    "net_revenue": "Net revenue",
    "ebita": "EBITA",
    "ebita_pct": "EBITA margin",
    "og_pct": "Organic growth",
    "bqr_pct": "BQR",
    "wc_pct": "Working capital",
    "wc_pct_ex_cash": "WC excl. cash",
    "overdue_ar_pct": "Overdue AR",
    "wip_over_30_pct": "WIP over 30 days",
}


def build_view(snapshot: Snapshot, config: dict[str, Any]) -> dict[str, Any]:
    currency = config.get("group", {}).get("reporting_currency", "GBP")
    summaries = [derive.bu_summary(snapshot, u, config) for u in snapshot.business_units]
    rollup = derive.portfolio_rollup(summaries)

    panels = []
    for summary in summaries:
        unit = summary["unit"]
        bu = unit["key"]
        itds = summary["itds"]
        gov = summary["governance"]

        panels.append(
            {
                "key": bu,
                "name": unit["name"],
                "short": unit.get("short_name", unit["name"]),
                "contact": unit.get("contact", "—"),
                "country": unit.get("country", ""),
                "products": unit.get("products", []),
                "overall": summary["overall"],
                "overall_label": STATUS_LABELS[summary["overall"]],
                "overall_token": STATUS_TOKEN[summary["overall"]],
                "overall_glyph": STATUS_GLYPH[summary["overall"]],
                "stage": (gov.stage if gov else None) or "Not reported",
                "expected_exit": (gov.expected_exit if gov else None) or "—",
                "pnl_rows": [_metric_row(snapshot, bu, m, config, currency) for m in PNL_METRICS],
                "wc_rows": [_wc_row(snapshot, bu, m, config, currency) for m in WC_METRICS],
                "nr_chart": trajectory(snapshot, bu, "net_revenue", currency=currency),
                "ebita_chart": trajectory(snapshot, bu, "ebita", currency=currency),
                "itds": itds,
                "itds_posture": (itds.posture if itds else None) or "Not assessed",
                "itds_token": POSTURE_TOKEN.get((itds.posture if itds else "") or "", "muted"),
                "risk_rail": risk_rail(itds),
                "control_bar": control_bar(itds),
                "key_areas": [
                    {
                        "name": k.name,
                        "status": k.status,
                        "token": k.status if k.status != "unknown" else "muted",
                        "glyph": KEY_AREA_GLYPH.get(k.status, "–"),
                        "narrative": k.narrative,
                    }
                    for k in (itds.key_areas if itds else [])
                ],
                "improvements": [
                    {
                        "title": i.title,
                        "owner": i.owner,
                        "measure": i.measure_of_success,
                        "target_date": fmt_date(i.target_date),
                        "status": i.status,
                        "status_label": STATUS_LABELS[i.status],
                        "token": STATUS_TOKEN[i.status],
                        "glyph": STATUS_GLYPH[i.status],
                        "notes": i.notes,
                    }
                    for i in summary["initiatives"]
                ],
            }
        )

    seed_run = snapshot.run("seed")
    return {
        "group": snapshot.group,
        "period_label": month_label(snapshot.period),
        "quarter_label": snapshot.quarter.replace("-", " "),
        "next_quarter_label": next_quarter(snapshot.quarter).replace("-", " "),
        "generated": fmt_stamp(snapshot.generated_at),
        "currency": currency,
        "panels": panels,
        "rollup": {
            "net_revenue": {
                "value": fmt_money(rollup["net_revenue"].value, currency),
                "reference": fmt_money(rollup["net_revenue"].reference, currency),
                "delta_pct": fmt_delta_pct(rollup["net_revenue"]),
                "token": STATUS_TOKEN[rollup["net_revenue"].status(derive.bands(config))],
                "glyph": STATUS_GLYPH[rollup["net_revenue"].status(derive.bands(config))],
            },
            "ebita": {
                "value": fmt_money(rollup["ebita"].value, currency),
                "reference": fmt_money(rollup["ebita"].reference, currency),
                "delta_pct": fmt_delta_pct(rollup["ebita"]),
                "token": STATUS_TOKEN[rollup["ebita"].status(derive.bands(config))],
                "glyph": STATUS_GLYPH[rollup["ebita"].status(derive.bands(config))],
            },
            "ebita_pct": {
                "value": fmt_pct(rollup["ebita_pct"].value),
                "reference": fmt_pct(rollup["ebita_pct"].reference),
                "delta": fmt_delta(rollup["ebita_pct"]),
                "token": STATUS_TOKEN[rollup["ebita_pct"].status(derive.bands(config))],
                "glyph": STATUS_GLYPH[rollup["ebita_pct"].status(derive.bands(config))],
            },
            "itds_mean": f"{rollup['itds_mean']:.0f}" if rollup["itds_mean"] is not None else "—",
            "counts": rollup["status_counts"],
            "bu_count": rollup["bu_count"],
        },
        "runs": [
            {
                "id": r.source_id,
                "status": r.status,
                "detail": r.detail,
                "fetched": fmt_stamp(r.fetched_at),
                "modified": fmt_date(r.item_modified) if r.item_modified else "—",
                "rows": r.rows,
            }
            for r in snapshot.runs
        ],
        "seeded": seed_run is not None and seed_run.status == "seed",
        "itds_period": (snapshot.itds[0].period if snapshot.itds else "—").replace("-", " "),
    }


def render(snapshot: Snapshot, config: dict[str, Any], template_dir: Path | None = None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("dashboard.html.j2").render(**build_view(snapshot, config))
