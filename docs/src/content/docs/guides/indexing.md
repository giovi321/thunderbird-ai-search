---
title: How Indexing Works
description: The per-folder loop, cycle outcomes, rate limiting, and state persistence.
---

![Indexer pipeline](/thunderbird-ai-search/assets/indexer-pipeline.svg)

The indexer runs in two steps per cycle.

---

## Step 1 — Per-folder loop

For each folder, in order:

1. **STATUS pre-pass.** Queries `UIDVALIDITY`, `UIDNEXT`, and `MESSAGES` for every folder upfront. Total `MESSAGES` minus the account's current Qdrant count gives the cycle's estimate of unique remaining emails.

2. **Fetch.** `UID SEARCH (last_uid + 1):*` returns only UIDs not seen yet; `UID FETCH` pulls them in batches of `imap_fetch_batch`.

3. **Filter.** Anything whose Message-ID already lives in Qdrant gets skipped, so an email in multiple folders is stored once.

4. **Embed and store.** New emails are sent to Ollama in batches of `batch_size`, then upserted into Qdrant.

5. **Checkpoint.** The per-folder `last_uid` advances after every successful upsert batch and is written to `state.json` immediately. An abort mid-folder loses at most one batch.

These sub-steps run interleaved per folder, not as four sequential phases over the whole mailbox. The addon dashboard shows all four counters moving in parallel as the cycle progresses.

---

## Step 2 — Cleanup (optional)

Runs after step 1 if `cleanup_enabled: true` and at least `cleanup_interval_hours` have passed since the last cleanup. The indexer scans IMAP Message-IDs across all folders and removes any Qdrant entry whose Message-ID is no longer on the server.

Cleanup is expensive (it touches every header in every folder), so `cleanup_interval_hours` (default 24) prevents it from running on every 15-minute cycle.

---

## Cycle outcomes

Each cycle records an outcome per account in `state.json`, visible in the addon dashboard:

| Outcome | Meaning |
|---------|---------|
| `completed` | All folders processed cleanly |
| `aborted_rate_limit` | Server returned `IMAP4.abort` mid-fetch (Gmail throttling). Per-folder checkpoints preserved |
| `aborted_embed` | Ollama returned a non-recoverable error. Per-folder checkpoints preserved |
| `cap_reached` | `imap_max_fetches_per_cycle` was hit; cycle ended on purpose. Resume next cycle |
| `connection_failed` | IMAP login or initial connect failed |

---

## How rate limiting is handled

- **Throttled commands.** `imap_batch_delay` waits between FETCH batches; `imap_folder_delay` waits between folders.
- **Smaller batches under pressure.** Lowering `imap_fetch_batch` reduces command burstiness.
- **Per-folder UID checkpoints.** When `IMAP4.abort` fires, the cycle ends gracefully and every folder's `last_uid` is preserved. The next cycle resumes exactly where this one left off.
- **Soft fetch cap.** `imap_max_fetches_per_cycle` caps work per cycle, ending early with `cap_reached` instead of hitting Gmail's hard limit.
- **Bisect-retry on Ollama 400.** A single email Ollama can't tokenize is isolated by recursive batch bisection and skipped, instead of aborting the whole batch.

---

## State persistence

The indexer keeps a JSON state file in a Docker volume (`server_state`). Per-folder UID checkpoints, cycle history, and cleanup timestamps survive container restarts and rebuilds.

State file shape (`~/.config/thunderbird-ai-search/state.json`):

```json
{
  "account-name": {
    "folders": {
      "[Gmail]/All Mail":  {"uidvalidity": "12345", "last_uid": 8421, "uidnext": 8500},
      "[Gmail]/Drafts":    {"uidvalidity": "67890", "last_uid": 312,  "uidnext": 313}
    },
    "last_run_at": "2026-05-05T14:23:11+00:00",
    "last_run_duration_seconds": 240,
    "last_run_indexed": 1500,
    "last_run_outcome": "completed",
    "history": [ { "...": "last 10 cycle results" } ]
  }
}
```

Stale folder entries are pruned automatically at the start of each cycle.

To force a full re-index from scratch:

```bash
docker compose down
docker volume rm thunderbird-ai-search_server_state
docker compose up -d
```
