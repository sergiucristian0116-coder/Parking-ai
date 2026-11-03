from __future__ import annotations

import uvicorn

from parking_service.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "parking_service.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
