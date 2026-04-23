"""Single entrypoint for running EvoAgent server."""

from __future__ import annotations

import uvicorn

from app.api.server import app as server_app


def main() -> None:
    uvicorn.run(server_app, host="0.0.0.0", port=8194, reload=False)


if __name__ == "__main__":
    main()
