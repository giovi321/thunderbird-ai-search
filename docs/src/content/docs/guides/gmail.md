---
title: Gmail Setup
description: Authentication, folder strategy, and rate limit tuning for Gmail accounts.
---

Gmail requires a few extra steps compared to standard IMAP servers.

---

## Authentication: App Password required

Gmail does not accept your regular Google password over IMAP. You need an **App Password**:

1. Enable [2-Step Verification](https://myaccount.google.com/security) on your Google account
2. Go to **Google Account → Security → 2-Step Verification → App passwords**
3. Create a password for "Mail" / "Other"
4. Use that 16-character password in `config.yaml`

```yaml
accounts:
  - name: Gmail
    host: imap.gmail.com
    port: 993
    username: you@gmail.com
    password: abcd efgh ijkl mnop   # App Password, spaces optional
    use_ssl: true
    folders: []
```

---

## Folder strategy: Gmail-aware mode

When `folders: []`, the indexer detects Gmail (by host or by the presence of `[Gmail]/` folders) and indexes only:

- `[Gmail]/All Mail`: covers every regular email once
- `[Gmail]/Drafts`, `[Gmail]/Trash`, `[Gmail]/Spam`: sit outside All Mail

INBOX, Sent Mail, and every user label are skipped. In Gmail's IMAP they're views into All Mail, so fetching them duplicates work. On heavily-labelled accounts (200+ folders is common) this used to cause 3 to 5x duplicate fetches.

### Enable All Mail in IMAP

Go to **Gmail Settings → Forwarding and POP/IMAP → Folder Size Limits**, and **Settings → Labels → All Mail → Show in IMAP**.

If All Mail isn't visible, the indexer falls back to `INBOX + Drafts/Trash/Spam` (archived emails won't be indexed) and logs a warning.

---

## Rate limits and tuning for first sync

Gmail enforces per-second, per-session, and per-day limits. The recommended starting point for a large account:

```yaml
indexer:
  imap_fetch_batch: 25
  imap_folder_delay: 2.0
  imap_batch_delay: 0.5
  imap_max_fetches_per_cycle: 1500
```

With `imap_max_fetches_per_cycle: 1500`, each cycle ends with outcome `cap_reached` instead of `aborted_rate_limit`. Gmail tolerates capped cycles much better than hard session exhaustion. The indexer resumes from per-folder UID checkpoints on the next 15-minute tick.

First sync of a 75k-message account typically takes 1 to 3 days of cycles before it catches up. Watch progress in the addon settings page or with `docker compose logs -f server`.

---

## Search results after re-indexing

Search results show `folder: [Gmail]/All Mail` for newly indexed emails. Older entries from before the switch keep whatever folder name they had in Qdrant. Clicking through opens the email in Thunderbird via Message-ID lookup, so the displayed folder name doesn't matter for navigation.
