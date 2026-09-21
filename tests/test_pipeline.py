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


def test_build_carries_live_financials(config):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    assert snap.has_live_financials is True
    assert snap.run("omegro_monthly").status == "ok"


# Published figures from the Omegro P8 FY26 pack, "David Turner Group" band,
# USD thousands. These assert the source of record, not our arithmetic.
P8_QTD = {
    "tbl": {"net_revenue": (581.784, 647.492), "ebita": (192.306, 241.657)},
    "tlm": {"net_revenue": (1523.971, 1575.765), "ebita": (259.267, 375.920)},
    "grosvenor": {"net_revenue": (1016.312, 1003.483), "ebita": (319.176, 284.754)},
}


@pytest.mark.parametrize("bu", list(P8_QTD))
def test_qtd_financials_match_the_reporting_pack(config, bu):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    for metric, (actual, forecast) in P8_QTD[bu].items():
        assert snap.value(bu, metric, "qtd", "actual") == pytest.approx(actual)
        assert snap.value(bu, metric, "qtd", "forecast") == pytest.approx(forecast)


def test_out_of_scope_units_are_dropped_entirely(config):
    """A divesting unit must not survive anywhere in the snapshot — not as a
    panel, not in a total, and not as a stray fact in the committed file."""
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    assert [u["key"] for u in snap.business_units] == ["tbl", "tlm", "grosvenor"]
    assert not [f for f in snap.facts if f.bu == "agentos"]
    assert not [c for c in snap.commentary if c.bu == "agentos"]
    assert "agentos" not in snap.to_json()

    summaries = [derive.bu_summary(snap, u, config) for u in snap.business_units]
    rollup = derive.portfolio_rollup(summaries)
    # 581.784 + 1523.971 + 1016.312, i.e. the group total less AgentOS.
    assert rollup["net_revenue"].value == pytest.approx(3122.067)


def test_ebita_margin_is_a_ratio_of_sums_not_an_average(config):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    summaries = [derive.bu_summary(snap, u, config) for u in snap.business_units]
    rollup = derive.portfolio_rollup(summaries)
    expected = (192.306 + 259.267 + 319.176) / 3122.067
    assert rollup["ebita_pct"].value == pytest.approx(expected)


def test_commentary_is_attached_and_quoted(config):
    from omegro_tracker.build import build

    snap = build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")
    opex = snap.commentary_for("tlm", "opex")
    assert opex and "AI token usage" in opex[0].text


def test_config_scopes_the_bpc_group_down_to_the_three_led_units(config):
    """BPC's "David Turner Group" row band still carries AgentOS, which is
    divesting. The entry stays so its alias keeps matching, but it is out of
    scope and nothing it carries may reach the snapshot."""
    keys = [u["key"] for u in config["business_units"]]
    assert keys == ["tbl", "tlm", "grosvenor", "agentos"]
    in_scope = [u["key"] for u in config["business_units"] if u.get("in_scope", True)]
    assert in_scope == ["tbl", "tlm", "grosvenor"]


def test_every_in_scope_unit_has_a_named_leader(config):
    for unit in config["business_units"]:
        if unit.get("in_scope", True):
            assert unit.get("leader"), unit["key"]


# -- group summary ----------------------------------------------------------


def _live(config):
    from omegro_tracker.build import build

    return build(period="2026-08", config_dir=ROOT / "config", offline=ROOT / ".cache/graph")


def test_august_month_is_p8_qtd_less_p7_qtd(config):
    """July is month 1 of Q3, so the month split is exact, not apportioned."""
    snap = _live(config)
    for bu, jul, qtd in (
        ("tbl", 326.198, 581.784),
        ("tlm", 760.396, 1523.971),
        ("grosvenor", 513.713, 1016.312),
    ):
        assert snap.value(bu, "net_revenue", "month", "actual", "2026-07") == pytest.approx(jul)
        assert snap.value(bu, "net_revenue", "month", "actual", "2026-08") == pytest.approx(qtd - jul)


