"""ParkingAI relay/gateway service.

This package is meant to be deployed on a public VPS the town hall/operator
controls (with a real domain name and a real TLS certificate, e.g. via a
reverse proxy such as Caddy/nginx). It is intentionally separate from the
``parking_service`` package: it never touches camera credentials, the
SQLite database, or evidence files, and it is not required to run
ParkingAI locally/on a LAN. See ``REMOTE_ACCESS.md`` for the deployment
guide.
"""

from .relay_app import create_relay_app

__all__ = ["create_relay_app"]
