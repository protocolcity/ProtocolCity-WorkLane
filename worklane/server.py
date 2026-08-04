"""WorkLane board server entrypoint.

This keeps runtime ownership under ``worklane`` while reusing the
existing board app factory.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from worklane.products import (
    default_product_slug_with_source,
    discover_products,
    emit_empty_runtime_override_warning,
    is_empty_runtime_override,
    runtime_root,
    wl_data_dir,
)
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
            f"Demo data seeded for project '{report['slug']}'. "
            f"API: http://127.0.0.1:"
            f"{os.environ.get('TASK_PORT', '8799')}/api/admin/products"
        )

    host = os.environ.get("TASK_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK_PORT", "8799"))
    default_slug, default_source = default_product_slug_with_source()
    print(f"Starting WorkLane API server on http://{host}:{port} ...")
    print(f"Default product: {default_slug or '(none)'} (source: {default_source})")

    # Guard: refuse to serve a dead registry (wl-289).
    # discover_products() is disk-fresh each call, so this reflects the actual
    # state at bind time rather than the module-level app init.
    products = discover_products()
    if not products:
        data_dir = wl_data_dir()
        db_files = sorted(data_dir.glob("*.db")) if data_dir.is_dir() else []
        if db_files:
            # Stores exist but none resolved — bad products.json or wrong stems
            print(
                f"FATAL: {len(db_files)} store(s) exist in {data_dir} but the "
                f"product registry resolved empty. Aborting — serving an empty "
                f"registry would break all HTTP consumers. Verify products.json "
                f"and _IGNORED_DB_STEMS / scratch-glob configuration.",
                file=sys.stderr,
            )
            sys.exit(1)
        # No stores in data_dir: fresh install or wrong working directory.
        print(
            f"WARNING: product registry empty — no .db stores in {data_dir} "
            f"(runtime_dir={runtime_root()}). "
            f"If unexpected, verify WORKLANE_RUNTIME_DIR / "
            f"WORKLANE_RUNTIME_DIR and that "
            f"this server is run from the correct checkout.",
            file=sys.stderr,
        )
    elif is_empty_runtime_override():
        # wl-374: override pin + empty store still surfaces tradeos via the
        # default-always-present path — warn loudly so miswires are not silent.
        emit_empty_runtime_override_warning(stream=sys.stderr)

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

