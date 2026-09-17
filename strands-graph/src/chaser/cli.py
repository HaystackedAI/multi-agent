"""Command line interface: python -m chaser.cli sweep|decide <id> yes|ask "..."|status|seed|state."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import service
from .config import configure_logging


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="chaser", description="Chaser weekly-close agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="(re)load the demo dataset into .data/chaser.db")
    sub.add_parser("sweep", help="run one weekly close now")
    sub.add_parser("status", help="counts, last sweep, last report")
    sub.add_parser("state", help="full UI state as JSON")
    sub.add_parser("decisions", help="list pending decisions")

    p_decide = sub.add_parser("decide", help="approve or deny a decision")
    p_decide.add_argument("decision_id")
    p_decide.add_argument("response", help='"yes", "no" or free text')
    p_decide.add_argument("--edits", default="{}", help='JSON object of edits, e.g. \'{"body": "..."}\'')

    p_ask = sub.add_parser("ask", help="ask the agent a question")
    p_ask.add_argument("prompt")

    args = parser.parse_args(argv)

    if args.command == "seed":
        _print({"ok": True, "counts": service.seed()})
    elif args.command == "sweep":
        result = service.run_sweep()
        if result.get("ok"):
            print(f"Cycle {result['cycle_id']}: nodes {' -> '.join(result['execution_order'])}")
            print("\nPending decisions:")
            for d in result["pending_decisions"]:
                print(f"  [{d['id']}] ({d['kind']}) {d['summary']}")
            print("\nActions taken:")
            for a in result["actions_taken"]:
                print(f"  {a['agent']:<11} {a['status']:<8} {a['summary']}")
            print("\nReport narrative:\n  " + str(result["report"].get("narrative", "")))
        else:
            _print(result)
            return 1
    elif args.command == "status":
        _print(service.status())
    elif args.command == "state":
        _print(service.ui_state())
    elif args.command == "decisions":
        _print(service.ui_state()["pending"])
    elif args.command == "decide":
        try:
            edits = json.loads(args.edits)
        except json.JSONDecodeError as exc:
            print(f"--edits must be JSON: {exc}", file=sys.stderr)
            return 2
        _print(service.decide(args.decision_id, args.response, edits))
    elif args.command == "ask":
        print(service.ask(args.prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
