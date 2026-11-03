# ParkingAI REST API v1

The API is served by the Python service, independently of the preview server. The default bind is `127.0.0.1:8766`; configure `PARKINGAI_HOST`, `PARKINGAI_PORT`, and `PARKINGAI_CORS_ORIGINS` before startup. Use HTTPS termination at a trusted reverse proxy before allowing access outside a trusted local network. The service itself does not terminate TLS.

All routes below use the `/api/v1` prefix except `/health`. JSON timestamps are UTC ISO 8601 strings. Protected routes require `Authorization: Bearer <access_token>`. Access tokens expire after 15 minutes. Refresh tokens are opaque, expire after 14 days, and rotate on every refresh.

## Authentication

### `POST /api/v1/auth/login`

Request:

```json
{ "username": "admin", "password": "<configured password>" }
```

Response (`200`):

```json
{
  "access_token": "<short-lived token>",
  "token_type": "bearer",
  "expires_in": 900,
  "refresh_token": "<rotating refresh token>",
  "user": { "id": "<user-id>", "username": "admin", "role": "ADMIN" }
}
```

Five failed attempts from one client address within a minute are rate-limited (`429`).

### `POST /api/v1/auth/refresh`

Request: `{ "refresh_token": "<current refresh token>" }`. Returns a new access token and a new refresh token (`200`). Reuse of the previous refresh token returns `401`.

### `POST /api/v1/auth/logout`

Requires an access token. Request: `{ "refresh_token": "<current refresh token>" }`. Revokes that refresh token and returns `204`.

### `GET /api/v1/me`

Returns the authenticated user id, username, role, and active status.

### `GET /api/v1/users`, `POST /api/v1/users`

ADMIN only. Create request:

```json
{ "username": "operator-01", "password": "<at least 12 characters>", "role": "OPERATOR" }
```

Roles are `ADMIN`, `OPERATOR`, and `VIEWER`. Passwords are stored as scrypt hashes.

## Cameras

### `GET /api/v1/cameras`

Returns configured camera IDs, names, source types, enabled state, parking zones, and parking time limits. This response intentionally omits stream URLs, host addresses, usernames, and passwords.

### `POST /api/v1/cameras`

ADMIN only. Example RTSP request:

```json
{
  "id": "camera-01",
  "name": "Intrare",
  "source_type": "rtsp",
  "url": "rtsp://camera-host:554/stream-path",
  "username": "<camera user>",
  "password": "<camera password>",
  "enabled": true,
  "zones": [[[100, 100], [600, 100], [600, 400], [100, 400]]],
  "parking_limit_seconds": 900
}
```

Supported source types: `rtsp`, `hls`, `url`, `onvif`. HLS accepts HTTP(S); RTSP accepts RTSP(S); generic URL accepts either. ONVIF uses `host`, `port`, optional credentials, and an optional profile token/name instead of `url`. URL user-info is rejected; camera credentials are encrypted with `PARKINGAI_ENCRYPTION_KEY` and never returned by the API. The default maximum is 20 cameras (`PARKINGAI_MAX_CAMERAS`).

### `GET /api/v1/cameras/{cameraId}`

Returns the same non-secret camera representation as the camera list.

### `GET /api/v1/cameras/{cameraId}/configuration`

ADMIN only. Returns the configured stream URL or ONVIF host/profile for desktop camera administration; never returns a password or username.

### `PATCH /api/v1/cameras/{cameraId}`

ADMIN only. Accepts changed camera fields, including `enabled`, `zones`, and `parking_limit_seconds`. A blank/omitted credential does not erase the existing encrypted credential.

### `DELETE /api/v1/cameras/{cameraId}`

ADMIN only. Cameras with event/session history are disabled rather than physically deleted so that historical references remain valid.

### `POST /api/v1/cameras/test`

ADMIN only. Accepts the same connection fields as camera creation and tests a stream without saving it. Results include `OK`, `URL_INVALID`, `FORMAT_INCOMPATIBLE`, or `UNAVAILABLE`. OpenCV does not reliably distinguish a wrong RTSP password from an offline host across all camera/FFmpeg builds, so an unsuccessful open is reported as `UNAVAILABLE`, not falsely labeled as an authentication failure.

### `POST /api/v1/cameras/onvif/profiles`

ADMIN only. Request: `{ "host": "...", "port": 80, "username": "...", "password": "..." }`. Queries the ONVIF media service and returns profile tokens/names. This is a directed profile query, not network-wide WS-Discovery.

### `GET /api/v1/cameras/{cameraId}/status`

Returns runtime status, connectivity, processing FPS, last frame/reconnect times, processed frame count, and a sanitized error message.

### `GET /api/v1/cameras/{cameraId}/live?mode=snapshot|stream`