def test_group_month_totals(config):
    from omegro_tracker.render import _group_rows

    snap = _live(config)
    summaries = [derive.bu_summary(snap, u, config) for u in snap.business_units]
    rows = {r["key"]: r for r in _group_rows(snap, summaries, config, "USD")}
    # 255.586 + 763.575 + 502.599
    assert rows["net_revenue"]["cells"]["month"]["value"] == "$1.52m"
    assert rows["net_revenue"]["cells"]["qtd"]["value"] == "$3.12m"
    # No FY source is connected, so the column must be empty, not a partial sum.
    assert rows["net_revenue"]["cells"]["fy"]["reported"] is False
    assert rows["net_revenue"]["cells"]["fy"]["value"] == "—"


def test_group_total_is_withheld_when_a_unit_has_not_reported(config):
    """A sum over a partial set reads as the group's number while omitting a
    business, so it must not be produced at all."""
    from omegro_tracker.render import _group_rows

    snap = _live(config)
    snap.facts = [
        f for f in snap.facts
        if not (f.bu == "tlm" and f.metric == "net_revenue" and f.basis == "qtd")
    ]
    summaries = [derive.bu_summary(snap, u, config) for u in snap.business_units]
    rows = {r["key"]: r for r in _group_rows(snap, summaries, config, "USD")}
    assert rows["net_revenue"]["cells"]["qtd"]["reported"] is False
    # The month column is untouched and still totals.
    assert rows["net_revenue"]["cells"]["month"]["reported"] is True


def test_full_year_is_measured_against_baseline_not_forecast(config):
    """Baseline is the start-of-year plan and is fixed; forecast is the latest
    iteration and moves. Measuring the year against forecast would compare the
    latest estimate with itself."""
    from omegro_tracker.render import GROUP_BANDS, _group_rows

    bands = {b["key"]: b for b in GROUP_BANDS}
    assert bands["fy"]["compare_head"] == "vs baseline"
    assert bands["month"]["compare_head"] == "vs fcst"
    assert bands["qtd"]["compare_head"] == "vs fcst"

    snap = _live(config)
    # Give one unit an FY baseline and forecast; the variance must use the
    # baseline as the reference, not the forecast.
    for bu, baseline, forecast in (
        ("tbl", 4000.0, 3800.0), ("tlm", 1000.0, 1000.0), ("grosvenor", 1000.0, 1000.0),
    ):
        snap.facts += [
            Fact(bu, "net_revenue", "year", "baseline", "2026", baseline, "k"),
            Fact(bu, "net_revenue", "year", "forecast", "2026", forecast, "k"),
        ]
    summaries = [derive.bu_summary(snap, u, config) for u in snap.business_units]
    fy = {r["key"]: r for r in _group_rows(snap, summaries, config, "USD")}["net_revenue"]["cells"]["fy"]
    assert fy["reported"] is True
    assert fy["value"] == "$5.80m"        # summed forecast
    assert fy["display"] == "-3.3%"       # against summed baseline of 6.00m


def test_no_exit_date_anywhere(config):
    """The group does not sell, so an "expected date to exit" is meaningless."""
    from omegro_tracker.model import Governance
    from omegro_tracker.render import render

    assert not hasattr(Governance(bu="x", period="2026-Q3"), "expected_exit")
    html = render(_live(config), config)
    assert "exit by" not in html.lower()
    assert "expected exit" not in html.lower()


# -- OG scorecard location --------------------------------------------------


def test_og_scorecard_reads_from_the_nelson_leadership_area(config):
    spec = next(s for s in config["sources"] if s["id"] == "og_scorecard")
    assert spec["site"] == "GRPNelsonPortfolioFinanceRenukaSimpsonGroup-Leadership"
    assert spec["path"] == "Leadership/09. Operational Governance"
    # Resolved as a folder, never pinned to one quarter's workbook.
    assert spec["kind"] == "folder"
    assert "item_id" not in spec
    assert spec["prefer"] == "_VALUES"


def test_og_scorecard_has_a_portfolio_fallback(config):
    spec = next(s for s in config["sources"] if s["id"] == "og_scorecard_portfolio_copy")
    assert spec["site"] == "OmegroNelsonPortfolio"
    assert spec["path"] == "Operational Governance/Scoring Assessment"


