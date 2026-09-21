"""Detect which dashboard sections have new source material.

The weekly scan answers one question per section of the dashboard: has the
workbook behind it been republished since we last looked? It deliberately
compares against a recorded watermark (`data/seen.json`) rather than against
the snapshot, because a source can be updated in SharePoint in a week when the
refresh did not run, and we still want to say so.

A source appearing for the first time is reported as prominently as one that
changed — `bu_monthly_submissions` turning up is the event that finally fills
the working-capital pane, and it would otherwise read as "no change".
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .graph import DriveItem, GraphClient, GraphError

SEEN_PATH = Path("data/seen.json")

# Reading order of the dashboard, so the email lists sections the way the page
# does rather than the order sources happen to sit in config.
SECTION_ORDER = ["P&L", "Working capital", "Full year", "Operational governance", "ITDS"]

# How deep to walk when a folder holds only subfolders. Finance files the
# monthly pack under "Monthly Reporting / <year> / <n>. <Mon>", so two levels
# covers it without turning a scan into a crawl.
MAX_DESCENT = 2


@dataclass
class SourceState:
    """What the last scan saw for one source."""

    name: str | None = None
    modified: str | None = None
    checked_at: str | None = None


@dataclass
class SourceChange:
    source_id: str
    section: str
    label: str
    status: str                      # new | updated | unchanged | missing | error
    file_name: str | None = None
    web_path: str | None = None
    modified: str | None = None
    previous_modified: str | None = None
    detail: str = ""

    @property
    def notable(self) -> bool:
        """Worth putting in front of someone on a Monday morning."""
        return self.status in {"new", "updated", "error"}


@dataclass
class ScanResult:
    scanned_at: str
    changes: list[SourceChange] = field(default_factory=list)

    @property
    def notable(self) -> list[SourceChange]:
        return [c for c in self.changes if c.notable]

    @property
    def sections_changed(self) -> list[str]:
        seen: list[str] = []
        for c in self.changes:
            if c.status in {"new", "updated"} and c.section not in seen:
                seen.append(c.section)
        return seen

    @property
    def errors(self) -> list[SourceChange]:
        return [c for c in self.changes if c.status == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {"scanned_at": self.scanned_at, "changes": [asdict(c) for c in self.changes]}


def load_seen(path: Path = SEEN_PATH) -> dict[str, SourceState]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: SourceState(**v) for k, v in raw.items()}


def save_seen(seen: dict[str, SourceState], path: Path = SEEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: asdict(v) for k, v in seen.items()}, indent=2, sort_keys=True))


def _newest_file(
    client: GraphClient, drive_id: str, path: str, pattern: str, depth: int = MAX_DESCENT
) -> tuple[DriveItem | None, str]:
    """Newest matching file at `path`, descending into dated subfolders.

    Finance organises by period, so the folder a source names is often a
    parent of year and month folders rather than the folder holding the
    workbook. Truncating a `{year}/{month_folder}` path and stopping there
    finds only directories and reports the source missing every week — so
    when a folder yields no match, descend into its most recent subfolder.
    """
    try:
        children = list(client.children(drive_id, path))
    except GraphError:
        # The folder for this period may not have been created yet. That is a
        # normal state mid-cycle, not a failure of the scan.
        return None, path
    files = [
        c for c in children
        if not c.is_folder and fnmatch.fnmatch(c.name.lower(), pattern)
    ]
    if files:
        return max(files, key=lambda c: c.last_modified), path

    folders = [c for c in children if c.is_folder]
    if not folders or depth <= 0:
        return None, path
    newest = max(folders, key=lambda c: c.last_modified)
    return _newest_file(
        client, drive_id, f"{path}/{newest.name}".strip("/"), pattern, depth - 1
    )


def _resolve(
    client: GraphClient, spec: dict[str, Any], tokens: dict[str, str]
) -> tuple[DriveItem | None, str]:
    """The file a source currently points at.

    Path placeholders are filled from `tokens` where known; an unknown one is
    truncated and the walk above recovers the rest.
    """
    drive_id = spec.get("drive_id")
    if not drive_id:
        # Some sources are registered by site id before their library has been
        # resolved. That is a config gap, not a reason to abandon the scan.
        raise GraphError(
            f"{spec['id']}: no drive_id — registered by site only, resolve its "
            "document library before it can be watched"
        )

    if item_id := spec.get("item_id"):
        return client.item(drive_id, item_id), spec.get("path", "")

    raw_path = spec.get("path", "")
    path = raw_path
    pattern = (spec.get("match") or "*").lower()
    if "{" in pattern:
        # A per-BU pattern cannot be resolved without a unit; match broadly and
        # let the newest file in the folder stand for the source.
        pattern = "*"

    # Try the current period's folder first, then fall back to the parent of
    # the placeholder and let the walk find the most recent one. Finance may
    # not have cut this period's folder yet, and "the latest thing filed" is
    # the honest answer when they have not.
    candidates: list[str] = []
    substituted = raw_path
    for token, value in tokens.items():
        substituted = substituted.replace("{" + token + "}", value)
    if "{" not in substituted:
        candidates.append(substituted)
    if "{" in raw_path:
        candidates.append(raw_path.split("{", 1)[0].rstrip("/"))
    elif not candidates:
        candidates.append(raw_path)

    item, path = None, ""
    for candidate in candidates:
        if not candidate:
            continue
        item, found_in = _newest_file(client, drive_id, candidate, pattern)
        if item is not None:
            path = found_in
            break
    if item is None:
        # Report the folder we actually looked in, not the template.
        return None, next((c for c in candidates if c), "")

    # Where a quarter has both variants, prefer the one the parser wants.
    marker = (spec.get("prefer") or "").lower()
    if marker and marker not in item.name.lower():
        siblings = [
            c for c in client.children(drive_id, path)
            if not c.is_folder
            and marker in c.name.lower()
            and c.last_modified == item.last_modified
        ]
        if siblings:
            return siblings[0], path
    return item, path


def period_tokens(config: dict[str, Any], today: date | None = None) -> dict[str, str]:
    """Values for the `{placeholders}` source paths use."""
    from .model import default_period, quarter_of

    period = default_period(today)
    year, month = period.split("-")
    name = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[int(month) - 1]
    quarter = quarter_of(period)
    baseline = next(
        (
            str(s.get("baseline_qsr_folder"))
            for s in config.get("sources", [])
            if s.get("baseline_qsr_folder")
        ),
        "",
    )
    return {
        "year": year,
        "month_folder": f"{int(month)}. {name}",
        "qsr_folder": f"{year}.{month}",
        "baseline_qsr_folder": baseline,
        "quarter": quarter,
    }


def scan(
    config: dict[str, Any],
    client: GraphClient,
    *,
    seen: dict[str, SourceState] | None = None,
    today: date | None = None,
) -> tuple[ScanResult, dict[str, SourceState]]:
    """Check every source that feeds a dashboard section.

    Returns the result and the updated watermarks. The caller decides whether
    to persist them — a scan that is only being previewed should not move the
    watermark, or the next real scan reports nothing.
    """
    seen = dict(seen or {})
    tokens = period_tokens(config, today)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes: list[SourceChange] = []

    for spec in config["sources"]:
        section = spec.get("section")
        if not section:
            continue  # reference material, not a dashboard pane

        source_id = spec["id"]
        previous = seen.get(source_id, SourceState())
        label = spec.get("label", source_id)

        try:
            item, resolved = _resolve(client, spec, tokens)
        except GraphError as exc:
            changes.append(
                SourceChange(
                    source_id, section, label, "error",
                    previous_modified=previous.modified, detail=str(exc),
                )
            )
            continue

        if item is None:
            changes.append(
                SourceChange(
                    source_id, section, label, "missing",
                    previous_modified=previous.modified,
                    web_path=f"{spec.get('site', '')}/{resolved}".strip("/"),
                    detail=f"nothing in {resolved or spec.get('path', '?')} "
                           f"matching {spec.get('match', '*')}",
                )
            )
            continue

        if previous.modified is None:
            status = "new"
        elif item.last_modified != previous.modified or item.name != previous.name:
            status = "updated"
        else:
            status = "unchanged"

        changes.append(
            SourceChange(
                source_id=source_id,
                section=section,
                label=label,
                status=status,
                file_name=item.name,
                web_path=f"{spec.get('site', '')}/{resolved}".strip("/"),
                modified=item.last_modified,
                previous_modified=previous.modified,
            )
        )
        seen[source_id] = SourceState(
            name=item.name, modified=item.last_modified, checked_at=now
        )

    # Dashboard reading order, changed first within each section.
    changes.sort(
        key=lambda c: (
            SECTION_ORDER.index(c.section) if c.section in SECTION_ORDER else len(SECTION_ORDER),
            not c.notable,
            c.source_id,
        )
    )

    return ScanResult(scanned_at=now, changes=changes), seen
