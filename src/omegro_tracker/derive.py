"""Derived views over the fact table: variance, RAG and the roll-ups.

Nothing here reads a source. Everything is computed from `Snapshot.facts`, so
the same logic applies whether a number came from a monthly submission, the OG
scorecard or a seeded placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .model import Snapshot, months_in_quarter, next_quarter, quarter_of

Direction = Literal["higher", "lower"]


@dataclass
class Variance:
    """A forecast/actual pair and what it means.

    `favourable` is the sign adjusted for direction: EBITA under forecast is
    adverse, working capital under target is favourable. The dashboard colours
    on `status`, never on the raw sign.
    """

    reference: float | None          # forecast, target or baseline
    value: float | None              # actual or projection
    unit: str = "kGBP"
    direction: Direction = "higher"

    @property
    def delta(self) -> float | None:
        if self.reference is None or self.value is None:
            return None
        return self.value - self.reference

    @property
    def delta_pct(self) -> float | None:
        """Undefined against a zero reference — the template prints N/A there
        and so do we, rather than showing an infinite variance."""
        if self.delta is None or not self.reference:
            return None
        return self.delta / abs(self.reference)

    @property
    def favourable(self) -> bool | None:
        if self.delta is None:
            return None
        return self.delta >= 0 if self.direction == "higher" else self.delta <= 0

    @property
    def reported(self) -> bool:
        return self.value is not None

    def status(self, bands: dict[str, float]) -> str:
        """RAG against variance-to-reference bands.

        A percentage-point metric (EBITA %, WC %) is judged on the absolute
        point movement, not a percentage of a percentage — a 2pt slip on a 10%
        margin is a 20% relative swing, which would read as a crisis on a
        relative band.
        """
        if self.delta is None:
            return "unknown"
        if self.favourable:
            return "on_track"

        if self.unit == "pct":
            magnitude = abs(self.delta)
            on_track, at_risk = bands.get("on_track_pt", 0.01), bands.get("at_risk_pt", 0.03)
        else:
            if self.delta_pct is None:
                return "unknown"
            magnitude = abs(self.delta_pct)
            on_track, at_risk = bands["on_track"], bands["at_risk"]

        if magnitude <= on_track:
            return "on_track"
        return "at_risk" if magnitude <= at_risk else "off_track"


def _direction(metric: str, metrics_cfg: dict[str, Any]) -> Direction:
    return metrics_cfg.get(metric, {}).get("direction", "higher")  # type: ignore[return-value]


def _unit(metric: str, metrics_cfg: dict[str, Any], currency: str) -> str:
    unit = metrics_cfg.get(metric, {}).get("unit", "kGBP")
    return "pct" if unit == "pct" else f"k{currency}"


def variance(
    snapshot: Snapshot,
    bu: str,
    metric: str,
    basis: str,
    *,
    period: str | None = None,
    reference: str = "forecast",
    value: str = "actual",
    config: dict[str, Any] | None = None,
) -> Variance:
    config = config or {}
    metrics_cfg = config.get("metrics", {})
    currency = config.get("group", {}).get("reporting_currency", "GBP")
    return Variance(
        reference=snapshot.value(bu, metric, basis, reference, period),
        value=snapshot.value(bu, metric, basis, value, period),
        unit=_unit(metric, metrics_cfg, currency),
        direction=_direction(metric, metrics_cfg),
    )


def bands(config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("rag", {}).get("variance_pct", {})
    return {
        "on_track": raw.get("on_track", 0.05),
        "at_risk": raw.get("at_risk", 0.10),
        # Percentage-point equivalents of the same tolerance.
        "on_track_pt": raw.get("on_track_pt", 0.01),
        "at_risk_pt": raw.get("at_risk_pt", 0.03),
    }


def ytd_actual(snapshot: Snapshot, bu: str, metric: str) -> float | None:
    """Year to date from the monthly facts.

    Prefers an explicitly reported YTD figure; otherwise sums the closed months
    of the year. Returns None rather than a partial sum if no month reported —
    a missing YTD must not render as zero.
    """
    if (explicit := snapshot.value(bu, metric, "ytd", "actual")) is not None:
        return explicit

    year = snapshot.period.split("-")[0]
    values = [
        f.value
        for f in snapshot.facts
        if f.bu == bu
        and f.metric == metric
        and f.basis == "month"
        and f.measure == "actual"
        and f.period.startswith(year)
        and f.period <= snapshot.period
        and f.value is not None
    ]
    return sum(values) if values else None


def qtd_actual(snapshot: Snapshot, bu: str, metric: str) -> float | None:
    if (explicit := snapshot.value(bu, metric, "qtd", "actual")) is not None:
        return explicit
    wanted = [m for m in months_in_quarter(snapshot.quarter) if m <= snapshot.period]
    values = [
        f.value
        for f in snapshot.facts
        if f.bu == bu
        and f.metric == metric
        and f.basis == "month"
        and f.measure == "actual"
        and f.period in wanted
        and f.value is not None
    ]
    return sum(values) if values else None


def outturn(snapshot: Snapshot, bu: str, metric: str) -> float | None:
    """Full-quarter projection — what the group calls outturn.

    The submitted projection is authoritative; where a BU has not given one,
    fall back to QTD actual plus the remaining months of the quarter at
    forecast, which is the arithmetic the reviewers do by hand anyway.
    """
    if (projection := snapshot.value(bu, metric, "quarter", "projection", snapshot.quarter)) is not None:
        return projection

    actual = qtd_actual(snapshot, bu, metric)
    if actual is None:
        return None
    remaining = [m for m in months_in_quarter(snapshot.quarter) if m > snapshot.period]
    forecasts = [
        snapshot.value(bu, metric, "month", "forecast", m) for m in remaining
    ]
    if any(f is None for f in forecasts):
        return None
    return actual + sum(f for f in forecasts if f is not None)


def bu_summary(snapshot: Snapshot, unit: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The per-BU headline: the handful of figures that decide whether this
    business needs attention this month."""
    bu = unit["key"]
    band = bands(config)
    itds = snapshot.itds_for(bu)
    gov = snapshot.governance_for(bu)

    headline = {}
    for metric in ("net_revenue", "ebita", "ebita_pct", "wc_pct"):
        v = variance(snapshot, bu, metric, "qtd", config=config)
        headline[metric] = {"variance": v, "status": v.status(band)}

    quarter = {}
    for metric in ("net_revenue", "ebita"):
        v = variance(
            snapshot, bu, metric, "quarter",
            period=snapshot.quarter, value="projection", config=config,
        )
        quarter[metric] = {"variance": v, "status": v.status(band)}

    initiatives = snapshot.initiatives_for(bu)
    off_track = sum(1 for i in initiatives if i.status == "off_track")
    at_risk = sum(1 for i in initiatives if i.status == "at_risk")

    # Overall status: the worst of the reported signals. A BU whose figures
    # have not landed reads as "not reported", not as green.
    signals = [v["status"] for v in headline.values()] + [v["status"] for v in quarter.values()]
    if gov and gov.overall_status != "unknown":
        signals.append(gov.overall_status)
    if off_track:
        signals.append("off_track")
    elif at_risk:
        signals.append("at_risk")

    order = {"off_track": 3, "at_risk": 2, "on_track": 1, "unknown": 0}
    reported = [s for s in signals if s != "unknown"]
    overall = max(reported, key=lambda s: order[s]) if reported else "unknown"

    return {
        "unit": unit,
        "headline": headline,
        "quarter": quarter,
        "overall": overall,
        "governance": gov,
        "itds": itds,
        "initiatives": initiatives,
        "initiatives_off_track": off_track,
        "initiatives_at_risk": at_risk,
        "reported": any(v["variance"].reported for v in headline.values()),
    }