def test_scorecard_pick_prefers_the_values_snapshot():
    """Both copies exist for a quarter; the values snapshot is the parseable
    one, so it wins a tie on modification time."""
    from omegro_tracker.build import _pick_scorecard
    from omegro_tracker.graph import DriveItem

    def item(name, modified):
        return DriveItem(id=name, name=name, size=1, last_modified=modified,
                         download_url=None, is_folder=False)

    class FakeClient:
        def __init__(self, items):
            self._items = items

        def children(self, drive_id, path):
            return iter(self._items)

    spec = {
        "drive_id": "d", "path": "p",
        "match": "*Operational Governance Scorecard*.xlsx", "prefer": "_VALUES",
    }

    same_day = [
        item("Q226 - Operational Governance Scorecard - June 2026.xlsx", "2026-08-05T13:54:11Z"),
        item("Q226 - Operational Governance Scorecard - June 2026_VALUES.xlsx", "2026-08-05T13:54:11Z"),
    ]
    assert "_VALUES" in _pick_scorecard(FakeClient(same_day), spec).name

    # A genuinely newer quarter beats the preference.
    newer = same_day + [
        item("Q326 - Operational Governance Scorecard - September 2026.xlsx", "2026-10-04T09:00:00Z")
    ]
    assert _pick_scorecard(FakeClient(newer), spec).name.startswith("Q326")

    # Non-matching files are ignored, and an empty folder yields nothing.
    assert _pick_scorecard(FakeClient([item("Goals 2026.xlsx", "2026-11-01T00:00:00Z")]), spec) is None


# -- weekly scan ------------------------------------------------------------


def _drive_item(name, modified, folder=False):
    from omegro_tracker.graph import DriveItem

    return DriveItem(name, name, 1, modified, None, folder)


class _FakeDrive:
    """A SharePoint tree. Unknown paths 404, as Graph does."""

    def __init__(self, tree):
        self.tree = tree

    def item(self, drive_id, item_id):
        return _drive_item("QDSR_Q2_Validation_Assessment.html", "2026-08-20T14:54:16Z")

    def children(self, drive_id, path):
        from omegro_tracker.graph import GraphError

        if path not in self.tree:
            raise GraphError(f"404 {path}")
        return iter(self.tree[path])


_TREE = {
    # Finance files the pack under year / month folders.
    "Monthly Reporting": [_drive_item("2026", "2026-10-08T09:00:00Z", True)],
    "Monthly Reporting/2026": [_drive_item("9. Sep", "2026-10-08T09:12:00Z", True)],
    "Monthly Reporting/2026/9. Sep": [
        _drive_item("Omegro NR P9 FY26.xlsx", "2026-10-08T09:12:00Z")
    ],
    "Leadership/09. Operational Governance": [
        _drive_item("Q226 - Operational Governance Scorecard - June 2026.xlsx", "2026-08-05T13:54:11Z"),
        _drive_item(
            "Q226 - Operational Governance Scorecard - June 2026_VALUES.xlsx", "2026-08-05T13:54:11Z"
        ),
    ],
}


def _scan(config, tree=None, seen=None):
    from omegro_tracker import scan as scanner

    return scanner.scan(config, _FakeDrive(tree or _TREE), seen=seen)


def test_scan_walks_into_dated_subfolders(config):
    """The pack sits under year/month folders. Stopping at the named folder
    finds only directories and would report the source missing every week."""
    result, _ = _scan(config)
    pnl = next(c for c in result.changes if c.section == "P&L")
    assert pnl.status == "new"
    assert pnl.file_name == "Omegro NR P9 FY26.xlsx"
    assert pnl.web_path.endswith("Monthly Reporting/2026/9. Sep")


def test_scan_is_quiet_when_nothing_moved(config):
    _, seen = _scan(config)
    result, _ = _scan(config, seen=seen)
    assert result.sections_changed == []
    assert not any(c.status in {"new", "updated"} for c in result.changes)


