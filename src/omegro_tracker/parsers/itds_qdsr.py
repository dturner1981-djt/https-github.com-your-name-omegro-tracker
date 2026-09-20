"""Parse the QDSR Validation Assessment dashboard into ITDS records.

The portfolio publishes its ITDS position as a single self-contained HTML
report covering all 14 Nelson VBUs. Each VBU gets a `<section id="vbu-...">`
containing a summary grid, a control-effectiveness snapshot and a focus
population list; the report also opens with a comparison table carrying posture
and current risk for every VBU.

Parsing HTML is not ideal, but this report *is* the published position — the
per-BU assessment workbooks behind it are not consistently filed, and the
narrative in the key-area cells exists nowhere else. Where the workbooks are
available, `itds_workbook.py` supersedes this.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

from ..model import ITDS, KeyArea

# Status is carried as an emoji in the key-area paragraphs. The report uses
# exactly these three.
_ICON_STATUS = {"✅": "good", "⚠️": "warning", "⚠": "warning", "❌": "critical"}

_KEY_AREAS = {
    "backup resilience": "Backup resilience",
    "iam and mfa": "IAM & MFA",
    "iam & mfa": "IAM & MFA",
    "detection & response": "Detection & Response",
    "detection and response": "Detection & Response",
}


def _text(fragment: str) -> str:
    """Strip tags, collapse whitespace, unescape entities."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", raw.replace("%", ""))
    return float(m.group().replace(",", "")) if m else None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _match_bu(label: str, units: Iterable[dict[str, Any]]) -> str | None:
    """Resolve a report label to a configured BU key via the alias list.

    The report calls them "TBL", "tlmNexus" and "Grosvenor Systems"; config
    knows those as aliases of tbl / tlm / grosvenor.
    """
    needle = re.sub(r"[^a-z0-9]", "", label.lower())
    for unit in units:
        for alias in [unit["name"], unit.get("short_name", ""), *unit.get("aliases", [])]:
            if alias and re.sub(r"[^a-z0-9]", "", alias.lower()) == needle:
                return unit["key"]
    return None


def _key_areas(fragment: str) -> list[KeyArea]:
    areas: list[KeyArea] = []
    for para in re.findall(r'<p class="key-area">(.*?)</p>', fragment, re.S):
        icon = next((i for i in _ICON_STATUS if i in para), None)
        body = _text(para)
        m = re.match(r"^\s*[^\w]*\s*([^:]+):\s*(.*)$", body)
        if not m:
            continue
        raw_name = m.group(1).strip().lstrip("✅⚠️❌ ").strip()
        name = _KEY_AREAS.get(raw_name.lower(), raw_name)
        areas.append(
            KeyArea(
                name=name,
                status=_ICON_STATUS.get(icon or "", "unknown"),  # type: ignore[arg-type]
                narrative=m.group(2).strip(),
            )
        )
    return areas


def _summary_table(doc: str, units: list[dict[str, Any]], period: str) -> dict[str, ITDS]:
    """The opening portfolio comparison table: posture, current risk, summary,
    key-area narratives. Present for every VBU, so it is the baseline."""
    out: dict[str, ITDS] = {}
    table = re.search(r'<table class="exec-summary-table".*?</table>', doc, re.S)
    if not table:
        return out

    for row in re.findall(r"<tr>(.*?)</tr>", table.group(), re.S):
        cells = re.findall(r"<td.*?>(.*?)</td>", row, re.S)
        if len(cells) < 6:
            continue
        key = _match_bu(_text(cells[0]), units)
        if not key:
            continue
        out[key] = ITDS(
            bu=key,
            period=period,
            posture=_text(cells[1]) or None,
            risk_current=_num(_text(cells[2])),
            summary=_text(cells[4]),
            key_areas=_key_areas(cells[5]),
        )
    return out


