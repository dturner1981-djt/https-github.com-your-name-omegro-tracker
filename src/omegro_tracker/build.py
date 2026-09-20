"""Refresh orchestration: pull every configured source into one Snapshot.

A source that cannot be reached does not fail the build. It records a
`SourceRun` with the reason and the dashboard shows that pane as unreported,
because a governance dashboard that silently drops a business unit is worse
than one that says it could not read it.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .graph import GraphClient, GraphError
from .model import (
    Commentary,
    Fact,
    Governance,
    Initiative,
    ITDS,
    KeyArea,
    Snapshot,
    SourceRun,
    default_period,
    quarter_of,
)
from .parsers import itds_qdsr, monthly_review, og_scorecard, omegro_monthly

CONFIG_DIR = Path("config")
EXTRACT_DIR = Path("data/extracted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    portfolio = yaml.safe_load((config_dir / "portfolio.yml").read_text())
    sources = yaml.safe_load((config_dir / "sources.yml").read_text())
    return {**portfolio, "sources": sources["sources"], "graph": sources["graph"]}


def _source(config: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    return next((s for s in config["sources"] if s["id"] == source_id), None)


# -- individual source loaders ----------------------------------------------


def _load_itds(
    config: dict[str, Any], client: GraphClient | None, offline: Path | None
) -> tuple[list[ITDS], SourceRun]:
    spec = _source(config, "itds_qdsr")
    units = config["business_units"]
    period = spec.get("period") if spec else None
    risk_max = config.get("itds", {}).get("max_score", 400)

    path: Path | None = None
    modified = None
    if offline and (candidate := offline / "qdsr_q2_2026.html").exists():
        path = candidate
    elif client and spec:
        try:
            item = client.item(spec["drive_id"], spec["item_id"])
            path = client.download(spec["drive_id"], item)
            modified = item.last_modified
        except GraphError as exc:
            return [], SourceRun("itds_qdsr", "error", str(exc), _now())

    if path is None:
        return [], SourceRun("itds_qdsr", "missing", "no client and no cached report", _now())

    records = itds_qdsr.parse(
        path.read_text(encoding="utf-8", errors="replace"),
        units=units,
        period=period or quarter_of(default_period()),
        risk_max=risk_max,
    )
    return records, SourceRun(
        "itds_qdsr", "ok", f"parsed {len(records)} business units", _now(), modified, len(records)
    )


def _load_monthly(
    config: dict[str, Any], client: GraphClient | None, period: str
) -> tuple[list[Fact], list[Initiative], list[Governance], SourceRun]:
    spec = _source(config, "bu_monthly_submissions")
    if client is None or spec is None:
        return [], [], [], SourceRun(
            "bu_monthly_submissions", "missing", "no Graph client", _now()
        )

    facts: list[Fact] = []
    initiatives: list[Initiative] = []
    governance: list[Governance] = []
    found: list[str] = []

    try:
        children = [c for c in client.children(spec["drive_id"], spec["path"]) if not c.is_folder]
    except GraphError as exc:
        return [], [], [], SourceRun("bu_monthly_submissions", "error", str(exc), _now())

    for unit in config["business_units"]:
        aliases = [unit["name"], unit.get("short_name", ""), *unit.get("aliases", [])]
        candidates = [
            c
            for c in children
            if monthly_review.match_period(c.name, period)
            and any(a and a.lower() in c.name.lower() for a in aliases)
        ]
        if not candidates:
            continue
        latest = max(candidates, key=lambda c: c.last_modified)
        path = client.download(spec["drive_id"], latest)
        unit_facts, unit_initiatives, unit_gov = monthly_review.parse(
            path,
            bu=unit["key"],
            period=period,
            currency=unit.get("currency", "GBP"),
        )
        facts += unit_facts
        initiatives += unit_initiatives
        if unit_gov:
            governance.append(unit_gov)
        found.append(latest.name)

    status = "ok" if found else "missing"
    detail = ", ".join(found) if found else f"no submissions matching {period}"
    return facts, initiatives, governance, SourceRun(
        "bu_monthly_submissions", status, detail, _now(), rows=len(facts)
    )


def _load_og(
    config: dict[str, Any], client: GraphClient | None, quarter: str
) -> tuple[list[Fact], list[Governance], SourceRun]:
    spec = _source(config, "og_scorecard")
    if client is None or spec is None:
        return [], [], SourceRun("og_scorecard", "missing", "no Graph client", _now())
    try:
        item = client.item(spec["drive_id"], spec["item_id"])
        path = client.download(spec["drive_id"], item)
    except GraphError as exc:
        return [], [], SourceRun("og_scorecard", "error", str(exc), _now())

    facts, governance = og_scorecard.parse(
        path, units=config["business_units"], period=spec.get("period", quarter)
    )
    status = "ok" if facts else "missing"
    return facts, governance, SourceRun(
        "og_scorecard",
        status,
        f"{len(facts)} facts across {len(governance)} units" if facts else "scanner matched no unit rows",
        _now(),
        item.last_modified,
        len(facts),
    )


def _load_omegro_monthly(
    config: dict[str, Any], client: GraphClient | None, period: str, quarter: str
) -> tuple[list[Fact], list[Commentary], SourceRun]:
    """The group's actual financials.

    Live path: pull the period's NR/EBITA pack from the Finance & Accounting
    Community library and parse it. Offline path: use the committed extract of
    the same workbooks, which carries its own provenance and is reproduced
    exactly by the parser on the next authenticated refresh.
    """
    spec = _source(config, "omegro_monthly")
    units = config["business_units"]

    if client is not None and spec is not None:
        try:
            folder = spec["path"].format(year=period.split("-")[0], month_folder=_month_folder(period))
            children = [c for c in client.children(spec["drive_id"], folder) if not c.is_folder]
            matched = [c for c in children if fnmatch.fnmatch(c.name.lower(), spec["match"].lower())]
            if matched:
                facts: list[Fact] = []
                commentary: list[Commentary] = []
                modified = None
                for item in sorted(matched, key=lambda c: c.last_modified)[-2:]:
                    path = client.download(spec["drive_id"], item)
                    f, c = omegro_monthly.parse(
                        path, units=units, period=period, quarter=quarter
                    )
                    facts += f
                    commentary += c
                    modified = item.last_modified
                facts += omegro_monthly.derive_margin(facts)
                return facts, commentary, SourceRun(
                    "omegro_monthly",
                    "ok",
                    ", ".join(i.name for i in matched),
                    _now(),
                    modified,
                    len(facts),
                )
        except GraphError as exc:
            return [], [], SourceRun("omegro_monthly", "error", str(exc), _now())

    path = EXTRACT_DIR / f"{period}.json"
    if not path.exists():
        return [], [], SourceRun(
            "omegro_monthly", "missing", f"no live pack and no extract for {period}", _now()
        )

    raw = json.loads(path.read_text())
    facts = [Fact(**f) for f in raw.get("facts", [])]
    commentary = [Commentary(**c) for c in raw.get("commentary", [])]
    return facts, commentary, SourceRun(
        "omegro_monthly",
        "ok",
        f"{raw.get('_source', path.name)} — committed extract read {raw.get('_read_at', 'unknown')}",
        _now(),
        rows=len(facts),
    )


def _month_folder(period: str) -> str:
    """Finance names the month folders "8. Aug", "11. Nov"."""
    month = int(period.split("-")[1])
    name = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[month - 1]
    return f"{month}. {name}"


# -- orchestration ----------------------------------------------------------


def build(
    *,
    period: str | None = None,
    config_dir: Path = CONFIG_DIR,
    client: GraphClient | None = None,
    offline: Path | None = None,
) -> Snapshot:
    config = load_config(config_dir)
    # Out-of-scope units are kept in config so their aliases still match — a
    # divesting business lingers in the BPC row band after it leaves the group
    # — but nothing they carry reaches the snapshot.
    all_units = config["business_units"]
    config["business_units"] = [u for u in all_units if u.get("in_scope", True)]
    in_scope = {u["key"] for u in config["business_units"]}
    period = period or default_period()
    quarter = quarter_of(period)

    facts: list[Fact] = []
    initiatives: list[Initiative] = []
    commentary: list[Commentary] = []
    governance: list[Governance] = []
    runs: list[SourceRun] = []

    itds, itds_run = _load_itds(config, client, offline)
    runs.append(itds_run)

    m_facts, m_initiatives, m_gov, m_run = _load_monthly(config, client, period)
    facts += m_facts
    initiatives += m_initiatives
    governance += m_gov
    runs.append(m_run)

    om_facts, om_commentary, om_run = _load_omegro_monthly(config, client, period, quarter)
    facts += om_facts
    commentary += om_commentary
    runs.append(om_run)

    og_facts, og_gov, og_run = _load_og(config, client, quarter)
    facts += og_facts
    runs.append(og_run)
    # Monthly submissions are the BU's own statement of stage and status; the
    # scorecard is the portfolio's. Where both exist the submission wins for
    # status and the scorecard fills the score.
    by_bu = {g.bu: g for g in governance}
    for g in og_gov:
        if existing := by_bu.get(g.bu):
            existing.stage = existing.stage or g.stage
            existing.score = existing.score if existing.score is not None else g.score
        else:
            by_bu[g.bu] = g
    governance = list(by_bu.values())

    facts = [f for f in facts if f.bu in in_scope]
    commentary = [c for c in commentary if c.bu in in_scope]
    initiatives = [i for i in initiatives if i.bu in in_scope]
    governance = [g for g in governance if g.bu in in_scope]

    return Snapshot(
        generated_at=_now(),
        period=period,
        quarter=quarter,
        group=config["group"],
        business_units=config["business_units"],
        facts=facts,
        initiatives=initiatives,
        commentary=commentary,
        governance=governance,
        itds=itds,
        runs=runs,
    )
