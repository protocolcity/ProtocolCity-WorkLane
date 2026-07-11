"""WorkLane board server entrypoint.

This keeps runtime ownership under ``worklane`` while reusing the
existing board app factory.
"""

from __future__ import annotations

import os

from worklane.task_server import create_app

app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("TASK_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK_PORT", "8799"))
    print(f"Starting WorkLane board on http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

