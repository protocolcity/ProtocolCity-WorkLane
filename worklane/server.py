"""WorkLane board server entrypoint.

This keeps runtime ownership under ``worklane`` while reusing the
existing board app factory.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from worklane.task_server import create_app

app = create_app()


def main(argv: Optional[List[str]] = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="worklane",
        description="WorkLane board server",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Before bind, bootstrap the isolated demo product store "
            "(same as `wl demo`; never touches real product DBs)"
        ),
    )
    parser.add_argument(
        "--demo-force",
        action="store_true",
        help="With --demo, wipe and re-seed the demo store only",
    )
    args = parser.parse_args(argv)

    if args.demo or args.demo_force:
        from worklane import demo as demo_mod

        try:
            report = demo_mod.bootstrap_demo(force=bool(args.demo_force))
        except demo_mod.DemoError as exc:
            print(f"Error: demo bootstrap failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(report["message"])
        print(
            f"Demo board: http://127.0.0.1:"
            f"{os.environ.get('TASK_PORT', '8799')}/admin/tickets/"
            f"{report['slug']}?view=board"
        )

    host = os.environ.get("TASK_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK_PORT", "8799"))
    print(f"Starting WorkLane board on http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

