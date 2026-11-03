<<<<<<< HEAD
# ParkingAI

ParkingAI combines a Tauri/React desktop interface, a Python camera-processing/API service, and a separate PWA client. The Android application is not part of this workspace; the versioned API contract is documented in [API.md](./API.md).

## Local development

The Python service uses Python 3.12 and the packages in `ai-engine/requirements.txt`. Create/install the `ai-engine/.venv` environment before running the desktop application.

The API no longer requires manually exported secrets to start: if `PARKINGAI_JWT_SECRET`, `PARKINGAI_ENCRYPTION_KEY`, or `PARKINGAI_ADMIN_PASSWORD` are not set, ParkingAI generates them on first run and persists what must survive restarts (the JWT signing secret and the camera-credential encryption key) under `ai-engine/data`. This is what makes the installed desktop application start correctly without a configured shell environment. You can still set these variables explicitly for production/shared deployments; see [CAMERA_SETUP.md](./CAMERA_SETUP.md) for details and for locating the generated initial admin password.

Start the desktop application from the project root with `npm run tauri dev`; the Tauri host starts the local API when the UI initializes. The API binds to `127.0.0.1:8766` by default.

To run the API independently of the desktop UI, start it from PowerShell:

```powershell
.\ai-engine\.venv\Scripts\python.exe .\ai-engine\app\run_api.py
```

This is the recommended mode when the API must remain available to Android or PWA clients after the desktop UI closes. See [CAMERA_SETUP.md](./CAMERA_SETUP.md) for network access and secret handling.

For the PWA, set `PARKINGAI_API_URL` to the Python API origin and `PARKINGAI_PWA_HOST` / `PARKINGAI_PWA_PORT` as needed, then run `npm run dev` from `mobile-pwa`. The PWA now proxies authenticated requests to the real API; it contains no seeded vehicle or login data.

## Runtime and network notes

Camera definitions, user records, sessions, incidents, and audit records are stored in SQLite under `ai-engine/data` by default. Evidence JPEGs are stored in its `evidence` subdirectory. Camera passwords are encrypted with the Fernet key in `PARKINGAI_ENCRYPTION_KEY` or, when not set, the one auto-generated into `ai-engine/data/local_secrets.json`; preserve that key/file securely or saved camera credentials cannot be decrypted.

For authorized LAN access set `PARKINGAI_HOST=0.0.0.0` and restrict the Windows firewall. Use a TLS-terminating reverse proxy for remote LAN/WAN access. Do not expose plain HTTP to the internet, and do not port-forward `PARKINGAI_PORT` directly to the internet. For real internet access (Android on 4G/5G, remote Wi-Fi) without opening any inbound port, use the outbound relay/gateway tunnel described in [REMOTE_ACCESS.md](./REMOTE_ACCESS.md) instead. The Android client should connect only to the API (directly on a LAN, or through the relay from the internet) and must never receive private RTSP URLs or camera passwords.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for service boundaries and current limitations, [CAMERA_SETUP.md](./CAMERA_SETUP.md) for camera registration, and [REMOTE_ACCESS.md](./REMOTE_ACCESS.md) for the remote-access tunnel and relay deployment guide.
=======
# Parking-ai
AI-powered parking monitoring system with desktop software, vehicle detection, license plate recognition, and Android application integration.
>>>>>>> 73f2277d04c77cbe5eeadb0540f98f6c85252e56
