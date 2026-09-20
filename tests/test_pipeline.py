"""Tests for the extraction and derivation layers.

The ITDS tests assert against figures published in the Q2-26 QDSR Validation
Assessment, so a parser regression shows up as a wrong number rather than as a
silently empty pane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omegro_tracker import derive
from omegro_tracker.build import load_config
from omegro_tracker.derive import Variance
from omegro_tracker.model import (
    Fact,
    Snapshot,
    default_period,
    month_label,
    months_in_quarter,
    next_quarter,
    prior_month,
    quarter_of,
)
from omegro_tracker.parsers import itds_qdsr
from omegro_tracker.parsers.monthly_review import _number, match_period

ROOT = Path(__file__).resolve().parents[1]
QDSR = ROOT / ".cache/graph/qdsr_q2_2026.html"


@pytest.fixture(scope="module")
def config() -> dict:
    return load_config(ROOT / "config")


# -- period arithmetic ------------------------------------------------------


def test_quarter_of():
    assert quarter_of("2026-01") == "2026-Q1"
    assert quarter_of("2026-08") == "2026-Q3"
    assert quarter_of("2026-12") == "2026-Q4"


def test_next_quarter_wraps_the_year():
    assert next_quarter("2026-Q3") == "2026-Q4"
    assert next_quarter("2026-Q4") == "2027-Q1"


def test_prior_month_wraps_the_year():
    assert prior_month("2026-01") == "2025-12"
    assert prior_month("2026-08") == "2026-07"


def test_months_in_quarter():
    assert months_in_quarter("2026-Q3") == ["2026-07", "2026-08", "2026-09"]


def test_month_label_matches_template_format():
    assert month_label("2026-08") == "Aug-26"


def test_default_period_waits_for_close():
    from datetime import date

    # Submissions are due within 7 business days, so early in the month the
    # latest closed month is two back.
    assert default_period(date(2026, 9, 3)) == "2026-07"
    assert default_period(date(2026, 9, 20)) == "2026-08"
    assert default_period(date(2026, 1, 5)) == "2025-11"


# -- number parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234", 1234.0),
        ("(500)", -500.0),
        ("12.5%", 0.125),
        ("£1,000", 1000.0),
        ("[Enter Name]", None),
        ("TBC", None),
        ("", None),
        (None, None),
        (42, 42.0),
    ],
)
def test_number_parsing(raw, expected):
    assert _number(raw) == expected


def test_match_period_accepts_the_spellings_bus_use():
    for name in (
        "TBL - Aug-26 - Monthly Review.xlsx",
        "TBL Aug 26 monthly review.xlsx",
        "Grosvenor Aug2026.xlsx",
        "tlmNexus 2026-08.xlsx",
    ):
        assert match_period(name, "2026-08"), name
    assert not match_period("TBL - Jul-26 - Monthly Review.xlsx", "2026-08")


# -- variance ---------------------------------------------------------------


def test_variance_direction_decides_favourability():
    # EBITA below forecast is adverse.
    assert Variance(100, 90, "kGBP", "higher").favourable is False
    # Working capital below target is favourable.
    assert Variance(0.14, 0.12, "pct", "lower").favourable is True


def test_variance_pct_undefined_against_zero():
    assert Variance(0, 50).delta_pct is None
    assert Variance(0, 50).delta == 50


def test_percentage_point_metrics_use_point_bands():
    bands = {"on_track": 0.05, "at_risk": 0.10, "on_track_pt": 0.01, "at_risk_pt": 0.03}
    # A margin slipping 2.7 points is a 12% relative move but only 2.7pt — it
    # must not read as off track on the relative band.
    margin = Variance(0.231, 0.204, "pct", "higher")
    assert margin.status(bands) == "at_risk"
    # Money uses the relative band.
    assert Variance(679, 580, "kGBP", "higher").status(bands) == "off_track"


def test_unreported_is_not_green():
    assert Variance(100, None).status({"on_track": 0.05, "at_risk": 0.10}) == "unknown"
    assert Variance(100, None).reported is False


# -- roll-ups ---------------------------------------------------------------


def _snapshot(facts: list[Fact]) -> Snapshot:
    return Snapshot(
        generated_at="2026-09-20T00:00:00+00:00",
        period="2026-08",
        quarter="2026-Q3",
        business_units=[{"key": "x", "name": "X"}],
        facts=facts,
    )


def test_qtd_sums_closed_months_when_not_reported():
    snap = _snapshot(
        [
            Fact("x", "net_revenue", "month", "actual", "2026-07", 100),
            Fact("x", "net_revenue", "month", "actual", "2026-08", 120),
            # September is not closed and must not be counted.
            Fact("x", "net_revenue", "month", "forecast", "2026-09", 130),
        ]
    )
    assert derive.qtd_actual(snap, "x", "net_revenue") == 220


def test_qtd_prefers_an_explicitly_reported_figure():
    snap = _snapshot(
        [
            Fact("x", "net_revenue", "month", "actual", "2026-07", 100),
            Fact("x", "net_revenue", "qtd", "actual", "2026-08", 999),
        ]
    )
    assert derive.qtd_actual(snap, "x", "net_revenue") == 999


def test_missing_data_returns_none_not_zero():
    assert derive.qtd_actual(_snapshot([]), "x", "net_revenue") is None
    assert derive.ytd_actual(_snapshot([]), "x", "net_revenue") is None


def test_outturn_falls_back_to_qtd_plus_remaining_forecast():
    snap = _snapshot(
        [
            Fact("x", "net_revenue", "month", "actual", "2026-07", 100),
            Fact("x", "net_revenue", "month", "actual", "2026-08", 120),
            Fact("x", "net_revenue", "month", "forecast", "2026-09", 130),
        ]
    )
    assert derive.outturn(snap, "x", "net_revenue") == 350


def test_outturn_prefers_the_submitted_projection():
    snap = _snapshot(
        [
            Fact("x", "net_revenue", "month", "actual", "2026-07", 100),
            Fact("x", "net_revenue", "quarter", "projection", "2026-Q3", 400),
        ]
    )
    assert derive.outturn(snap, "x", "net_revenue") == 400


# -- ITDS parser ------------------------------------------------------------

# Published Q2-26 figures. These are assertions about the source of record, not
# about our arithmetic — if they change, the assessment changed.
ITDS_EXPECTED = {
    "tbl": dict(posture="Moderate", current=126.0, initial=18.0, strong=28, focus=22, low=4, kri=6.8),
    "tlm": dict(posture="Strong", current=22.0, initial=22.0, strong=47, focus=3, low=2, kri=9.4),
    "grosvenor": dict(
        posture="Needs Improvement", current=200.0, initial=97.0, strong=15, focus=35, low=10, kri=5.1
    ),
}


@pytest.fixture(scope="module")
def itds_records(config):
    if not QDSR.exists():
        pytest.skip("QDSR report not cached; run `omegro-tracker refresh`")
    return {
        r.bu: r
        for r in itds_qdsr.parse(
            QDSR.read_text(encoding="utf-8", errors="replace"),
            units=config["business_units"],
            period="2026-Q2",
        )
    }


@pytest.mark.parametrize("bu", list(ITDS_EXPECTED))
def test_itds_matches_published_assessment(itds_records, bu):
    record = itds_records[bu]
    want = ITDS_EXPECTED[bu]
    assert record.posture == want["posture"]
    assert record.risk_current == want["current"]
    assert record.risk_initial == want["initial"]
    assert record.controls_strong == want["strong"]
    assert record.controls_focus == want["focus"]
    assert record.controls_low == want["low"]
    assert record.average_kri == want["kri"]
    assert record.controls_assessed == 50


def test_itds_control_counts_reconcile(itds_records):
    """`low` is a subset of `focus`, and strong + focus is the assessed set."""
    for record in itds_records.values():
        assert record.controls_low <= record.controls_focus
        assert record.controls_strong + record.controls_focus == record.controls_assessed


def test_itds_movement_sign(itds_records):
    # Grosvenor's residual risk has risen since baseline; tlmNexus is unchanged.
    assert itds_records["grosvenor"].movement == 103.0
    assert itds_records["tlm"].movement == 0.0


def test_itds_key_areas_carry_status_and_narrative(itds_records):
    areas = itds_records["grosvenor"].key_areas
    assert {a.name for a in areas} == {"Backup resilience", "IAM & MFA", "Detection & Response"}
    assert all(a.narrative for a in areas)
    assert all(a.status in {"good", "warning", "critical"} for a in areas)


def test_itds_risk_pct_is_against_the_fixed_400_ceiling(itds_records):
    assert itds_records["grosvenor"].risk_pct == pytest.approx(0.50)
    assert itds_records["tlm"].risk_pct == pytest.approx(0.055)


# -- snapshot round trip ----------------------------------------------------


def test_snapshot_round_trips(config):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    restored = Snapshot.from_dict(json.loads(snap.to_json()))
    assert restored.period == snap.period
    assert len(restored.facts) == len(snap.facts)
    assert len(restored.itds) == len(snap.itds)
    if snap.itds:
        assert restored.itds[0].key_areas[0].name == snap.itds[0].key_areas[0].name


def test_seeded_build_is_flagged_as_seeded(config):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    assert snap.has_live_financials is False
    assert snap.run("seed") is not None and snap.run("seed").status == "seed"


def test_config_business_units_are_the_three_group_units(config):
    assert [u["key"] for u in config["business_units"]] == ["tbl", "tlm", "grosvenor"]