def test_scan_reports_a_republished_workbook(config):
    _, seen = _scan(config)
    moved = {
        **_TREE,
        "Monthly Reporting/2026/9. Sep": [
            _drive_item("Omegro NR P9 FY26.xlsx", "2026-10-13T08:30:00Z")
        ],
    }
    result, _ = _scan(config, tree=moved, seen=seen)
    assert result.sections_changed == ["P&L"]
    assert next(c for c in result.changes if c.section == "P&L").status == "updated"


def test_scan_prefers_the_values_snapshot(config):
    result, _ = _scan(config)
    og = next(c for c in result.changes if c.source_id == "og_scorecard")
    assert "_VALUES" in og.file_name


def test_a_source_that_cannot_be_read_does_not_abort_the_scan(config):
    """A half-configured source is reported, not raised — one bad entry must
    not cost us the whole Monday scan."""
    broken_config = {
        **config,
        "sources": [
            *config["sources"],
            {"id": "broken", "section": "ITDS", "label": "Half-wired source", "path": "x"},
        ],
    }
    result, _ = _scan(broken_config)
    broken = next(c for c in result.changes if c.source_id == "broken")
    assert broken.status == "error"
    assert "drive_id" in broken.detail
    # Everything else still got scanned.
    assert any(c.status == "new" for c in result.changes)


def test_unwatchable_sources_do_not_raise_a_weekly_false_alarm(config):
    """A source with no drive cannot be watched. It must be left out of the
    scan rather than erroring every week about a gap we already know about."""
    result, _ = _scan(config)
    assert not any(c.source_id == "itds_assessments" for c in result.changes)
    assert result.errors == []


def test_dry_run_must_not_advance_the_watermark(config):
    """A preview that moves the watermark loses the change for the real run."""
    from omegro_tracker import scan as scanner

    _, seen = _scan(config)
    before = dict(seen)
    moved = {
        **_TREE,
        "Monthly Reporting/2026/9. Sep": [
            _drive_item("Omegro NR P9 FY26.xlsx", "2026-10-13T08:30:00Z")
        ],
    }
    # scan() returns updated watermarks but never persists them itself.
    result, updated = scanner.scan(config, _FakeDrive(moved), seen=before)
    assert result.sections_changed == ["P&L"]
    assert before["omegro_monthly"].modified == "2026-10-08T09:12:00Z"
    assert updated["omegro_monthly"].modified == "2026-10-13T08:30:00Z"


def test_seen_watermarks_round_trip(tmp_path, config):
    from omegro_tracker import scan as scanner

    _, seen = _scan(config)
    path = tmp_path / "seen.json"
    scanner.save_seen(seen, path)
    assert scanner.load_seen(path) == seen


def test_subject_names_the_sections_not_just_that_it_ran(config):
    from omegro_tracker import notify

    _, seen = _scan(config)
    moved = {
        **_TREE,
        "Monthly Reporting/2026/9. Sep": [
            _drive_item("Omegro NR P9 FY26.xlsx", "2026-10-13T08:30:00Z")
        ],
    }
    result, _ = _scan(config, tree=moved, seen=seen)
    assert "P&L updated" in notify.subject(result)

    quiet, _ = _scan(config, seen=seen)
    assert "no source updates" in notify.subject(quiet)


def test_alert_body_carries_the_dashboard_link(config):
    from omegro_tracker import notify

    result, _ = _scan(config)
    subject, html_body, text = notify.build(result, config)
    url = config["group"]["dashboard_url"]
    assert url in html_body and url in text
    assert subject and "Turner Group" in subject


def test_alert_recipient_is_configured(config):
    assert config["alerts"]["recipients"] == ["david.turner@omegro.com"]
    # Monday, morning, UTC.
    minute, hour, _, _, dow = config["alerts"]["cron"].split()
    assert dow == "1" and 6 <= int(hour) <= 9 and minute == "0"


def test_every_dashboard_section_is_watched(config):
    from omegro_tracker.scan import SECTION_ORDER

    watched = {s["section"] for s in config["sources"] if s.get("section")}
    assert watched == set(SECTION_ORDER)
