"""The Operational Governance framework, applied to what we already hold.

From Q3-26 the OG scorecard gains a fifth dimension, IT & Data Security,
scored from the QDSR security maturity score. We already parse that score for
all three units out of the Portfolio Security Assessment, and the CFO
published the bands that turn it into points — so this one column of the
scorecard can be computed here rather than waited for.

Nothing else in the scorecard is derived. The financial, leadership,
operational and talent dimensions, and the overall stage they add up to, are
the portfolio's to publish; this module never guesses at them.
"""

from __future__ import annotations

from typing import Any

from .model import ITDS, ITDSDimension


def _quarter_index(quarter: str) -> tuple[int, int]:
    year, q = quarter.split("-Q")
    return int(year), int(q)


def _at_or_after(quarter: str, threshold: str) -> bool:
    return _quarter_index(quarter) >= _quarter_index(threshold)


def band_for(qdsr: float, bands: list[dict[str, Any]]) -> dict[str, Any]:
    """The first band whose ceiling the score does not exceed.

    Bands are read in published order and the last one is open-ended, so a
    score is always placed. The published Red band is "161-200" and Black is
    "200+", which both claim exactly 200; read in order, 200 lands in Red.
    That is the reading that favours the business, so it is flagged on the
    page rather than settled silently — see `boundary`.
    """
    for band in bands:
        if band["max"] is None or qdsr <= band["max"]:
            return band
    return bands[-1]


def on_a_band_boundary(qdsr: float, bands: list[dict[str, Any]]) -> bool:
    """True where the score sits exactly on a published band ceiling and the
    next band's wording also claims it."""
    return any(b["max"] is not None and qdsr == b["max"] for b in bands[:-1])


def itds_dimension(
    record: ITDS, framework: dict[str, Any], quarter: str
) -> ITDSDimension | None:
    """Score one BU's ITDS dimension, or None if it is not yet published."""
    spec = framework.get("itds_dimension") or {}
    bands = spec.get("bands") or []
    if record.risk_current is None or not bands:
        return None
    if not _at_or_after(quarter, spec.get("published_from", quarter)):
        return None

    qdsr = record.risk_current
    band = band_for(qdsr, bands)
    counts = _at_or_after(quarter, spec.get("counts_from", "9999-Q4"))

    esc = spec.get("escalation") or {}
    reasons: list[str] = []
    queries: list[str] = []
    if _at_or_after(quarter, esc.get("from", "9999-Q4")):
        floor = esc.get("qdsr_at_or_above")
        if floor is not None and qdsr >= floor:
            reasons.append(
                f"QDSR {qdsr:.0f} is at or above the Needs Improvement threshold of {floor:.0f}"
            )
        # The framework names EDR coverage and MFA enforcement but does not
        # define what counts as a failure. A `critical` QDSR key area on
        # either is the nearest published equivalent, so it is raised as a
        # question, not asserted as an escalation.
        for area in record.key_areas:
            if area.name in (esc.get("critical_control_areas") or []) and area.status == "critical":
                queries.append(
                    f"{area.name} is rated critical in the QDSR — confirm with "
                    f"IT & DS whether this is a critical control failure"
                )

    return ITDSDimension(
        qdsr=qdsr,
        qdsr_period=record.period,
        band=band["name"],
        points=float(band["points"]),
        points_max=float(framework.get("points_per_dimension", 6)),
        counts=counts,
        target=float(spec.get("target", 80)),
        escalated=bool(reasons),
        escalation_reasons=reasons,
        escalation_queries=queries,
    )


def points_max(framework: dict[str, Any], quarter: str) -> float:
    """The scorecard's maximum total, which rises when ITDS starts counting."""
    spec = framework.get("itds_dimension") or {}
    counts = _at_or_after(quarter, spec.get("counts_from", "9999-Q4"))
    key = "points_max_after" if counts else "points_max_before"
    return float(framework.get(key, 36))
