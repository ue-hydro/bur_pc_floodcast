"""``floodcast operate``: the continuously running operational system."""

from __future__ import annotations

import json
import tomllib


def add_operate_arguments(parser) -> None:
    from pathlib import Path

    sub = parser.add_subparsers(dest="action", required=True)
    for name, text in (
        ("run-once", "Process every pending IFS cycle once, then exit"),
        ("daemon", "Poll forever: screen each new IFS cycle and trigger the models when needed"),
        ("spinup", "Warm the hydrological state from archived IFS data (do this before first use)"),
        ("site", "Regenerate the public website from the latest outputs"),
        ("status", "Print the latest published state and daemon health"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--config", type=Path, required=True,
                         help="operational TOML (see examples/operational/)")
        cmd.add_argument("--verbose", action="store_true")
        if name == "run-once":
            cmd.add_argument("--cycle", help="process this cycle (e.g. 2026111812) regardless of age")
        if name == "spinup":
            cmd.add_argument("--end", help="state valid time (default: latest available cycle)")
            cmd.add_argument("--days", type=int, help="spin-up length (default: hydrology.spinup_days)")
            cmd.add_argument("--source", default="aws", choices=("aws", "azure", "google"),
                             help="archive mirror with history")
        if name == "site":
            cmd.add_argument("--out", type=Path, help="output directory (default: publish.output_dir)")


def run_operate(args, config=None, *, json_output=True):
    from pydantic import ValidationError

    from floodcast.core.errors import FloodcastError
    from floodcast.operational import system
    from floodcast.operational.config import OperationalConfig

    try:
        cfg = OperationalConfig.from_file(args.config)
    except (ValidationError, tomllib.TOMLDecodeError, KeyError, FileNotFoundError) as exc:
        raise FloodcastError("INVALID_CONFIG", "operational", str(exc)) from exc
    domain, store, processor = system.build_processor(cfg)
    system.setup_logging(cfg, domain.domain_id, verbose=args.verbose)
    if args.action == "status":
        print(json.dumps(system.status(cfg), indent=2))
        return 0
    if args.action == "spinup":
        end = system.parse_cycle(args.end) if args.end else None
        path = system.spinup(cfg, end=end, days=args.days, source=args.source)
        print(json.dumps({"state": str(path)}, indent=2))
        return 0
    if args.action == "site":
        from floodcast.operational.site import build_site

        out = args.out or cfg.publish.output_dir
        if out is None:
            raise FloodcastError("INVALID_CONFIG", "operational", "no --out and no publish.output_dir")
        files = build_site(store.root, out, site_title=cfg.publish.site_title,
                           base_url=cfg.publish.base_url)
        print(json.dumps({"site": str(out), "files": len(files)}, indent=2))
        return 0
    daemon = system.Daemon(cfg, processor=processor)
    if args.action == "daemon":
        daemon.run_forever()
        return 0
    if args.cycle:
        cycle = system.parse_cycle(args.cycle)
        status = processor.process(cycle, full=True)
        daemon._refresh_site()
        print(json.dumps({"cycle": status["cycle"], "level": status["level"],
                          "stages": status["stages"], "errors": status["errors"]}, indent=2))
        return 0 if not status["errors"] else 1
    done = daemon.run_once()
    daemon._refresh_site()
    print(json.dumps({"processed_cycles": done}, indent=2))
    return 0
