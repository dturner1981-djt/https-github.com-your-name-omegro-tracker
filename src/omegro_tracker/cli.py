"""Command line entry point.

    omegro-tracker refresh            pull sources -> data/snapshot.json
    omegro-tracker render             snapshot.json -> dist/index.html
    omegro-tracker build              refresh + render
    omegro-tracker validate           report what each source parser matched
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import CONFIG_DIR, build as build_snapshot, load_config
from .graph import GraphClient, GraphError
from .model import Snapshot, default_period
from .render import render as render_html

DEFAULT_SNAPSHOT = Path("data/snapshot.json")
DEFAULT_OUTPUT = Path("dist/index.html")


def _client(args: argparse.Namespace, config: dict) -> GraphClient | None:
    if args.offline:
        return None
    graph_cfg = config.get("graph", {})
    kwargs = {
        "download_dir": graph_cfg.get("download_dir", ".cache/graph"),
        "timeout": graph_cfg.get("request_timeout_seconds", 120),
    }
    try:
        if args.device_code:
            import os

            tenant = os.environ.get("OMEGRO_TRACKER_TENANT_ID")
            client_id = os.environ.get("OMEGRO_TRACKER_CLIENT_ID")
            if not (tenant and client_id):
                raise GraphError(
                    "--device-code needs OMEGRO_TRACKER_TENANT_ID and OMEGRO_TRACKER_CLIENT_ID."
                )
            return GraphClient.from_device_code(
                tenant, client_id, graph_cfg.get("scopes", ["Sites.Read.All"]), **kwargs
            )
        return GraphClient.from_env(**kwargs)
    except GraphError as exc:
        print(f"warning: {exc}\n         continuing without live sources.", file=sys.stderr)
        return None


def cmd_refresh(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    client = _client(args, config)
    snapshot = build_snapshot(
        period=args.period,
        config_dir=Path(args.config),
        client=client,
        offline=Path(config["graph"].get("download_dir", ".cache/graph")),
    )
    out = Path(args.snapshot)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(snapshot.to_json())

    print(f"period {snapshot.period} ({snapshot.quarter}) -> {out}")
    for run in snapshot.runs:
        print(f"  [{run.status:7}] {run.source_id:24} {run.detail}")
    if not snapshot.has_live_financials:
        print(
            "\nnote: no live financial source resolved; the dashboard will show the "
            "illustrative placeholders and say so.",
            file=sys.stderr,
        )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    path = Path(args.snapshot)
    if not path.exists():
        print(f"error: {path} not found — run `omegro-tracker refresh` first.", file=sys.stderr)
        return 1
    snapshot = Snapshot.from_dict(json.loads(path.read_text()))
    config = load_config(Path(args.config))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(snapshot, config), encoding="utf-8")
    print(f"{out} ({out.stat().st_size:,} bytes)")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    return cmd_refresh(args) or cmd_render(args)


def cmd_validate(args: argparse.Namespace) -> int:
    """Report what the scanning parsers matched, so a mapping can be confirmed
    against the real workbook before anyone acts on the numbers."""
    from .parsers import og_scorecard

    config = load_config(Path(args.config))
    client = _client(args, config)
    if client is None:
        print("error: validate needs a Graph connection.", file=sys.stderr)
        return 1

    spec = next((s for s in config["sources"] if s["id"] == args.source), None)
    if spec is None:
        print(f"error: unknown source {args.source!r}", file=sys.stderr)
        return 1

    from .build import _pick_scorecard

    if "item_id" in spec:
        item = client.item(spec["drive_id"], spec["item_id"])
    else:
        item = _pick_scorecard(client, spec)
        if item is None:
            print(f"error: nothing in {spec['path']!r} matching {spec['match']!r}", file=sys.stderr)
            return 1
    path = client.download(spec["drive_id"], item)
    print(f"{spec['id']}: {item.name} ({item.size:,} bytes, modified {item.last_modified})")
    print(f"  from {spec['site']} / {spec['path']}")
    if args.source.startswith("og_scorecard"):
        print(og_scorecard.describe(path, config["business_units"]))
    else:
        print(f"  downloaded to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omegro-tracker", description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_DIR), help="config directory")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--offline", action="store_true", help="skip Graph entirely")
    parser.add_argument("--device-code", action="store_true", help="delegated sign-in")

    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn in (("refresh", cmd_refresh), ("build", cmd_build)):
        p = sub.add_parser(name)
        p.add_argument("--period", default=None, help=f"YYYY-MM (default {default_period()})")
        p.add_argument("--output", default=str(DEFAULT_OUTPUT))
        p.set_defaults(func=fn)

    p = sub.add_parser("render")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("validate")
    p.add_argument("--source", default="og_scorecard")
    p.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