def _detail_sections(doc: str, units: list[dict[str, Any]], into: dict[str, ITDS]) -> None:
    """Per-VBU pages carry initial risk, the control-effectiveness snapshot,
    average KRI and the published focus population. Enrich in place."""
    for unit in units:
        record = into.get(unit["key"])
        if record is None:
            continue

        # The per-VBU page is addressed either by anchor or by the "VBU name"
        # row; the anchor slug is derived from the report's own label, which we
        # do not know ahead of time, so match on the name row.
        section = None
        for alias in [unit["name"], unit.get("short_name", ""), *unit.get("aliases", [])]:
            if not alias:
                continue
            m = re.search(
                r"<strong>VBU name:</strong>\s*" + re.escape(alias) + r"\s*</li>(.{0,4000})",
                doc,
                re.S | re.I,
            )
            if m:
                section = m.group(1)
                break
            m = re.search(r'id="vbu-' + re.escape(_slug(alias)) + r'"(.{0,12000})', doc, re.S)
            if m:
                section = m.group(1)
                break
        if section is None:
            continue

        # Each snapshot tile is label -> value -> description, in that order, so
        # a regex that starts from the description and scans forward picks up
        # the *next* tile's value. Parse whole tiles and index them instead.
        tiles: list[tuple[str, str, str]] = re.findall(
            r'effectiveness-label">(.*?)</div>\s*'
            r'<div class="effectiveness-value">(.*?)</div>\s*'
            r'<div class="effectiveness-desc">(.*?)</div>',
            section,
            re.S,
        )

        def tile(*needles: str) -> str | None:
            for label, value, desc in tiles:
                haystack = f"{_text(label)} {_text(desc)}".lower()
                if any(n.lower() in haystack for n in needles):
                    return _text(value)
            return None

        def tile_int(*needles: str) -> int | None:
            raw = tile(*needles)
            return int(float(raw.replace(",", ""))) if raw else None

        record.controls_strong = tile_int("controls at 7 or above", "stronger")
        record.controls_focus = tile_int("controls below 7", "focus")
        # Match on the range itself — "low" as a needle also hits "Controls
        # below 7", which is the focus tile.
        record.controls_low = tile_int("0–3 range", "0-3 range")
        if kri := tile("average kri"):
            record.average_kri = float(kri)
        if m := re.search(r"(\d+)\s+controls assessed", _text(section), re.I):
            record.controls_assessed = int(m.group(1))
        if m := re.search(r"Initial risk total.*?value\">([\d.,]+)<", section, re.S):
            record.risk_initial = _num(m.group(1))
        if m := re.search(r"Current risk total.*?value\">([\d.,]+)<", section, re.S):
            record.risk_current = _num(m.group(1)) or record.risk_current

        flat = _text(section)
        # "Risk movement is 103.0 points higher than initial" / "unchanged from initial"
        if record.risk_initial is None and record.risk_current is not None:
            if re.search(r"risk movement is unchanged", flat, re.I):
                record.risk_initial = record.risk_current
            elif m := re.search(r"risk movement is ([\d.,]+) points (higher|lower)", flat, re.I):
                delta = float(m.group(1).replace(",", ""))
                record.risk_initial = record.risk_current - (
                    delta if m.group(2).lower() == "higher" else -delta
                )

        for field, label in (
            ("not_met", "Not met"),
            ("largely_not_met", "Largely not met"),
            ("partially_met", "Partially met"),
        ):
            if m := re.search(re.escape(label) + r":?\s*\|?\s*(\d+)", flat):
                setattr(record, field, int(m.group(1)))

        # The workbook reference sits in the section hero, above the "VBU name"
        # row we anchored on, so look it up across the whole document.
        for alias in [unit["name"], unit.get("short_name", ""), *unit.get("aliases", [])]:
            if not alias:
                continue
            if m := re.search(
                r"ITDS Assessment - " + re.escape(alias) + r" - [^.<|\"]+\.xlsx", doc
            ):
                record.source_file = m.group().strip()
                break


def parse(
    content: str,
    *,
    units: list[dict[str, Any]],
    period: str,
    risk_max: float = 400.0,
) -> list[ITDS]:
    """Parse a QDSR report. Returns one record per configured BU found."""
    records = _summary_table(content, units, period)
    _detail_sections(content, units, records)
    for record in records.values():
        record.risk_max = risk_max
    return [records[u["key"]] for u in units if u["key"] in records]
