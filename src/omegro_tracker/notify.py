"""Compose and send the weekly alert.

The email has one job: say which dashboard sections have new material, name
the file, and link to the refreshed dashboard. It is read on a phone on a
Monday morning, so the answer is in the subject line and the first two lines
of the body.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from .scan import ScanResult, SourceChange

STATUS_LABEL = {
    "new": "New",
    "updated": "Updated",
    "unchanged": "No change",
    "missing": "Not found",
    "error": "Could not read",
}

STATUS_COLOUR = {
    "new": "#0ca30c",
    "updated": "#0ca30c",
    "unchanged": "#818b88",
    "missing": "#818b88",
    "error": "#d03b3b",
}


def _stamp(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y %H:%M")
    except ValueError:
        return iso


def subject(result: ScanResult, group_name: str = "Turner Group") -> str:
    """Say what happened, not that something happened.

    "Weekly scan complete" makes the reader open the mail to find out whether
    it matters; naming the sections means they often need not.
    """
    if result.errors and not result.sections_changed:
        return f"{group_name} tracker — {len(result.errors)} source(s) could not be read"

    sections = result.sections_changed
    if not sections:
        return f"{group_name} tracker — no source updates this week"

    named = ", ".join(sections[:3])
    if len(sections) > 3:
        named += f" and {len(sections) - 3} more"
    suffix = " (with read errors)" if result.errors else ""
    return f"{group_name} tracker — {named} updated{suffix}"


def _headline(result: ScanResult) -> str:
    sections = result.sections_changed
    if not sections:
        base = "No source workbooks changed since the last scan."
    elif len(sections) == 1:
        base = f"New source material for {sections[0]}."
    else:
        base = f"New source material for {len(sections)} sections: {', '.join(sections)}."
    if result.errors:
        base += f" {len(result.errors)} source(s) could not be read — listed below."
    return base


def text_body(result: ScanResult, *, dashboard_url: str, group_name: str = "Turner Group") -> str:
    lines = [
        f"{group_name} tracker — weekly source scan",
        f"Scanned {_stamp(result.scanned_at)} UTC",
        "",
        _headline(result),
        "",
        f"Dashboard: {dashboard_url}",
        "",
    ]
    section = None
    for change in result.changes:
        if change.section != section:
            section = change.section
            lines.append(f"{section}")
        lines.append(
            f"  [{STATUS_LABEL[change.status]}] {change.file_name or change.label}"
            + (f" — {_stamp(change.modified)}" if change.modified else "")
            + (f" — {change.detail}" if change.detail else "")
        )
    return "\n".join(lines)


def _row(change: SourceChange) -> str:
    colour = STATUS_COLOUR[change.status]
    weight = "600" if change.notable else "400"
    name = html.escape(change.file_name or change.label)
    detail = html.escape(change.detail) if change.detail else ""
    where = html.escape(change.web_path or "")
    return f"""
      <tr>
        <td style="padding:8px 12px;border-bottom:1px solid #e2e6e4;font-size:13px;vertical-align:top">
          <span style="color:{colour};font-weight:600">{STATUS_LABEL[change.status]}</span>
        </td>
        <td style="padding:8px 12px;border-bottom:1px solid #e2e6e4;font-size:13px;vertical-align:top">
          <div style="font-weight:{weight};color:#141817">{name}</div>
          {f'<div style="color:#818b88;font-size:11.5px;margin-top:2px">{where}</div>' if where else ''}
          {f'<div style="color:#4f5956;font-size:12px;margin-top:3px">{detail}</div>' if detail else ''}
        </td>
        <td style="padding:8px 12px;border-bottom:1px solid #e2e6e4;font-size:12.5px;
                   color:#4f5956;white-space:nowrap;vertical-align:top">
          {_stamp(change.modified)}
        </td>
      </tr>"""


def html_body(result: ScanResult, *, dashboard_url: str, group_name: str = "Turner Group") -> str:
    """Inline styles throughout — email clients discard <style> blocks."""
    rows: list[str] = []
    section = None
    for change in result.changes:
        if change.section != section:
            section = change.section
            rows.append(
                f"""
      <tr><td colspan="3" style="padding:16px 12px 6px;font-size:11px;font-weight:700;
              letter-spacing:.07em;text-transform:uppercase;color:#818b88">
        {html.escape(section)}
      </td></tr>"""
            )
        rows.append(_row(change))

    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:680px;margin:0 auto;color:#141817;line-height:1.5">
  <div style="border-left:3px solid #0d4f52;padding-left:14px;margin-bottom:20px">
    <div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#818b88">
      {html.escape(group_name)} &middot; weekly source scan
    </div>
    <h1 style="margin:4px 0 0;font-size:19px;font-weight:700">{html.escape(_headline(result))}</h1>
    <div style="font-size:12.5px;color:#4f5956;margin-top:4px">
      Scanned {_stamp(result.scanned_at)} UTC
    </div>
  </div>

  <a href="{html.escape(dashboard_url)}"
     style="display:inline-block;background:#0d4f52;color:#ffffff;text-decoration:none;
            padding:10px 18px;border-radius:3px;font-size:14px;font-weight:600">
    Open the refreshed dashboard
  </a>

  <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin-top:22px">
    <tbody>{''.join(rows)}</tbody>
  </table>

  <p style="font-size:11.5px;color:#818b88;margin-top:22px;line-height:1.6">
    Sent by omegro-tracker. A section is flagged when the workbook behind it has been
    republished in SharePoint since the last scan; the dashboard link always shows the
    most recent successful refresh.
  </p>
</div>"""


def build(
    result: ScanResult, config: dict[str, Any]
) -> tuple[str, str, str]:
    """Returns (subject, html, text) for the configured group."""
    group = config.get("group", {})
    name = group.get("name", "Turner Group")
    url = group.get("dashboard_url", "")
    return (
        subject(result, name),
        html_body(result, dashboard_url=url, group_name=name),
        text_body(result, dashboard_url=url, group_name=name),
    )
