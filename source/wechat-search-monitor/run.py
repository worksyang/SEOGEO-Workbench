from __future__ import annotations

import os
import socket
import sys

from app import create_app
from app.config import Config


def _port_is_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    port = int(Config.PORT)
    if _port_is_in_use(port):
        print(
            f"[BOOT] 127.0.0.1:{port} is already in use; "
            "skip Flask startup before creating scheduler threads.",
            file=sys.stderr,
            flush=True,
        )
        return 98

    flask_app = create_app()
    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=_env_bool("FLASK_DEBUG", default=False),
        use_reloader=False,
        threaded=True,
    )
    return 0


app = create_app() if __name__ != "__main__" else None


if __name__ == "__main__":
    raise SystemExit(main())