def portfolio_rollup(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Group-level aggregation. Money adds up; ratios do not — EBITA % is
    recomputed from the summed components rather than averaged."""
    def total(metric: str, measure: str) -> float | None:
        """Sum across units, but only when every unit reported.

        A total over a partial set is not a smaller total, it is a wrong one —
        it reads as the group's number while silently omitting a business.
        """
        values = [
            s["headline"][metric]["variance"].value
            if measure == "actual"
            else s["headline"][metric]["variance"].reference
            for s in summaries
            if metric in s["headline"]
        ]
        if not values or any(v is None for v in values):
            return None
        return sum(values)  # type: ignore[arg-type]

    nr_actual = total("net_revenue", "actual")
    nr_forecast = total("net_revenue", "forecast")
    ebita_actual = total("ebita", "actual")
    ebita_forecast = total("ebita", "forecast")

    counts = {"on_track": 0, "at_risk": 0, "off_track": 0, "unknown": 0}
    for s in summaries:
        counts[s["overall"]] += 1

    itds_scores = [s["itds"].risk_current for s in summaries if s["itds"] and s["itds"].risk_current is not None]

    return {
        "net_revenue": Variance(nr_forecast, nr_actual, "kGBP", "higher"),
        "ebita": Variance(ebita_forecast, ebita_actual, "kGBP", "higher"),
        "ebita_pct": Variance(
            (ebita_forecast / nr_forecast) if nr_forecast else None,
            (ebita_actual / nr_actual) if nr_actual else None,
            "pct",
            "higher",
        ),
        "status_counts": counts,
        "itds_total": sum(itds_scores) if itds_scores else None,
        "itds_mean": (sum(itds_scores) / len(itds_scores)) if itds_scores else None,
        "bu_count": len(summaries),
    }