Requires authentication. Snapshot mode returns the latest JPEG. Stream mode returns multipart MJPEG. Camera credentials and RTSP addresses are not sent to the client.

## Vehicle data and incidents

### `GET /api/v1/vehicles?camera_id={id}&limit=100`

Returns persisted parking sessions, newest updates first. `limit` is between 1 and 500.

### `GET /api/v1/vehicles/{vehicleId}`

Returns the latest persisted session for a camera-scoped vehicle identifier such as `camera-01:15`.

### `GET /api/v1/detections?camera_id={id}&limit=100`

Returns persisted vehicle observations and their normalized payloads.

### `GET /api/v1/history?camera_id={id}&limit=100`

Returns persisted parking-session history.

### `GET /api/v1/incidents?status=PENDING&camera_id={id}&limit=100`

Returns persisted incident records. Supported statuses: `PENDING`, `CONFIRMED`, `REJECTED`.

### `GET /api/v1/incidents/{incidentId}`

Returns one incident or `404`.

### `POST /api/v1/incidents/{incidentId}/decision`

ADMIN and OPERATOR only. A pending incident can be decided once:

```json
{ "decision": "confirmed", "reason_code": "VALID", "comment": "Reviewed by agent" }
```

`decision` is `confirmed` or `rejected`. The response contains the persisted incident with status `CONFIRMED` or `REJECTED`. A competing second decision returns `409`. The acting user, reason, comment, and optional `Idempotency-Key` are written to the audit log.

### `GET /api/v1/evidence/{evidenceId}`

Returns the JPEG evidence for an incident when available. Access is authenticated; paths are constrained to the configured evidence directory.

## System and updates

### `GET /api/v1/dashboard?camera_id={id}`

Returns current AI state, camera health, and recent persisted sessions and detections. Intended for the desktop monitor.

### `GET /api/v1/system/status`

Returns API/AI state, bind host/port, HTTPS termination status, configured camera limit, and camera health summary.

### `POST /api/v1/system/start`, `POST /api/v1/system/stop`

ADMIN and OPERATOR only. Starts or stops camera processing while leaving the API process available.

### `GET /api/v1/events`

Authenticated Server-Sent Events stream. Event types currently include `camera_status`, `detections`, `vehicle_observation`, `incident`, `incident_decision`, and `system_status`. Clients reconnect on network loss and should treat the REST resources as the source of truth.

## Remote access (relay/gateway tunnel)

ADMIN only. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md) for the full architecture, trust model, and relay deployment guide. These endpoints configure and observe the desktop's outbound tunnel to an operator-controlled relay; they never expose the tunnel token in full.

### `GET /api/v1/remote-access/status`

Returns `{ enabled, connected, instance_id, label, gateway_url, tunnel_token_preview, last_connected_at, last_error }`. `instance_id` is a random, persistent identifier (not derived from any IP address). `tunnel_token_preview` is redacted (first/last 4 characters only).

### `POST /api/v1/remote-access/config`

Request: `{ "gateway_url": "https://relay.example.org", "enabled": true, "label": "Town Hall - Main" }`. Any field may be omitted to leave it unchanged. Persists the configuration and starts/stops the tunnel accordingly. Returns the same shape as `GET /api/v1/remote-access/status`.

### `POST /api/v1/remote-access/regenerate-credentials`

Rotates the tunnel token and reconnects. The relay only accepts the rotation if the desktop proves it held the previous token (see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)), so this is safe to call without any relay-side administrative step.

### `POST /api/v1/remote-access/test`

Request: `{ "gateway_url": "https://relay.example.org" }`. Performs a one-shot handshake against the given gateway without affecting the live tunnel connection. Returns `{ "ok": true|false, "detail": "..." }`.

## Common errors

Errors use FastAPI's JSON `detail` response. Expected status codes include `400` for malformed input, `401` for missing/invalid authentication, `403` for insufficient role, `404` for missing resources, `409` for duplicate IDs/decisions/camera limit, `422` for schema validation, `429` for rate limiting, and `503` for unavailable AI/camera resources.

## Android integration notes

Android clients should use an `OPERATOR` or `VIEWER` account, store access tokens in platform-protected memory/storage, and never receive camera passwords or private RTSP URLs. A client can list cameras/incidents and fetch live/evidence through these authorized API routes. Use HTTPS for remote access. On a LAN, the Android app can call the desktop's API directly; from the internet (4G/5G, remote Wi-Fi), it should call the relay's public URL (`https://<relay-domain>/instances/{instance_id}/api/v1/...`) instead — see [REMOTE_ACCESS.md](REMOTE_ACCESS.md). Either path enforces the exact same JWT/role authentication, since the relay only forwards opaque HTTPS traffic into the desktop's own FastAPI app. The API currently uses offset-style bounded `limit` queries rather than cursor pagination; high-volume deployments should add stable cursor pagination before large-scale mobile rollout.
