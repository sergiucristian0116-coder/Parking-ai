# Remote access (outbound tunnel) for ParkingAI

This document describes how the Android/PWA client can reach ParkingAI Desktop from the internet (4G/5G, remote Wi-Fi) **without opening any inbound port** on the town hall's router/firewall, and how to deploy the relay/gateway component that makes this possible.

## Why a tunnel instead of port forwarding or a VPN

- Opening `PARKINGAI_PORT` (default `8766`) to the internet would expose the desktop API directly to attackers scanning for open ports; this is explicitly out of scope.
- A commercial/proprietary VPN product would add an external dependency and cost, and was explicitly rejected.
- Instead, ParkingAI Desktop **dials out** to a relay/gateway service over an authenticated WebSocket (WSS) connection. Outbound connections are almost always allowed by default firewalls, so no router/firewall changes are needed at the town hall.

## Architecture

```
Android app / PWA  --HTTPS (public internet, real TLS cert)-->  Relay/Gateway (your VPS, your domain)
                                                                        ^
                                                                        | outbound WSS, initiated by Desktop
                                                                        |
                                                                  ParkingAI Desktop (town hall PC)
```

- The relay exposes a public HTTPS surface at `https://<your-domain>/instances/{instance_id}/api/v1/...`.
- When a request arrives at the relay, it is forwarded as a small JSON frame over the existing WebSocket tunnel to the connected desktop.
- The desktop executes the forwarded request **in-process** against its own FastAPI app (via `httpx.ASGITransport`), so **all existing authentication (JWT), roles (ADMIN/OPERATOR/VIEWER), rate limiting, and validation logic run exactly as they do for LAN requests.** The relay does not re-implement or bypass any of it.
- The relay never receives camera RTSP URLs/credentials, and never decrypts anything — it only relays bytes that would cross the wire as ordinary HTTPS traffic anyway.
- Multiple ParkingAI Desktop instances (e.g. one per town hall) can use the same relay; each is identified by its own `instance_id` and has its own dedicated tunnel connection. **This is per-instance routing, not a shared multi-tenant backend** — each installation still has its own separate local database, evidence store, and users.

## Identity and trust model

- Each desktop generates a random `instance_id` (e.g. `parkingai-3f9e...`) the first time remote access is configured. **This is never derived from an IP address**, so it stays valid across network changes, dynamic IPs, and reconnects.
- A `tunnel_token` (a long random secret, distinct from any user password) authenticates the desktop to the relay.
- The relay uses **trust-on-first-use (TOFU)**: the first token seen for a given `instance_id` is bound to it. Any later connection must present that same token.
- **Token rotation**: the desktop's "REGENERARE CREDENȚIALE" action creates a new token and reconnects, presenting both the new token and the previous one. The relay only accepts the rotation if the previous token matches what it has on file — this lets an operator rotate credentials from the desktop UI without any manual relay-side step, while still preventing a third party (who does not already hold the current token) from hijacking an `instance_id`.
- An optional `/admin/instances/{instance_id}/reset` endpoint, gated by `PARKINGAI_RELAY_ADMIN_TOKEN`, lets you manually clear a binding (for example, if a PC is lost/replaced and you need to let a new token bind as "first use" again).

## What is and is not verified

- The tunnel protocol, the relay's request/response forwarding, the TOFU binding, and the token rotation logic are covered by automated tests that run the relay and the desktop tunnel client against each other on loopback (`ai-engine/app/tests/test_remote_access.py`). These prove the *code* is correct.
- **A genuine test from a phone on 4G/5G against a real public domain with a real TLS certificate has not been performed as part of this change**, because that requires a real VPS with DNS and a certificate that only you can provision. Follow the deployment steps below, then validate with a real phone before relying on this in production. Do not assume it works end-to-end until you have done that test yourself.

## Deploying the relay

The relay is a separate, small FastAPI service (`ai-engine/app/relay_service/`) meant to run on your own public VPS — it has no dependency on cameras, the SQLite database, or evidence files.

1. **Provision a VPS** with a public IP and a DNS name pointing at it (e.g. `relay.yourtownhall.ro`).
2. **Install Python 3.11+** and the ParkingAI relay's dependencies (`fastapi`, `uvicorn[standard]`, from `ai-engine/requirements.txt`; the relay only needs these two, not `onvif-zeep`/`cryptography`, but installing the full file is simplest).
3. **Run the relay** on a local port, e.g. `8877`:
   ```bash
   cd ai-engine/app
   PARKINGAI_RELAY_DATA_DIR=/var/lib/parkingai-relay \
   PARKINGAI_RELAY_ADMIN_TOKEN="<a long random admin token, optional but recommended>" \
   python run_relay.py
   ```
