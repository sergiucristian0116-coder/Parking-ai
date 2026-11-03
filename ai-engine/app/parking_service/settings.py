from __future__ import annotations

import json
import os
import secrets as secrets_module
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


ENGINE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = ENGINE_DIR.parent

LOCAL_SECRETS_FILENAME = "local_secrets.json"


def _restrict_file_permissions(path: Path) -> None:
    """Best-effort restriction of a secrets file to the current OS user.

    This is defense in depth, not the primary access control: the containing
    data directory is already expected to be private to the machine/user
    running ParkingAI (the SQLite database with encrypted camera credentials
    lives next to it under the same assumption).
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        if not username:
            return
        try:
            subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{username}:F",
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except OSError:
            pass


def _load_or_create_local_secrets(data_dir: Path) -> dict[str, str]:
    """Load persisted, machine-local JWT/encryption secrets, generating them once.

    ParkingAI requires a JWT signing secret and a Fernet key to encrypt stored
    camera credentials. Advanced/production deployments can still supply
    ``PARKINGAI_JWT_SECRET`` and ``PARKINGAI_ENCRYPTION_KEY`` explicitly (for
    example to share them across multiple hosts). When they are not supplied
    -- which is always the case for the installed desktop application, since
    it is launched without a configured shell environment -- ParkingAI
    generates strong random secrets on first run and persists them next to
    the database so the service keeps working (and existing encrypted data
    stays decryptable) across restarts, without ever hardcoding a secret.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_path = data_dir / LOCAL_SECRETS_FILENAME

    values: dict[str, str] = {}
    if secrets_path.exists():
        try:
            values = json.loads(secrets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}

    changed = False
    jwt_secret = values.get("jwt_secret", "")
    if len(jwt_secret) < 32:
        jwt_secret = secrets_module.token_urlsafe(48)
        values["jwt_secret"] = jwt_secret
        changed = True

    encryption_key = values.get("encryption_key", "")
    try:
        Fernet(encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        encryption_key = Fernet.generate_key().decode("ascii")
        values["encryption_key"] = encryption_key
        changed = True

    if changed:
        secrets_path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        _restrict_file_permissions(secrets_path)

    return values


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database_path: Path
    evidence_dir: Path
    access_token_secret: str
    camera_encryption_key: bytes
    bootstrap_admin_password: str | None
    cors_origins: tuple[str, ...]
    max_cameras: int
    parking_limit_seconds: int
    api_base_path: str = "/api/v1"
    data_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("PARKINGAI_DATA_DIR", ENGINE_DIR / "data")).resolve()

        access_secret = os.environ.get("PARKINGAI_JWT_SECRET", "")
        encryption_key = os.environ.get("PARKINGAI_ENCRYPTION_KEY", "")
        if not access_secret or not encryption_key:
            local_secrets = _load_or_create_local_secrets(data_dir)
            access_secret = access_secret or local_secrets["jwt_secret"]
            encryption_key = encryption_key or local_secrets["encryption_key"]

        if len(access_secret) < 32:
            raise RuntimeError("PARKINGAI_JWT_SECRET must contain at least 32 characters.")

        try:
            Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError(
                "PARKINGAI_ENCRYPTION_KEY must be a valid Fernet key."
            ) from exc

        origins = tuple(
            origin.strip()
            for origin in os.environ.get(
                "PARKINGAI_CORS_ORIGINS",
                "http://localhost:1420,tauri://localhost,http://tauri.localhost",
            ).split(",")
            if origin.strip()
        )
        max_cameras = int(os.environ.get("PARKINGAI_MAX_CAMERAS", "20"))
        if max_cameras < 1:
            raise RuntimeError("PARKINGAI_MAX_CAMERAS must be a positive integer.")

        return cls(
            host=os.environ.get("PARKINGAI_HOST", "127.0.0.1"),
            port=int(os.environ.get("PARKINGAI_PORT", "8766")),
            database_path=Path(os.environ.get("PARKINGAI_DATABASE", data_dir / "parkingai.sqlite3")),
            evidence_dir=Path(os.environ.get("PARKINGAI_EVIDENCE_DIR", data_dir / "evidence")),
            access_token_secret=access_secret,
            camera_encryption_key=encryption_key.encode("ascii"),
            bootstrap_admin_password=os.environ.get("PARKINGAI_ADMIN_PASSWORD"),
            cors_origins=origins,
            max_cameras=max_cameras,
            parking_limit_seconds=int(os.environ.get("PARKINGAI_PARKING_LIMIT_SECONDS", "900")),
            data_dir=data_dir,
        )
