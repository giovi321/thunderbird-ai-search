---
title: Configuration
description: Full reference for config.yaml settings and environment variables.
---

All settings live in `config.yaml`. See `config.example.yaml` in the repo for a fully commented template.

---

## IMAP accounts

```yaml
accounts:
  - name: Work
    host: imap.gmail.com
    port: 993
    username: you@gmail.com
    password: your-app-password
    use_ssl: true
    folders: []   # empty = all folders
```

| Setting | Default | Description |
|---------|---------|-------------|
| `name` | — | Label for the account (shown in search results and addon) |
| `host` | — | IMAP server hostname |
| `port` | `993` | IMAP port |
| `username` | — | Email address / login |
| `password` | — | Password or app password |
| `use_ssl` | `true` | Use SSL/TLS |
| `folders` | `[]` (all) | Folders to index. Empty `[]` = all folders |

You can configure multiple accounts. All emails go into one shared vector store. Use the account dropdown in the addon to filter search results by account.

---

## Indexer

| Setting | Default | Description |
|---------|---------|-------------|
| `batch_size` | `50` | Emails per embedding batch sent to Ollama |
| `max_body_chars` | `4000` | Truncate email body before embedding |
| `schedule_minutes` | `15` | Re-index interval in minutes. `0` = index once only |
| `cleanup_enabled` | `true` | Remove Qdrant entries for emails deleted from IMAP |
| `cleanup_interval_hours` | `24` | Minimum hours between cleanup passes per account |
| `imap_fetch_batch` | `25` | Emails per IMAP FETCH command. Lower = gentler on rate-limited servers |
| `imap_folder_delay` | `2.0` | Seconds to wait between folders |
| `imap_batch_delay` | `0.5` | Seconds to wait between fetch batches |
| `imap_max_fetches_per_cycle` | `0` | Cap fetches per cycle; `0` = unlimited. Set ~`1000–2000` for Gmail |

---

## Ollama

| Setting | Default | Description |
|---------|---------|-------------|
| `base_url` | `http://localhost:11434` | Ollama URL (overridden in Docker to `http://host.docker.internal:11434`) |
| `model` | `nomic-embed-text` | Embedding model (768 dimensions) |

---

## Qdrant

| Setting | Default | Description |
|---------|---------|-------------|
| `url` | `http://localhost:6333` | Qdrant URL (overridden in Docker to `http://qdrant:6333`) |
| `collection` | `emails` | Qdrant collection name |

---

## API

| Setting | Default | Description |
|---------|---------|-------------|
| `host` | `0.0.0.0` | Bind address |
| `port` | `8342` | Server port |
| `api_key` | `""` | Shared secret. If set, all requests require `X-API-Key` header |

Generate an API key with:

```bash
openssl rand -hex 32
```

Set the same key in `config.yaml` (`api.api_key`) and in the addon settings.

---

## Environment variables

Every setting can be overridden via environment variables in `docker-compose.yml`. The following are set automatically for Docker networking:

| Variable | Description |
|----------|-------------|
| `TAIS_QDRANT_URL` | Qdrant URL inside Docker (`http://qdrant:6333`) |
| `TAIS_OLLAMA_BASE_URL` | Ollama URL via Docker host (`http://host.docker.internal:11434`) |
| `TAIS_API_HOST` | Bind address (`0.0.0.0`) |

Account passwords and the API key can be set via environment variables instead of hardcoding them in `config.yaml`:

```yaml
environment:
  - TAIS_ACCOUNTS_0_PASSWORD=your-password-here
  - TAIS_ACCOUNTS_1_PASSWORD=second-account-password
  - TAIS_API_API_KEY=your-api-key
```

Other available variables:

| Variable | Maps to |
|----------|---------|
| `TAIS_QDRANT_COLLECTION` | `qdrant.collection` |
| `TAIS_OLLAMA_MODEL` | `ollama.model` |
| `TAIS_API_PORT` | `api.port` |
| `TAIS_INDEXER_BATCH_SIZE` | `indexer.batch_size` |
| `TAIS_INDEXER_MAX_BODY_CHARS` | `indexer.max_body_chars` |
| `TAIS_INDEXER_SCHEDULE_MINUTES` | `indexer.schedule_minutes` |
| `TAIS_INDEXER_CLEANUP_ENABLED` | `indexer.cleanup_enabled` |
| `TAIS_INDEXER_IMAP_FETCH_BATCH` | `indexer.imap_fetch_batch` |
| `TAIS_INDEXER_IMAP_FOLDER_DELAY` | `indexer.imap_folder_delay` |
| `TAIS_INDEXER_IMAP_BATCH_DELAY` | `indexer.imap_batch_delay` |
| `TAIS_INDEXER_CLEANUP_INTERVAL_HOURS` | `indexer.cleanup_interval_hours` |
| `TAIS_INDEXER_IMAP_MAX_FETCHES_PER_CYCLE` | `indexer.imap_max_fetches_per_cycle` |
| `TAIS_LOG_LEVEL` | `log_level` |
