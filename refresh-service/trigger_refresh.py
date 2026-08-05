"""Entry point for the Render Cron Job. Runs once per schedule tick and
exits -- just pokes the always-on refresh-service's POST /refresh, which
itself decides (via within_scheduled_window in app.py) whether now is
actually inside the configured business-hours window before doing any
real work. That check lives in the long-running service rather than here
so it stays correct across NZ daylight saving without touching the cron
schedule itself.
"""
from __future__ import annotations
import os, sys
import truststore
truststore.inject_into_ssl()
import requests


def main():
    url = os.environ['REFRESH_SERVICE_URL'].rstrip('/') + '/refresh'
    secret = os.environ['REFRESH_SHARED_SECRET']
    r = requests.post(
        url,
        headers={'X-Refresh-Secret': secret, 'Content-Type': 'application/json'},
        json={},
        timeout=30,
    )
    print(r.status_code, r.text)
    sys.exit(0 if r.ok else 1)


if __name__ == '__main__':
    main()
