from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PARKINGAI_RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("PARKINGAI_RELAY_PORT", "8877"))
    uvicorn.run(
        "relay_service.relay_app:create_relay_app",
        factory=True,
        host=host,
        port=port,
        access_log=False,
        ws_max_size=8 * 1024 * 1024,
    )


if __name__ == "__main__":
    main()