4. **Terminate TLS with a reverse proxy** in front of the relay (the relay itself speaks plain HTTP/WS). Example with Caddy (automatic Let's Encrypt certificates):
   ```caddyfile
   relay.yourtownhall.ro {
       reverse_proxy 127.0.0.1:8877
   }
   ```
   Or with nginx, make sure to forward WebSocket upgrade headers for `/tunnel/*`:
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8877;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
       proxy_read_timeout 3600s;
   }
   ```
5. **Run the relay as a persistent service** (systemd unit, example):
   ```ini
   [Unit]
   Description=ParkingAI Relay
   After=network.target

   [Service]
   WorkingDirectory=/opt/parkingai/ai-engine/app
   ExecStart=/opt/parkingai/ai-engine/.venv/bin/python run_relay.py
   Environment=PARKINGAI_RELAY_DATA_DIR=/var/lib/parkingai-relay
   Environment=PARKINGAI_RELAY_ADMIN_TOKEN=<your admin token>
   Restart=on-failure
   User=parkingai

   [Install]
   WantedBy=multi-user.target
   ```
6. **Firewall**: only the reverse proxy's HTTPS port (443) needs to be open on the VPS. `8766` (the ParkingAI Desktop API) is never exposed publicly.

### Relay configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `PARKINGAI_RELAY_DATA_DIR` | `<repo>/relay_data` | Where `instances.json` (TOFU bindings) is stored. |
| `PARKINGAI_RELAY_ADMIN_TOKEN` | unset (admin reset disabled) | Required to call `/admin/instances/{id}/reset`. |
| `PARKINGAI_RELAY_RATE_LIMIT_PER_MINUTE` | `240` | Per-client-IP sliding-window rate limit on the public proxy endpoint. |
| `PARKINGAI_RELAY_MAX_CONCURRENT` | `16` | Max concurrent forwarded requests per connected instance. |
| `PARKINGAI_RELAY_MAX_BODY_BYTES` | `4194304` (4 MB) | Maximum accepted request body size (`413` if exceeded). |
| `PARKINGAI_RELAY_REQUEST_TIMEOUT_SECONDS` | `30` | How long the relay waits for the desktop to respond before returning `504`/`503`. |
| `PARKINGAI_RELAY_HOST` / `PARKINGAI_RELAY_PORT` | `0.0.0.0` / `8877` | Local bind address for `run_relay.py`. |

## Configuring ParkingAI Desktop

From the desktop app (ADMIN account), open **ACCES LA DISTANȚĂ**:

1. Enter the relay's public URL (e.g. `https://relay.yourtownhall.ro`).
2. Optionally set a label (e.g. "Primăria X — sediu central").
3. Click **TESTEAZĂ CONEXIUNEA** to perform a one-shot handshake without committing to a live connection.
4. Enable the toggle and click **SALVEAZĂ**. The status area shows `CONECTAT`/`DECONECTAT` and the last error, if any.
5. Use **REGENEREAZĂ CREDENȚIALE** periodically or if you suspect the token has leaked; this immediately invalidates the previous token.

These same actions are available over the API for automation/scripting; see the "Remote access" section of [API.md](API.md).

## Remote camera live view

The Android/PWA client reuses the existing `GET /api/v1/cameras/{cameraId}/live?mode=snapshot` endpoint (already used for LAN live view) through the tunnel: it is simple, works over the same chunked request/response framing used for every other forwarded call, and does not require a second, separate protocol (such as WebRTC/RTSP restreaming) to be designed, secured, and tested. Only the currently selected camera should be polled at a time from a mobile client to limit bandwidth/battery usage, consistent with the existing multi-camera (up to 20) design.

## Offline/reconnect behavior

- If the desktop loses connectivity to the relay (or the relay restarts), the tunnel client reconnects automatically with exponential backoff (2s, 4s, 8s, ... capped at 60s) — it does not hammer the relay.
- If one camera or the whole desktop goes offline, other ParkingAI Desktop instances connected to the same relay are unaffected; each has its own independent tunnel connection and `instance_id`.
- While reconnecting, `GET /api/v1/remote-access/status` reports `connected: false` along with the last error, so the desktop UI can show an honest status rather than a false "online" indicator.
