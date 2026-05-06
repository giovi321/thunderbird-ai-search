---
title: Troubleshooting
description: Common problems and how to fix them.
---

## Server won't start / "Cannot connect to Qdrant"

- Make sure Docker containers are running: `docker compose ps`
- Check logs: `docker compose logs server`

## "Cannot connect to Ollama"

- Verify Ollama is running on the host: `curl http://localhost:11434/api/tags`
- Ollama must run on the Docker host, not inside a container

## Indexing is slow

First-time indexing downloads every email over IMAP — normal for large mailboxes. Gmail accounts may need several cycles due to rate limits; the indexer saves progress and picks up where it left off.

Monitor progress:
```bash
docker compose logs -f server
```

Reduce `indexer.max_body_chars` for faster embedding at the cost of search quality.

## Only a fraction of emails indexed

- Check for OVERQUOTA errors in logs (Gmail rate limiting)
- For Gmail, the first run may only index a portion before hitting rate limits. Subsequent runs continue automatically
- To force a full re-index, delete the state volume and restart:
  ```bash
  docker compose down
  docker volume rm thunderbird-ai-search_server_state
  docker compose up -d
  ```

## Indexer says "Could not reach server" in addon settings

- Make sure you're running the latest addon code (reload if using temporary install)
- Check that the Server URL is correct in the addon settings

## CORS errors in the addon

- If using a reverse proxy, make sure it forwards all headers including `X-API-Key`
- Check that the server URL in addon settings matches exactly (including `https://`)
- If the browser console reports `Access-Control-Allow-Origin' does not match '*, *'`, the proxy is sending the header twice — once from FastAPI and once from itself. See the [reverse proxy guide](/thunderbird-ai-search/guides/reverse-proxy/) for the correct configuration.
- If the browser console reports `(Reason: CORS request did not succeed). Status code: (null)`, the TLS handshake is failing — the certificate isn't trusted by Thunderbird. See the [custom certificate guide](/thunderbird-ai-search/guides/custom-certificate/).

## "Email not found in Thunderbird"

The email exists in the search index but not in Thunderbird's local cache. Make sure Thunderbird has synced the relevant folder.

## Gmail: authentication failed

Gmail requires an [App Password](https://support.google.com/accounts/answer/185833), not your regular Google password. Enable 2-factor authentication first, then generate an app password.

## Emails re-fetched on every restart

- Make sure the `server_state` volume is configured in `docker-compose.yml`
- Do not use `docker compose down -v` — the `-v` flag deletes volumes
