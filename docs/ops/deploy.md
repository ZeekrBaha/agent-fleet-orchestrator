# Deploying Fleet in Production

## Quick start

```bash
uv run uvicorn fleet.main:app --host 0.0.0.0 --port 8000
```

For production, bind to `127.0.0.1` and front with nginx (see Security below).

## Required environment variables

| Variable | Description | Example |
|---|---|---|
| `FLEET_API_TOKEN` | Bearer token for all write endpoints | `s3cr3t-token-value` |
| `FLEET_DB_PATH` | Path to the SQLite database file | `/var/lib/fleet/fleet.db` |
| `FLEET_HOST` | Bind address | `127.0.0.1` |
| `FLEET_PORT` | Bind port | `8000` |

Fleet validates configuration at startup and will refuse to start if
`FLEET_API_TOKEN` is empty and `FLEET_HOST` is not a loopback address.

## systemd unit

Save as `/etc/systemd/system/fleet.service`:

```ini
[Unit]
Description=Fleet orchestrator
After=network.target

[Service]
Type=simple
User=fleet
WorkingDirectory=/opt/fleet
ExecStart=/opt/fleet/.venv/bin/uvicorn fleet.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --log-level info
Restart=on-failure
RestartSec=5s

Environment=FLEET_API_TOKEN=<long-random-value>
Environment=FLEET_DB_PATH=/var/lib/fleet/fleet.db
Environment=FLEET_HOST=127.0.0.1
Environment=FLEET_PORT=8000

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fleet
sudo systemctl start fleet
```

## nginx reverse proxy (recommended)

Bind Fleet to `127.0.0.1` and expose it via nginx with TLS:

```nginx
server {
    listen 443 ssl;
    server_name fleet.example.com;

    ssl_certificate     /etc/ssl/certs/fleet.crt;
    ssl_certificate_key /etc/ssl/private/fleet.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Required for Server-Sent Events (event stream).
        proxy_buffering off;
        proxy_cache off;
    }
}
```

## Security notes

- **Set `FLEET_API_TOKEN` to a long random value** — at least 32 characters.
  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Bind to `127.0.0.1` behind nginx** in production. Binding to `0.0.0.0`
  without a token is explicitly rejected by Fleet at startup.
- Store the token in a secrets manager or systemd `EnvironmentFile`; do not
  commit it to version control.
- Run Fleet as a dedicated non-root user (`fleet` in the example above).
- The SQLite DB file should be readable and writable only by the `fleet` user.

## Health check

```bash
python -m fleet.cli doctor --db /var/lib/fleet/fleet.db
```

All checks should return `[OK]` on a healthy instance.
