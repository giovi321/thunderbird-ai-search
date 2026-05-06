---
title: API Reference
description: REST endpoints, Docker Compose setup, and reverse proxy configuration.
---

## REST endpoints

All endpoints require the `X-API-Key` header if `api.api_key` is set in `config.yaml`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search` | Semantic search. Body: `{"query": "...", "limit": 10, "account": "work"}` |
| `GET` | `/health` | Qdrant + Ollama connectivity status |
| `GET` | `/stats` | Total indexed emails, last index time, account list |
| `GET` | `/accounts` | Per-account details: email count, folders, status |
| `POST` | `/reindex` | Trigger full re-index for all accounts (returns 202) |
| `POST` | `/reindex/{name}` | Reindex a specific account (returns 202) |
| `GET` | `/indexer/status` | Live indexer progress: phase, current account/folder, completion % |

---

## Docker Compose reference

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
    volumes:
      - qdrant_data:/qdrant/storage

  server:
    build: ./server
    ports: ["8342:8342"]
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - server_state:/root/.config/thunderbird-ai-search
    environment:
      - TAIS_QDRANT_URL=http://qdrant:6333
      - TAIS_OLLAMA_BASE_URL=http://host.docker.internal:11434
      - TAIS_API_HOST=0.0.0.0
      - TZ=Europe/Rome
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  qdrant_data:
  server_state:
```

Set `TZ` to your timezone for correct log timestamps and IMAP date queries.

---

## Reverse proxy (Apache)

To expose the server through a domain with TLS:

```apache
<VirtualHost *:443>
    ServerName search.yourdomain.com

    SSLEngine on
    SSLCertificateFile /path/to/cert.pem
    SSLCertificateKeyFile /path/to/key.pem

    ProxyPreserveHost On
    ProxyPass / http://localhost:8342/
    ProxyPassReverse / http://localhost:8342/
</VirtualHost>
```

Enable the required modules:

```bash
sudo a2enmod proxy proxy_http ssl
sudo a2ensite your-site.conf
sudo systemctl restart apache2
```

Then set the server URL in the addon settings to `https://search.yourdomain.com`.
