"""Canonical data model.

Everything the pipeline extracts lands in one flat fact table. Sources disagree
about shape — the OG scorecard is a wide quarterly matrix, the monthly review is
a metric-by-basis block, the ITDS assessment is a control register — so each
parser normalises into `Fact` and nothing downstream needs to know where a
number came from.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable, Iterator, Literal

# A reporting basis. `month` is the single closed month; `qtd`/`ytd` are
# cumulative within the current quarter/year; `quarter` is the full quarter and
# is where the outturn (projection) lives; `next_quarter` carries forward
# visibility only.
Basis = Literal["month", "qtd", "ytd", "quarter", "next_quarter", "year"]

# What the number *is*. These are not interchangeable and the distinction
# drives every variance on the page:
#
#   baseline   the plan set at the start of the year. Fixed for the year.
#   forecast   the most recent QSR iteration of the expected position. Moves
#              as the year progresses, so a variance against forecast says
#              "against what we last told each other", not "against plan".
#   actual     booked in the GL.
#   projection the current best estimate for a period still running — what
#              the group calls the outturn.
#   target     an operational goal, used for working capital rather than P&L.
#   prior      the same measure one period earlier.
#
# Measuring the full year against `forecast` would compare the latest estimate
# with itself; the year is measured against `baseline`.
Measure = Literal["forecast", "actual", "projection", "baseline", "target", "prior"]

Status = Literal["on_track", "at_risk", "off_track", "unknown"]

STATUS_LABELS: dict[str, str] = {
    "on_track": "On Track",
    "at_risk": "At Risk",
    "off_track": "Off Track",
    "unknown": "Not Reported",
}


@dataclass(frozen=True)
class Fact:
    """One number, fully qualified.

    `period` is ISO-ish: "2026-08" for a month, "2026-Q3" for a quarter,
    "2026" for a year. `value` is None when a source was reached but the cell
    was blank — that is different from the fact being absent entirely, and the
    dashboard shows it as "not reported" rather than zero.
    """

    bu: str
    metric: str
    basis: Basis
    measure: Measure
    period: str
    value: float | None
    unit: str = "kGBP"
    source: str = "unknown"
    as_of: str | None = None

    @property
    def id(self) -> tuple[str, str, str, str, str]:
        return (self.bu, self.metric, self.basis, self.measure, self.period)


@dataclass
class Initiative:
    """A row from the Improvement Plan / IM Value Drivers block, or a Working
    Capital action. Owner initials come through in the source as a suffix on the
    initiative name (e.g. "Sales bookings (RS)")."""

    bu: str
    title: str
    owner: str
    measure_of_success: str
    target_date: str | None
    status: Status
    notes: str
    kind: Literal["improvement", "working_capital"] = "improvement"


@dataclass
class Commentary:
    """A preparer's own explanation of a variance, lifted verbatim.

    The monthly pack requires commentary wherever a variance breaches the
    materiality threshold, and that text is the most useful thing on the page —
    it says *why*. It is quoted, never paraphrased.
    """

    bu: str
    metric: str
    period: str
    text: str
    source: str = "unknown"
    author: str | None = None


@dataclass
class Governance:
    """Operational Governance position for one BU at one quarter end."""

    bu: str
    period: str
    stage: str | None = None
    score: float | None = None
    score_max: float | None = None
    overall_status: Status = "unknown"
    commentary: str = ""


@dataclass
class KeyArea:
    """One of the three ITDS key areas the portfolio tracks."""

    name: str
    status: Literal["good", "warning", "critical", "unknown"]
    narrative: str


@dataclass
class ITDS:
    """ITDS position from the Portfolio Security Assessment.

    `risk_current` and `risk_initial` are out of `risk_max` (400) and *lower is
    better*. `movement` is current minus initial: positive means residual risk
    has increased since the baseline assessment.
    """

    bu: str
    period: str
    posture: str | None = None
    risk_current: float | None = None
    risk_initial: float | None = None
    risk_max: float = 400.0
    controls_assessed: int | None = None
    controls_strong: int | None = None        # scored >= 7
    controls_focus: int | None = None         # scored < 7
    controls_low: int | None = None           # scored 0-3
    average_kri: float | None = None
    not_met: int | None = None
    largely_not_met: int | None = None
    partially_met: int | None = None
    key_areas: list[KeyArea] = field(default_factory=list)
    summary: str = ""
    source_file: str | None = None

    @property
    def movement(self) -> float | None:
        if self.risk_current is None or self.risk_initial is None:
            return None
        return self.risk_current - self.risk_initial

    @property
    def risk_pct(self) -> float | None:
        if self.risk_current is None or not self.risk_max:
            return None
        return self.risk_current / self.risk_max


@dataclass
class SourceRun:
    """Provenance for one source in one refresh. Rendered in the dashboard
    footer so a reader can always tell how old each pane is and whether a pane
    is showing live data or a seeded placeholder."""

    source_id: str
    status: Literal["ok", "missing", "error", "seed"]
    detail: str = ""
    fetched_at: str | None = None
    item_modified: str | None = None
    rows: int = 0


@dataclass
class Snapshot:
    """The whole dashboard, serialisable. `render` takes exactly this and
    nothing else, so the HTML can be rebuilt offline from a committed
    snapshot."""

    generated_at: str
    period: str                      # reporting month, e.g. "2026-08"
    quarter: str                     # e.g. "2026-Q3"
    group: dict[str, Any] = field(default_factory=dict)
    business_units: list[dict[str, Any]] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    initiatives: list[Initiative] = field(default_factory=list)
    commentary: list[Commentary] = field(default_factory=list)
    governance: list[Governance] = field(default_factory=list)
    itds: list[ITDS] = field(default_factory=list)
    runs: list[SourceRun] = field(default_factory=list)

    # -- lookup helpers -----------------------------------------------------

    def fact(
        self, bu: str, metric: str, basis: str, measure: str, period: str | None = None
    ) -> Fact | None:
        period = period or self.period
        for f in self.facts:
            if (f.bu, f.metric, f.basis, f.measure, f.period) == (
                bu,
                metric,
                basis,
                measure,
                period,
            ):
                return f
        return None

    def value(
        self, bu: str, metric: str, basis: str, measure: str, period: str | None = None
    ) -> float | None:
        f = self.fact(bu, metric, basis, measure, period)
        return f.value if f else None

    def itds_for(self, bu: str) -> ITDS | None:
        return next((i for i in self.itds if i.bu == bu), None)

    def governance_for(self, bu: str) -> Governance | None:
        return next((g for g in self.governance if g.bu == bu), None)

    def initiatives_for(self, bu: str, kind: str | None = None) -> list[Initiative]:
        return [
            i
            for i in self.initiatives
            if i.bu == bu and (kind is None or i.kind == kind)
        ]

    def commentary_for(self, bu: str, metric: str | None = None) -> list[Commentary]:
        return [
            c
            for c in self.commentary
            if c.bu == bu and (metric is None or c.metric == metric)
        ]

    def run(self, source_id: str) -> SourceRun | None:
        return next((r for r in self.runs if r.source_id == source_id), None)

    @property
    def has_live_financials(self) -> bool:
        """True once a financial source has actually returned data. Drives the
        provenance banner — a dashboard showing seeded figures must say so."""
        return any(
            r.status == "ok"
            and r.source_id
            in {"omegro_monthly", "bu_monthly_submissions", "og_scorecard", "qsr_submissions"}
            for r in self.runs
        )

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # `movement` / `risk_pct` are properties, so asdict drops them; the
        # renderer recomputes from the dataclass, but a committed snapshot
        # should be readable on its own.
        for raw, obj in zip(d["itds"], self.itds):
            raw["movement"] = obj.movement
            raw["risk_pct"] = obj.risk_pct
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        return cls(
            generated_at=d["generated_at"],
            period=d["period"],
            quarter=d["quarter"],
            group=d.get("group", {}),
            business_units=d.get("business_units", []),
            facts=[Fact(**f) for f in d.get("facts", [])],
            initiatives=[Initiative(**i) for i in d.get("initiatives", [])],
            commentary=[Commentary(**c) for c in d.get("commentary", [])],
            governance=[Governance(**g) for g in d.get("governance", [])],
            itds=[
                ITDS(
                    **{
                        **{k: v for k, v in i.items() if k not in {"key_areas", "movement", "risk_pct"}},
                        "key_areas": [KeyArea(**k) for k in i.get("key_areas", [])],
                    }
                )
                for i in d.get("itds", [])
            ],
            runs=[SourceRun(**r) for r in d.get("runs", [])],
        )


# -- period arithmetic ------------------------------------------------------


def quarter_of(period: str) -> str:
    """"2026-08" -> "2026-Q3"."""
    year, month = period.split("-")
    return f"{year}-Q{(int(month) - 1) // 3 + 1}"


def next_quarter(quarter: str) -> str:
    year, q = quarter.split("-Q")
    n = int(q) + 1
    return f"{int(year) + 1}-Q1" if n > 4 else f"{year}-Q{n}"


def months_in_quarter(quarter: str) -> list[str]:
    year, q = quarter.split("-Q")
    start = (int(q) - 1) * 3 + 1
    return [f"{year}-{m:02d}" for m in range(start, start + 3)]


def month_label(period: str) -> str:
    """"2026-08" -> "Aug-26", the format the Nelson templates use."""
    year, month = period.split("-")
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{names[int(month) - 1]}-{year[2:]}"


def prior_month(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def default_period(today: date | None = None) -> str:
    """The month most likely to be closed.

    Submissions are due within 7 business days of month-end, so before roughly
    the 10th the latest closed month is two back, not one.
    """
    today = today or date.today()
    back = 2 if today.day < 10 else 1
    y, m = today.year, today.month - back
    while m < 1:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def iter_facts(facts: Iterable[Fact]) -> Iterator[Fact]:
    return iter(facts)
