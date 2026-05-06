"""IMAP email indexer for Thunderbird AI Search."""

import asyncio
import email
import email.header
import email.utils
import imaplib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Optional

from qdrant_client import models as qmodels

from server.config import AppConfig, ImapAccount
from server.embeddings import EmbeddingProvider
from server.vector_store import VectorStore, make_point_id

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".config" / "thunderbird-ai-search"
STATE_FILE = STATE_DIR / "state.json"


class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keeping text content."""

    def __init__(self):
        super().__init__()
        self._text = StringIO()

    def handle_data(self, data):
        self._text.write(data)

    def get_text(self) -> str:
        return self._text.getvalue()


def strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def decode_header_value(raw: Optional[str]) -> str:
    """Decode RFC 2047 encoded header values."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def parse_date(raw: Optional[str]) -> str:
    """Parse email Date header to ISO 8601 string."""
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return raw


def extract_body(msg: email.message.Message, max_chars: int) -> str:
    """Extract plain text body from email message."""
    text_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_parts.append(payload.decode(charset, errors="replace"))
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_parts.append(payload.decode(charset, errors="replace"))
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/html":
                html_parts.append(text)
            else:
                text_parts.append(text)

    body = "\n".join(text_parts) if text_parts else strip_html("\n".join(html_parts))
    return body[:max_chars]


@dataclass
class IndexerStatus:
    """Thread-safe status for the indexer, readable by the API."""

    running: bool = False
    phase: str = ""  # "fetching", "filtering", "embedding", "cleanup"
    current_account: str = ""
    current_folder: str = ""
    folders_done: int = 0
    folders_total: int = 0
    emails_fetched: int = 0
    folder_emails_done: int = 0
    folder_emails_total: int = 0
    emails_processed: int = 0
    emails_total: int = 0
    cleanup_folders_done: int = 0
    cleanup_folders_total: int = 0
    # cycle_total_estimate: unique emails remaining for this cycle = sum(MESSAGES) - already_indexed
    cycle_total_estimate: int = 0
    # Sub-step counters that accumulate over a cycle so the UI can show fetch/filter/embed in parallel
    emails_filtered_dup: int = 0     # emails skipped because already in Qdrant (deduped by Message-ID)
    emails_skipped_ollama: int = 0   # emails Ollama 400'd on (bisect-isolated and tombstoned)
    # Diagnostic counters for understanding rate-limit aborts
    cmds_this_cycle: int = 0         # IMAP commands sent in the current cycle
    bytes_this_cycle: int = 0        # bytes downloaded from IMAP this cycle (for daily bandwidth tracking)
    last_abort_detail: dict = field(default_factory=dict)
    # Adaptive-backoff state owned by the scheduler — surfaced here so the dashboard
    # can show "next run in 2h (backoff 8x active after 3 rate-limited cycles)".
    backoff_multiplier: float = 1.0
    consecutive_rate_limit_aborts: int = 0
    next_run_at_override: str = ""
    last_error: str = ""
    last_run: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_fetching(self, account: str, folder: str, folders_done: int, folders_total: int):
        with self._lock:
            self.running = True
            self.phase = "fetching"
            self.current_account = account
            self.current_folder = folder
            self.folders_done = folders_done
            self.folders_total = folders_total
            self.folder_emails_done = 0
            self.folder_emails_total = 0
            self.last_error = ""

    def set_folder_progress(self, done: int, total: int):
        with self._lock:
            self.folder_emails_done = done
            self.folder_emails_total = total

    def set_fetched_count(self, count: int):
        with self._lock:
            self.emails_fetched = count

    def set_cycle_total(self, total: int):
        with self._lock:
            self.cycle_total_estimate = total

    def begin_cycle(self):
        """Reset per-cycle counters at the start of an account's cycle."""
        with self._lock:
            self.emails_fetched = 0
            self.emails_filtered_dup = 0
            self.emails_processed = 0
            self.emails_skipped_ollama = 0
            self.cycle_total_estimate = 0
            self.cmds_this_cycle = 0
            self.bytes_this_cycle = 0
            self.last_abort_detail = {}

    def add_filtered(self, n: int):
        with self._lock:
            self.emails_filtered_dup += n

    def add_skipped_ollama(self, n: int):
        with self._lock:
            self.emails_skipped_ollama += n

    def add_commands(self, n: int = 1):
        with self._lock:
            self.cmds_this_cycle += n

    def add_bytes(self, n: int):
        with self._lock:
            self.bytes_this_cycle += n

    def set_abort_detail(self, detail: dict):
        with self._lock:
            self.last_abort_detail = dict(detail)

    def get_cmd_count(self) -> int:
        with self._lock:
            return self.cmds_this_cycle

    def get_bytes_count(self) -> int:
        with self._lock:
            return self.bytes_this_cycle

    def set_backoff_state(self, multiplier: float, consecutive: int, next_run_at: str = ""):
        with self._lock:
            self.backoff_multiplier = float(multiplier)
            self.consecutive_rate_limit_aborts = int(consecutive)
            self.next_run_at_override = next_run_at

    def set_filtering(self, account: str):
        with self._lock:
            self.running = True
            self.phase = "filtering"
            self.current_account = account
            self.current_folder = ""

    def set_embedding(self, account: str, total: int):
        with self._lock:
            self.running = True
            self.phase = "embedding"
            self.current_account = account
            self.current_folder = ""
            self.emails_processed = 0
            self.emails_total = total
            self.last_error = ""

    def set_progress(self, processed: int):
        with self._lock:
            self.emails_processed = processed

    def set_cleanup(self, account: str, folders_total: int = 0):
        with self._lock:
            self.running = True
            self.phase = "cleanup"
            self.current_account = account
            self.current_folder = ""
            self.cleanup_folders_done = 0
            self.cleanup_folders_total = folders_total

    def set_cleanup_progress(self, folder: str, done: int):
        with self._lock:
            self.current_folder = folder
            self.cleanup_folders_done = done

    def set_done(self):
        with self._lock:
            self.running = False
            self.phase = ""
            self.current_account = ""
            self.current_folder = ""
            self.cycle_total_estimate = 0
            self.last_error = ""
            self.last_run = datetime.now(timezone.utc).isoformat()

    def set_error(self, error: str):
        with self._lock:
            self.last_error = error

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "current_account": self.current_account,
                "current_folder": self.current_folder,
                "folders_done": self.folders_done,
                "folders_total": self.folders_total,
                "emails_fetched": self.emails_fetched,
                "folder_emails_done": self.folder_emails_done,
                "folder_emails_total": self.folder_emails_total,
                "emails_processed": self.emails_processed,
                "emails_total": self.emails_total,
                "cleanup_folders_done": self.cleanup_folders_done,
                "cleanup_folders_total": self.cleanup_folders_total,
                "cycle_total_estimate": self.cycle_total_estimate,
                "emails_filtered_dup": self.emails_filtered_dup,
                "emails_skipped_ollama": self.emails_skipped_ollama,
                "cmds_this_cycle": self.cmds_this_cycle,
                "bytes_this_cycle": self.bytes_this_cycle,
                "last_abort_detail": dict(self.last_abort_detail),
                "backoff_multiplier": self.backoff_multiplier,
                "consecutive_rate_limit_aborts": self.consecutive_rate_limit_aborts,
                "next_run_at_override": self.next_run_at_override,
                "last_error": self.last_error,
                "last_run": self.last_run,
            }


class EmailIndexer:
    """Indexes emails from IMAP accounts into Qdrant via Ollama embeddings."""

    def __init__(
        self,
        config: AppConfig,
        embedder: EmbeddingProvider,
        store: VectorStore,
    ):
        self.config = config
        self.embedder = embedder
        self.store = store
        self.status = IndexerStatus()
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except Exception:
                logger.warning("Could not read state file, starting fresh")
                return {}
            # Legacy schema detection: old state has only "last_run_date" per account.
            # New schema uses per-folder UID checkpoints under "folders".
            for acct_name, acct_state in state.items():
                if isinstance(acct_state, dict) and "folders" not in acct_state:
                    logger.info(
                        "Account '%s': legacy state schema detected, "
                        "will rebuild per-folder UID checkpoints on next cycle",
                        acct_name,
                    )
            return state
        return {}

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self._state, indent=2))

    def _connect(self, account: ImapAccount) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
        if account.use_ssl:
            conn = imaplib.IMAP4_SSL(account.host, account.port)
        else:
            conn = imaplib.IMAP4(account.host, account.port)
        conn.login(account.username, account.password)
        # Count LOGIN as one command toward the per-session count
        self.status.add_commands(1)
        return conn

    # Per-session caps on Gmail (≈7500 commands, ≈2.5 GB downloaded, idle drops, TLS
    # hiccups) surface as imaplib.IMAP4.abort. The session itself is dead, but the
    # account isn't actually rate-limited — a fresh LOGIN works immediately. So we
    # close the dead conn and reconnect with exponential backoff + jitter, mirroring
    # openArchiver's withRetry pattern. The 24-hour account-wide quota is a separate
    # concern handled by the scheduler-level adaptive backoff in main.py, which now
    # only kicks in when *all* per-batch retries here are exhausted.
    IMAP_RETRY_MAX_ATTEMPTS = 5

    def _reconnect_after_abort(
        self,
        conn: imaplib.IMAP4_SSL | imaplib.IMAP4 | None,
        account: ImapAccount,
        attempt: int,
    ) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
        """Tear down a dead IMAP session and open a fresh one with exp backoff + jitter."""
        try:
            if conn is not None:
                conn.logout()
        except Exception:
            pass
        delay = (2 ** attempt) + random.random()  # 2s, 4s, 8s, 16s, 32s + 0–1s jitter
        logger.info(
            "IMAP session for '%s' lost (attempt %d/%d) — reconnecting in %.1fs",
            account.name, attempt, self.IMAP_RETRY_MAX_ATTEMPTS, delay,
        )
        time.sleep(delay)
        return self._connect(account)

    # On Gmail, every regular email lives in [Gmail]/All Mail. INBOX, Sent Mail,
    # and user labels are all just views into All Mail — fetching from them duplicates.
    # Drafts, Trash, Spam are *separate* from All Mail, so we index them independently.
    # Result: each email is fetched exactly once, no label-overlap inflation.
    _GMAIL_INDEX_FOLDERS = {
        "[Gmail]/All Mail",
        "[Gmail]/Drafts",
        "[Gmail]/Trash",
        "[Gmail]/Spam",
    }

    def _get_folders(self, conn: imaplib.IMAP4_SSL, account: ImapAccount) -> list[str]:
        """Get folders to index. If account.folders is empty, list all (Gmail-aware)."""
        if account.folders:
            return list(account.folders)
        self.status.add_commands(1)
        status, data = conn.list()
        if status != "OK":
            return ["INBOX"]

        all_names: list[str] = []
        for item in data:
            if isinstance(item, bytes):
                match = re.search(rb'"([^"]*)"$|(\S+)$', item)
                if match:
                    name = (match.group(1) or match.group(2)).decode("utf-8", errors="replace")
                    all_names.append(name)

        # Detect Gmail by host or by presence of [Gmail]/ folders in the listing.
        # Google Workspace (custom domain) accounts also use imap.gmail.com.
        is_gmail = "gmail" in account.host.lower() or any(
            n.startswith("[Gmail]") for n in all_names
        )

        if is_gmail:
            keep = [n for n in all_names if n in self._GMAIL_INDEX_FOLDERS]
            if "[Gmail]/All Mail" not in keep:
                # Gmail has a per-account toggle that hides All Mail from IMAP. Without
                # it, we can't get full coverage with no duplicates — fall back to
                # INBOX + Drafts/Trash/Spam, which loses archived-only emails. Warn loudly.
                logger.warning(
                    "Gmail account '%s': [Gmail]/All Mail is not visible via IMAP. "
                    "To index archived emails, enable it in Gmail Settings → Forwarding "
                    "and POP/IMAP → Folder Size Limits → 'Show in IMAP'. Falling back "
                    "to INBOX + Drafts/Trash/Spam (archived emails will not be indexed).",
                    account.name,
                )
                fallback = ["INBOX"]
                for sys_folder in ("[Gmail]/Drafts", "[Gmail]/Trash", "[Gmail]/Spam"):
                    if sys_folder in all_names:
                        fallback.append(sys_folder)
                return fallback
            skipped_count = len(all_names) - len(keep)
            logger.info(
                "Gmail account '%s': indexing %d folders (%s); skipping %d "
                "INBOX/labels/virtuals that duplicate All Mail",
                account.name, len(keep), ", ".join(keep), skipped_count,
            )
            return keep

        # Non-Gmail IMAP: index every folder we found
        return all_names or ["INBOX"]

    def _get_folder_uid_state(
        self,
        conn: imaplib.IMAP4_SSL,
        folder: str,
    ) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """Get UIDVALIDITY, UIDNEXT, and MESSAGES count via STATUS.

        Returns (uidvalidity, uidnext, messages) or (None, None, None) on failure.
        MESSAGES is the current count of emails in the folder. UIDNEXT-1 is the
        max UID ever assigned (grows forever, even after deletions), so for a
        cycle-work estimate you want MESSAGES, not the UID range.
        """
        try:
            self.status.add_commands(1)
            status, data = conn.status(f'"{folder}"', "(UIDVALIDITY UIDNEXT MESSAGES)")
            if status != "OK" or not data:
                return None, None, None
            raw = data[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            uv_match = re.search(r"UIDVALIDITY\s+(\d+)", raw)
            un_match = re.search(r"UIDNEXT\s+(\d+)", raw)
            msg_match = re.search(r"MESSAGES\s+(\d+)", raw)
            uv = int(uv_match.group(1)) if uv_match else None
            un = int(un_match.group(1)) if un_match else None
            msgs = int(msg_match.group(1)) if msg_match else None
            return uv, un, msgs
        except Exception as e:
            logger.warning("STATUS failed for folder '%s': %s", folder, e)
            return None, None, None

    def _fetch_emails(
        self,
        conn: imaplib.IMAP4_SSL,
        folder: str,
        account: ImapAccount,
        last_uid: int = 0,
        prior_fetched_total: int = 0,
    ) -> tuple[list[dict], bool, imaplib.IMAP4_SSL | imaplib.IMAP4]:
        """Fetch emails from a folder with UID > last_uid.

        Returns (emails, fetch_aborted, conn). Emails are in ascending UID order
        and each carries a "uid" key. fetch_aborted is True only if every retry
        attempt (including fresh-LOGIN reconnects) failed — i.e. genuine 24h
        quota or persistent server-side block. A single IMAP4.abort transparently
        triggers a reconnect+retry, since Gmail just kills the *session*, not the
        account. The conn returned may be a fresh one if a reconnect occurred;
        the caller must rebind its local conn variable.

        prior_fetched_total is the cumulative emails fetched in this account run
        before this folder started — used so the running "Total fetched so far"
        in the status snapshot updates per batch, not just per folder.
        """
        max_attempts = self.IMAP_RETRY_MAX_ATTEMPTS

        # SELECT (with reconnect-on-abort)
        for attempt in range(1, max_attempts + 1):
            try:
                self.status.add_commands(1)
                status, _ = conn.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    logger.warning("Could not select folder '%s' on %s", folder, account.name)
                    return [], False, conn
                break
            except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                if attempt == max_attempts:
                    logger.error(
                        "  Folder '%s': IMAP abort during SELECT after %d attempts: %s",
                        folder, max_attempts, e,
                    )
                    self.status.set_abort_detail({
                        "folder": folder,
                        "phase": "select",
                        "raw_message": str(e),
                        "commands_in_session": self.status.get_cmd_count(),
                        "fetched_this_cycle": prior_fetched_total,
                        "attempts": max_attempts,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    })
                    return [], True, conn
                logger.warning(
                    "  Folder '%s': IMAP abort during SELECT (attempt %d/%d): %s",
                    folder, attempt, max_attempts, e,
                )
                conn = self._reconnect_after_abort(conn, account, attempt)
            except Exception as e:
                logger.warning("Error selecting folder '%s' on %s: %s", folder, account.name, e)
                return [], False, conn

        # SEARCH (with reconnect-on-abort + re-SELECT after each reconnect)
        uid_data = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.status.add_commands(1)
                search_criteria = f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
                status, uid_data = conn.uid("SEARCH", None, search_criteria)
                if status != "OK":
                    return [], False, conn
                break
            except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                if attempt == max_attempts:
                    logger.error(
                        "  Folder '%s': IMAP abort during SEARCH after %d attempts: %s",
                        folder, max_attempts, e,
                    )
                    self.status.set_abort_detail({
                        "folder": folder,
                        "phase": "search",
                        "raw_message": str(e),
                        "commands_in_session": self.status.get_cmd_count(),
                        "fetched_this_cycle": prior_fetched_total,
                        "attempts": max_attempts,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    })
                    return [], True, conn
                logger.warning(
                    "  Folder '%s': IMAP abort during SEARCH (attempt %d/%d): %s",
                    folder, attempt, max_attempts, e,
                )
                conn = self._reconnect_after_abort(conn, account, attempt)
                # SELECT state is lost across reconnect — restore it. If the
                # re-SELECT fails we cannot continue: any subsequent SEARCH/FETCH
                # would hit "illegal in state AUTH" and spin in the retry loop.
                try:
                    self.status.add_commands(1)
                    conn.select(f'"{folder}"', readonly=True)
                except Exception as sel_err:
                    logger.error(
                        "  Folder '%s': re-SELECT after reconnect failed during SEARCH: %s — aborting folder",
                        folder, sel_err,
                    )
                    self.status.set_abort_detail({
                        "folder": folder,
                        "phase": "search_reselect",
                        "raw_message": str(sel_err),
                        "commands_in_session": self.status.get_cmd_count(),
                        "fetched_this_cycle": prior_fetched_total,
                        "attempts": attempt,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    })
                    return [], True, conn
            except Exception as e:
                logger.warning("  Folder '%s': search failed: %s", folder, e)
                return [], False, conn

        raw_uids = uid_data[0].split() if uid_data and uid_data[0] else []
        # Defensive filter: SEARCH UID N:* may return UIDs <= last_uid in edge cases
        # (e.g. when last_uid+1 > UIDNEXT and the range is interpreted reversed)
        uids = [u for u in raw_uids if int(u) > last_uid]
        if not uids:
            logger.info("  Folder '%s': 0 new emails (last_uid=%d)", folder, last_uid)
            return [], False, conn

        logger.info("  Folder '%s': %d new emails to fetch (UID > %d)", folder, len(uids), last_uid)
        self.status.set_folder_progress(0, len(uids))
        emails: list[dict] = []

        fetch_batch = self.config.indexer.imap_fetch_batch
        batch_delay = self.config.indexer.imap_batch_delay
        for batch_start in range(0, len(uids), fetch_batch):
            batch_uids = uids[batch_start : batch_start + fetch_batch]
            uid_set = b",".join(batch_uids)

            if batch_start > 0:
                time.sleep(batch_delay)  # Throttle between fetch batches

            # FETCH this batch (with reconnect-on-abort + re-SELECT)
            msg_data = None
            batch_aborted = False
            for attempt in range(1, max_attempts + 1):
                try:
                    self.status.add_commands(1)
                    status, msg_data = conn.uid("FETCH", uid_set, "(RFC822)")
                    if status != "OK":
                        logger.warning("  Folder '%s': FETCH non-OK for batch %d", folder, batch_start)
                        msg_data = None
                    break
                except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                    if attempt == max_attempts:
                        logger.error(
                            "  Folder '%s': IMAP abort at batch %d after %d attempts: %s. "
                            "Returning %d emails fetched before abort.",
                            folder, batch_start, max_attempts, e, len(emails),
                        )
                        self.status.set_abort_detail({
                            "folder": folder,
                            "phase": "fetch",
                            "raw_message": str(e),
                            "commands_in_session": self.status.get_cmd_count(),
                            "fetched_this_cycle": prior_fetched_total + len(emails),
                            "batch_index": batch_start // fetch_batch,
                            "uids_in_failing_batch": len(batch_uids),
                            "attempts": max_attempts,
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                        })
                        batch_aborted = True
                        break
                    logger.warning(
                        "  Folder '%s': IMAP abort at batch %d (attempt %d/%d): %s — reconnecting",
                        folder, batch_start, attempt, max_attempts, e,
                    )
                    conn = self._reconnect_after_abort(conn, account, attempt)
                    try:
                        self.status.add_commands(1)
                        conn.select(f'"{folder}"', readonly=True)
                    except Exception as sel_err:
                        # Without a selected mailbox the next FETCH would raise
                        # "illegal in state AUTH" and the outer batch loop would
                        # spin every imap_batch_delay seconds. Abort the folder.
                        logger.error(
                            "  Folder '%s': re-SELECT after reconnect failed at batch %d: %s — aborting folder",
                            folder, batch_start, sel_err,
                        )
                        self.status.set_abort_detail({
                            "folder": folder,
                            "phase": "fetch_reselect",
                            "raw_message": str(sel_err),
                            "commands_in_session": self.status.get_cmd_count(),
                            "fetched_this_cycle": prior_fetched_total + len(emails),
                            "batch_index": batch_start // fetch_batch,
                            "uids_in_failing_batch": len(batch_uids),
                            "attempts": attempt,
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                        })
                        batch_aborted = True
                        break
                except Exception as e:
                    logger.warning("  Folder '%s': fetch batch failed: %s", folder, e)
                    msg_data = None
                    break

            if batch_aborted:
                emails.sort(key=lambda em: em["uid"])
                return emails, True, conn
            if msg_data is None:
                continue

            # Sum batch bandwidth (raw RFC822 bytes) for daily quota tracking.
            batch_bytes = sum(
                len(item[1]) for item in msg_data
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes)
            )
            if batch_bytes:
                self.status.add_bytes(batch_bytes)

            # Parse the batch response — msg_data contains pairs of (envelope, body)
            # interspersed with b')' closing markers. The envelope contains "UID N".
            for item in msg_data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                envelope = item[0]
                raw = item[1]
                if not isinstance(raw, bytes):
                    continue
                uid_value: Optional[int] = None
                if isinstance(envelope, bytes):
                    uid_match = re.search(rb"UID (\d+)", envelope)
                    if uid_match:
                        uid_value = int(uid_match.group(1))
                if uid_value is None:
                    continue  # cannot checkpoint without UID — skip
                try:
                    msg = email.message_from_bytes(raw)
                    message_id = msg.get("Message-ID", "").strip()
                    if not message_id:
                        continue

                    emails.append({
                        "uid": uid_value,
                        "message_id": message_id,
                        "subject": decode_header_value(msg.get("Subject")),
                        "from": decode_header_value(msg.get("From")),
                        "to": decode_header_value(msg.get("To")),
                        "date": parse_date(msg.get("Date")),
                        "body": extract_body(msg, self.config.indexer.max_body_chars),
                        "folder": folder,
                        "account": account.name,
                    })
                except Exception as e:
                    logger.warning("Error parsing email in %s/%s: %s", account.name, folder, e)
                    continue

            fetched_so_far = min(batch_start + len(batch_uids), len(uids))
            self.status.set_folder_progress(fetched_so_far, len(uids))
            self.status.set_fetched_count(prior_fetched_total + len(emails))
            logger.info("  Folder '%s': fetched %d/%d", folder, fetched_so_far, len(uids))

        # Ensure ascending UID order for monotonic checkpointing
        emails.sort(key=lambda em: em["uid"])
        return emails, False, conn

    def _record_usage_bucket(
        self,
        acct_state: dict,
        bytes_used: int,
        commands_used: int,
        fetched_emails: int,
    ) -> None:
        """Add this cycle's usage to a rolling per-hour bucket in state.json.

        Buckets are keyed by hour ("2026-05-05T17:00") so multiple cycles in
        the same hour coalesce. Capped at 25 entries (24h + a tiny buffer);
        older buckets are pruned. The 24h sum is computed on demand.
        """
        buckets = acct_state.setdefault("usage_buckets", {})
        now = datetime.now(timezone.utc)
        key = now.replace(minute=0, second=0, microsecond=0).isoformat()
        bucket = buckets.setdefault(key, {"bytes": 0, "commands": 0, "fetched_emails": 0})
        bucket["bytes"] = int(bucket.get("bytes", 0)) + int(bytes_used)
        bucket["commands"] = int(bucket.get("commands", 0)) + int(commands_used)
        bucket["fetched_emails"] = int(bucket.get("fetched_emails", 0)) + int(fetched_emails)

        # Prune buckets older than 25h to keep state.json small
        cutoff = now - timedelta(hours=25)
        stale = [k for k in buckets if self._parse_bucket_key(k) < cutoff]
        for k in stale:
            del buckets[k]
        # Hard cap regardless (defensive)
        if len(buckets) > 30:
            sorted_keys = sorted(buckets.keys())
            for k in sorted_keys[: len(buckets) - 30]:
                del buckets[k]
        self._save_state()

    @staticmethod
    def _parse_bucket_key(key: str) -> datetime:
        try:
            return datetime.fromisoformat(key)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    def _get_24h_usage(self, acct_state: dict) -> dict:
        """Sum hourly buckets within the last 24h. Returns aggregated totals."""
        buckets = acct_state.get("usage_buckets", {}) or {}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        total_bytes = 0
        total_cmds = 0
        total_fetched = 0
        oldest = None
        newest = None
        for key, bucket in buckets.items():
            ts = self._parse_bucket_key(key)
            if ts < cutoff:
                continue
            total_bytes += int(bucket.get("bytes", 0))
            total_cmds += int(bucket.get("commands", 0))
            total_fetched += int(bucket.get("fetched_emails", 0))
            if oldest is None or ts < oldest:
                oldest = ts
            if newest is None or ts > newest:
                newest = ts
        return {
            "bytes": total_bytes,
            "commands": total_cmds,
            "fetched_emails": total_fetched,
            "oldest_bucket": oldest.isoformat() if oldest else "",
            "newest_bucket": newest.isoformat() if newest else "",
        }

    HISTORY_CAP = 50  # rolling cycle-history window per account (was 10)

    def _record_run(
        self,
        acct_state: dict,
        started_at: datetime,
        indexed: int,
        outcome: str,
        error: str = "",
    ) -> None:
        """Persist per-cycle metadata, append to rolling history, update lifetime totals."""
        ended_at = datetime.now(timezone.utc)
        duration = (ended_at - started_at).total_seconds()
        acct_state["last_run_started_at"] = started_at.isoformat()
        acct_state["last_run_at"] = ended_at.isoformat()
        acct_state["last_run_duration_seconds"] = round(duration, 1)
        acct_state["last_run_indexed"] = indexed
        acct_state["last_run_outcome"] = outcome
        acct_state["last_run_error"] = error

        # Lifetime cumulative stats — survive history-window pruning so the user
        # always sees the big picture even if individual cycle entries roll off.
        # On first creation, look for the earliest available timestamp (cycle history,
        # last_run timestamps) to give upgraded users a reasonable "since" rather
        # than always saying "started just now".
        if "lifetime" not in acct_state:
            since = self._find_earliest_known_timestamp(acct_state, fallback=started_at.isoformat())
            acct_state["lifetime"] = {
                "cycles": 0,
                "indexed": 0,
                "bytes": 0,
                "since": since,
            }
        lifetime = acct_state["lifetime"]
        lifetime["cycles"] = int(lifetime.get("cycles", 0)) + 1
        lifetime["indexed"] = int(lifetime.get("indexed", 0)) + indexed
        # Bytes are filled in by _record_usage_bucket; we read the just-recorded value
        # via the snapshot in the caller, but easier here to add this cycle's bytes
        # straight from the status snapshot.
        try:
            cycle_bytes = self.status.snapshot().get("bytes_this_cycle", 0)
            lifetime["bytes"] = int(lifetime.get("bytes", 0)) + int(cycle_bytes)
        except Exception:
            pass
        if not lifetime.get("since"):
            lifetime["since"] = started_at.isoformat()

        history = acct_state.setdefault("history", [])
        history.append({
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": round(duration, 1),
            "indexed": indexed,
            "outcome": outcome,
            "error": error,
        })
        if len(history) > self.HISTORY_CAP:
            del history[: len(history) - self.HISTORY_CAP]
        self._save_state()

    def _find_earliest_known_timestamp(self, acct_state: dict, fallback: str) -> str:
        """Best-effort earliest timestamp for an account, used for lifetime.since.

        Walks history (oldest entry first) and falls back to last_run_started_at
        and last_run_at. Returns `fallback` if nothing's available — typically
        the case for a fresh state file where this cycle is genuinely the first.
        """
        history = acct_state.get("history", []) or []
        candidates: list[str] = []
        for entry in history:
            ts = entry.get("started_at") or entry.get("ended_at")
            if ts:
                candidates.append(ts)
        for key in ("last_run_started_at", "last_run_at"):
            ts = acct_state.get(key)
            if ts:
                candidates.append(ts)
        if not candidates:
            return fallback
        # Lexicographic sort works on ISO 8601 timestamps
        return min(candidates)

    def last_cycle_was_rate_limited(self) -> bool:
        """True if any account's most recent cycle aborted with rate_limit outcome.

        Used by the scheduler to drive adaptive backoff.
        """
        for account in self.config.accounts:
            outcome = self._state.get(account.name, {}).get("last_run_outcome", "")
            if outcome == "aborted_rate_limit":
                return True
        return False

    def is_account_paused(self, account_name: str) -> tuple[bool, str]:
        """Check if an account is currently paused. Returns (is_paused, paused_until_iso).

        An account is paused when state[acct]["paused_until"] is in the future.
        Past timestamps are silently ignored (treated as "not paused"); cleared
        on the next call to pause/resume to keep state.json tidy.
        """
        acct_state = self._state.get(account_name, {})
        paused_until_iso = acct_state.get("paused_until", "")
        if not paused_until_iso:
            return False, ""
        try:
            paused_until = datetime.fromisoformat(paused_until_iso)
        except Exception:
            return False, ""
        if paused_until <= datetime.now(timezone.utc):
            return False, paused_until_iso  # expired but not yet cleared
        return True, paused_until_iso

    def pause_account(self, account_name: str, hours: float = 6.0) -> str:
        """Pause an account for N hours (default 6). Returns the paused_until ISO timestamp."""
        if account_name not in {a.name for a in self.config.accounts}:
            raise ValueError(f"Account '{account_name}' not configured")
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        until_iso = until.isoformat()
        acct_state = self._state.setdefault(account_name, {})
        acct_state["paused_until"] = until_iso
        self._save_state()
        logger.info("Account '%s' paused until %s (%.1f hours)", account_name, until_iso, hours)
        return until_iso

    def resume_account(self, account_name: str) -> None:
        """Manually clear an account's paused_until."""
        if account_name not in {a.name for a in self.config.accounts}:
            raise ValueError(f"Account '{account_name}' not configured")
        acct_state = self._state.setdefault(account_name, {})
        if "paused_until" in acct_state:
            del acct_state["paused_until"]
            self._save_state()
            logger.info("Account '%s' resumed", account_name)

    def get_scheduler_persisted_state(self) -> dict:
        """Read scheduler-managed state from state.json.

        Currently holds `consecutive_rate_limit_aborts` so adaptive backoff
        survives container restarts (otherwise restart would reset to 0 and
        the indexer would hammer Gmail at the base 15-min cadence again,
        even if cycles were still failing).
        """
        return dict(self._state.get("_scheduler", {}))

    def save_scheduler_persisted_state(self, state: dict) -> None:
        self._state["_scheduler"] = dict(state)
        self._save_state()

    def _index_account(self, account: ImapAccount, loop: asyncio.AbstractEventLoop) -> tuple[int, bool]:
        """Index a single account using per-folder UID checkpoints.

        Each folder is fetched, embedded, upserted, and checkpointed independently,
        so a mid-account abort (rate limit, embed failure) preserves all completed
        per-folder progress. Returns (emails_indexed, was_aborted).
        """
        logger.info("Indexing account '%s' (%s)", account.name, account.host)
        started_at = datetime.now(timezone.utc)
        acct_state = self._state.setdefault(account.name, {})
        self.status.begin_cycle()

        try:
            conn = self._connect(account)
        except Exception as e:
            logger.error("Failed to connect to %s: %s", account.name, e)
            self.status.set_error(f"IMAP connection failed: {e}")
            self._record_run(acct_state, started_at, 0, "connection_failed", str(e))
            return 0, True

        try:  # noqa: outer try for all IMAP operations
            folders = self._get_folders(conn, account)
            folder_state: dict = acct_state.setdefault("folders", {})

            # Prune stale folder_state entries from a previous indexing strategy
            # (e.g. INBOX + user labels left over after switching to Gmail's
            # All-Mail-only mode). They were never being iterated but the dashboard
            # still rendered their stale checkpoint as "in progress". Qdrant data
            # is untouched — emails stay searchable, just with their original
            # folder name in the payload.
            current_set = set(folders)
            stale = [name for name in folder_state if name not in current_set]
            if stale:
                logger.info(
                    "Account '%s': pruning %d stale folder checkpoint(s) no longer "
                    "in the indexing strategy: %s",
                    account.name, len(stale), stale,
                )
                for name in stale:
                    del folder_state[name]
                self._save_state()

            logger.info("Account '%s': indexing %d folders: %s", account.name, len(folders), folders)
            folder_delay = self.config.indexer.imap_folder_delay
            embed_batch_size = self.config.indexer.batch_size
            existing_ids = self.store.get_all_ids(account=account.name)
            indexed = 0
            fetched_total = 0
            aborted = False
            outcome = "completed"
            error_msg = ""

            # STATUS pre-pass: gather UIDVALIDITY+UIDNEXT+MESSAGES for every folder upfront.
            # MESSAGES is the *current* count of emails (UIDNEXT keeps growing past deletions
            # and would over-estimate by 2x or more on long-lived Gmail accounts).
            # We use sum(MESSAGES) - already_indexed as the cycle's unique-email work estimate.
            # The STATUS calls front-load IMAP work we'd do anyway during fetch.
            logger.info("Account '%s': STATUS pre-pass over %d folders", account.name, len(folders))
            folder_uid_info: dict[str, tuple[Optional[int], Optional[int]]] = {}
            total_messages = 0
            for folder in folders:
                uv_pre, un_pre, msgs_pre = self._get_folder_uid_state(conn, folder)
                folder_uid_info[folder] = (uv_pre, un_pre)
                if msgs_pre is not None:
                    total_messages += msgs_pre
                logger.info(
                    "  Folder '%s': UIDVALIDITY=%s UIDNEXT=%s MESSAGES=%s",
                    folder, uv_pre, un_pre, msgs_pre,
                )

            # cycle_total is "remaining unique emails to handle this cycle" — works as a
            # denominator for the embedded-count progress bar. Drops to 0 once caught up.
            already_indexed = self.store.count(account=account.name)
            cycle_total = max(0, total_messages - already_indexed)
            self.status.set_cycle_total(cycle_total)
            logger.info(
                "Account '%s': %d total messages on server, %d already indexed → "
                "estimated %d remaining this cycle",
                account.name, total_messages, already_indexed, cycle_total,
            )

            for fi, folder in enumerate(folders):
                self.status.set_fetching(account.name, folder, fi, len(folders))
                self.status.set_fetched_count(fetched_total)
                if fi > 0:
                    time.sleep(folder_delay)  # Throttle between folders

                # Resolve UIDVALIDITY/UIDNEXT from pre-pass cache (avoid duplicate STATUS)
                uv, uidnext = folder_uid_info.get(folder, (None, None))
                saved = folder_state.get(folder, {}) if isinstance(folder_state.get(folder), dict) else {}
                if uv is None:
                    logger.warning(
                        "  Folder '%s': STATUS unavailable, falling back to full scan without checkpoint",
                        folder,
                    )
                    last_uid = 0
                    can_checkpoint = False
                else:
                    can_checkpoint = True
                    if str(saved.get("uidvalidity")) != str(uv):
                        if saved:
                            logger.info(
                                "  Folder '%s': UIDVALIDITY changed (%s -> %s), resetting checkpoint",
                                folder, saved.get("uidvalidity"), uv,
                            )
                        last_uid = 0
                    else:
                        last_uid = int(saved.get("last_uid", 0))

                if uidnext is not None and last_uid + 1 >= uidnext:
                    logger.info(
                        "  Folder '%s': up-to-date (last_uid=%d, UIDNEXT=%d)",
                        folder, last_uid, uidnext,
                    )
                    # Persist UIDVALIDITY even when no fetch is needed (handles first cycle on empty folder)
                    if can_checkpoint:
                        prev = folder_state.get(folder, {}) if isinstance(folder_state.get(folder), dict) else {}
                        folder_state[folder] = {
                            "uidvalidity": str(uv),
                            "last_uid": last_uid,
                            "uidnext": uidnext,
                            "last_fetched_at": prev.get("last_fetched_at", ""),
                            "last_fetched_count": 0,
                        }
                        self._save_state()
                    continue

                logger.info(
                    "  Folder '%s': UIDVALIDITY=%s UIDNEXT=%s, last_uid=%d",
                    folder, uv, uidnext, last_uid,
                )

                folder_emails, fetch_aborted, conn = self._fetch_emails(
                    conn, folder, account, last_uid, prior_fetched_total=fetched_total,
                )
                fetched_total += len(folder_emails)
                self.status.set_fetched_count(fetched_total)

                if folder_emails:
                    self.status.set_embedding(account.name, indexed + len(folder_emails))

                # Process in UID-ascending batches: filter -> embed -> upsert -> checkpoint
                embed_failed = False
                for i in range(0, len(folder_emails), embed_batch_size):
                    batch = folder_emails[i : i + embed_batch_size]
                    new_in_batch = [
                        em for em in batch
                        if make_point_id(em["message_id"]) not in existing_ids
                    ]
                    filtered_in_batch = len(batch) - len(new_in_batch)
                    if filtered_in_batch:
                        self.status.add_filtered(filtered_in_batch)

                    if new_in_batch:
                        texts = [
                            f"From: {em['from']}\nTo: {em['to']}\nSubject: {em['subject']}\n"
                            f"Date: {em['date']}\n\n{em['body']}"
                            for em in new_in_batch
                        ]
                        try:
                            embeddings = asyncio.run_coroutine_threadsafe(
                                self.embedder.embed(texts), loop
                            ).result(timeout=300)
                        except Exception as e:
                            logger.error(
                                "Embedding failed for batch in %s/%s: %s",
                                account.name, folder, e,
                            )
                            self.status.set_error(f"Embedding failed: {e}")
                            embed_failed = True
                            aborted = True
                            break

                        points = []
                        skipped_in_batch = 0
                        for em, vector in zip(new_in_batch, embeddings):
                            point_id = make_point_id(em["message_id"])
                            if vector is None:
                                # Ollama rejected this single input (bisect-isolated 400).
                                # Tombstone it via existing_ids so we don't re-fetch every cycle.
                                skipped_in_batch += 1
                                existing_ids.add(point_id)
                                continue
                            snippet = em["body"][:300]
                            points.append(
                                qmodels.PointStruct(
                                    id=point_id,
                                    vector=vector,
                                    payload={
                                        "message_id": em["message_id"],
                                        "subject": em["subject"],
                                        "from": em["from"],
                                        "to": em["to"],
                                        "date": em["date"],
                                        "folder": em["folder"],
                                        "account": em["account"],
                                        "snippet": snippet,
                                        "body": em["body"],
                                    },
                                )
                            )
                            existing_ids.add(point_id)

                        if points:
                            self.store.upsert(points)
                        indexed += len(points)
                        self.status.set_progress(indexed)
                        if skipped_in_batch:
                            self.status.add_skipped_ollama(skipped_in_batch)
                            logger.warning(
                                "Account '%s'/'%s': skipped %d emails Ollama rejected as unembeddable",
                                account.name, folder, skipped_in_batch,
                            )

                    # Advance per-folder checkpoint to max UID processed in this batch
                    # (whether newly indexed or already-existing). Persist immediately
                    # so a subsequent failure doesn't lose this batch's progress.
                    if can_checkpoint:
                        batch_max_uid = max(em["uid"] for em in batch)
                        folder_state[folder] = {
                            "uidvalidity": str(uv),
                            "last_uid": batch_max_uid,
                            "uidnext": uidnext,
                            "last_fetched_at": datetime.now(timezone.utc).isoformat(),
                            "last_fetched_count": len(folder_emails),
                        }
                        self._save_state()

                if fetch_aborted:
                    msg = f"Rate limited in folder '{folder}'. Partial index."
                    abort_detail = self.status.snapshot().get("last_abort_detail", {}) or {}
                    raw = abort_detail.get("raw_message") or ""
                    cmds = abort_detail.get("commands_in_session", 0)
                    logger.error(
                        "Account '%s': IMAP rate limit during fetch in folder '%s' "
                        "(after %d commands this cycle): %s",
                        account.name, folder, cmds, raw,
                    )
                    self.status.set_error(msg)
                    # Persist diagnostic detail to state so the addon can render "why" on idle
                    if abort_detail:
                        acct_state["last_abort_detail"] = abort_detail
                    aborted = True
                    outcome = "aborted_rate_limit"
                    error_msg = msg
                    break
                if embed_failed:
                    logger.error(
                        "Account '%s': stopping further folders this cycle due to embed failure",
                        account.name,
                    )
                    outcome = "aborted_embed"
                    # error_msg already set via status.last_error during embed exception
                    error_msg = self.status.snapshot().get("last_error", "Embedding failed")
                    acct_state["last_abort_detail"] = {
                        "folder": folder,
                        "phase": "embed",
                        "raw_message": error_msg,
                        "commands_in_session": self.status.get_cmd_count(),
                        "fetched_this_cycle": fetched_total,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    }
                    break

                # First-sync rate-limit guard: end the cycle voluntarily before Gmail throttles us.
                # The next cycle resumes from the per-folder UID checkpoint we just persisted.
                max_fetches = self.config.indexer.imap_max_fetches_per_cycle
                if max_fetches > 0 and fetched_total >= max_fetches:
                    logger.info(
                        "Account '%s': fetch cap reached (%d emails this cycle) — "
                        "ending gracefully, will resume next cycle",
                        account.name, fetched_total,
                    )
                    aborted = True  # skip cleanup to preserve quota for the next cycle's fetch
                    outcome = "cap_reached"
                    error_msg = f"Cycle paused at fetch cap ({max_fetches})"
                    break

            self._record_run(acct_state, started_at, indexed, outcome, error_msg)
            # Persist this cycle's IMAP usage into the rolling-24h hourly buckets
            # so the addon can show "today's usage vs Gmail's documented limits".
            snap = self.status.snapshot()
            self._record_usage_bucket(
                acct_state,
                bytes_used=snap.get("bytes_this_cycle", 0),
                commands_used=snap.get("cmds_this_cycle", 0),
                fetched_emails=fetched_total,
            )
            # Clear stale abort detail when the cycle ended cleanly so the addon
            # doesn't keep showing "rate-limited" forever after recovery.
            if outcome == "completed" and "last_abort_detail" in acct_state:
                del acct_state["last_abort_detail"]
                self._save_state()

            if aborted:
                logger.info(
                    "Account '%s': partial run (%s), per-folder checkpoints preserved",
                    account.name, outcome,
                )
            else:
                logger.info("Account '%s': cycle complete, %d new emails indexed", account.name, indexed)

            return indexed, aborted

        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _cleanup_account(self, account: ImapAccount) -> int:
        """Remove Qdrant points for emails deleted from server. Returns count removed.

        Throttled by config.indexer.cleanup_interval_hours so this expensive
        full-folder header scan doesn't run on every 15-min cycle.
        """
        acct_state = self._state.setdefault(account.name, {})
        interval_seconds = self.config.indexer.cleanup_interval_hours * 3600
        last_cleanup_iso = acct_state.get("last_cleanup_at")
        if last_cleanup_iso and interval_seconds > 0:
            try:
                last_cleanup = datetime.fromisoformat(last_cleanup_iso)
                age_seconds = (datetime.now(timezone.utc) - last_cleanup).total_seconds()
                if age_seconds < interval_seconds:
                    logger.info(
                        "Cleanup for '%s' skipped (last ran %.1fh ago, interval %dh)",
                        account.name, age_seconds / 3600, self.config.indexer.cleanup_interval_hours,
                    )
                    return 0
            except Exception:
                pass  # malformed timestamp — fall through and run cleanup

        logger.info("Cleanup pass for account '%s'", account.name)
        try:
            conn = self._connect(account)
        except Exception as e:
            logger.error("Cleanup: failed to connect to %s: %s", account.name, e)
            return 0

        try:
            # Mark the attempt timestamp now (not on success) so the throttle
            # applies even when cleanup aborts mid-scan — we don't want to
            # re-burn rate-limit budget every cycle if cleanup keeps failing.
            acct_state["last_cleanup_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()

            folders = self._get_folders(conn, account)
            self.status.set_cleanup(account.name, len(folders))
            server_point_ids: set[str] = set()
            skipped_folders = 0

            folder_delay = self.config.indexer.imap_folder_delay
            batch_delay = self.config.indexer.imap_batch_delay
            # Use a larger batch for cleanup since headers are much smaller than full emails
            cleanup_batch = self.config.indexer.imap_fetch_batch * 4

            max_attempts = self.IMAP_RETRY_MAX_ATTEMPTS

            for fi, folder in enumerate(folders):
                self.status.set_cleanup_progress(folder, fi)
                if fi > 0:
                    time.sleep(folder_delay)

                # SELECT with reconnect-on-abort
                selected = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        status, _ = conn.select(f'"{folder}"', readonly=True)
                        selected = status == "OK"
                        break
                    except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                        if attempt == max_attempts:
                            logger.error(
                                "Cleanup: SELECT '%s' aborted after %d attempts: %s",
                                folder, max_attempts, e,
                            )
                            skipped_folders += 1
                            break
                        logger.warning(
                            "Cleanup: SELECT '%s' aborted (attempt %d/%d): %s",
                            folder, attempt, max_attempts, e,
                        )
                        try:
                            conn = self._reconnect_after_abort(conn, account, attempt)
                        except Exception as reconn_err:
                            logger.error("Cleanup: reconnect failed: %s. Aborting cleanup.", reconn_err)
                            return 0
                    except Exception as e:
                        logger.warning("Cleanup: error selecting '%s': %s", folder, e)
                        break
                if not selected:
                    continue

                # SEARCH with reconnect-on-abort + re-SELECT
                uid_data = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        status, uid_data = conn.search(None, "ALL")
                        if status != "OK":
                            uid_data = None
                        break
                    except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                        if attempt == max_attempts:
                            logger.error(
                                "Cleanup: SEARCH '%s' aborted after %d attempts: %s",
                                folder, max_attempts, e,
                            )
                            skipped_folders += 1
                            uid_data = None
                            break
                        logger.warning(
                            "Cleanup: SEARCH '%s' aborted (attempt %d/%d): %s",
                            folder, attempt, max_attempts, e,
                        )
                        try:
                            conn = self._reconnect_after_abort(conn, account, attempt)
                            conn.select(f'"{folder}"', readonly=True)
                        except Exception as reconn_err:
                            logger.error("Cleanup: reconnect failed: %s. Aborting cleanup.", reconn_err)
                            return 0
                    except Exception as e:
                        logger.warning("Cleanup: search failed in '%s': %s", folder, e)
                        uid_data = None
                        break
                if uid_data is None:
                    continue

                uids = uid_data[0].split()
                folder_aborted = False

                # Batch fetch MESSAGE-ID headers (with reconnect-on-abort per batch)
                for batch_start in range(0, len(uids), cleanup_batch):
                    batch_uids = uids[batch_start : batch_start + cleanup_batch]
                    uid_set = b",".join(batch_uids)

                    if batch_start > 0:
                        time.sleep(batch_delay)

                    msg_data = None
                    for attempt in range(1, max_attempts + 1):
                        try:
                            status, msg_data = conn.fetch(
                                uid_set, "(BODY[HEADER.FIELDS (MESSAGE-ID)])"
                            )
                            if status != "OK":
                                msg_data = None
                            break
                        except (imaplib.IMAP4.abort, OSError, ConnectionError) as e:
                            if attempt == max_attempts:
                                logger.error(
                                    "Cleanup: FETCH '%s' batch %d aborted after %d attempts: %s",
                                    folder, batch_start, max_attempts, e,
                                )
                                skipped_folders += 1
                                msg_data = None
                                folder_aborted = True
                                break
                            logger.warning(
                                "Cleanup: FETCH '%s' batch %d aborted (attempt %d/%d): %s",
                                folder, batch_start, attempt, max_attempts, e,
                            )
                            try:
                                conn = self._reconnect_after_abort(conn, account, attempt)
                                conn.select(f'"{folder}"', readonly=True)
                            except Exception as reconn_err:
                                logger.error("Cleanup: reconnect failed: %s. Aborting cleanup.", reconn_err)
                                return 0
                        except Exception as e:
                            logger.warning("Cleanup: fetch error in '%s': %s", folder, e)
                            msg_data = None
                            break

                    if folder_aborted:
                        break
                    if msg_data is None:
                        continue

                    for item in msg_data:
                        if not isinstance(item, tuple) or len(item) < 2:
                            continue
                        raw = item[1]
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        match = re.search(r"Message-ID:\s*(.+)", raw, re.IGNORECASE)
                        if match:
                            mid = match.group(1).strip()
                            server_point_ids.add(make_point_id(mid))

            if skipped_folders > 0:
                logger.warning(
                    "Cleanup: skipped %d folders due to connection errors. "
                    "Skipping deletion to avoid false positives.",
                    skipped_folders,
                )
                return 0

            qdrant_ids = self.store.get_all_ids(account=account.name)
            stale_ids = qdrant_ids - server_point_ids

            if stale_ids:
                self.store.delete(list(stale_ids))
                logger.info(
                    "Cleanup: removed %d stale entries for account '%s'",
                    len(stale_ids), account.name,
                )

            acct_state["last_cleanup_removed"] = len(stale_ids)
            acct_state["last_cleanup_outcome"] = "completed"
            self._save_state()
            return len(stale_ids)

        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def run_full_index(
        self,
        loop: asyncio.AbstractEventLoop,
        account_name: Optional[str] = None,
    ) -> None:
        """Run index + cleanup for all accounts (or a specific one)."""
        accounts = self.config.accounts
        if account_name:
            accounts = [a for a in accounts if a.name == account_name]
            if not accounts:
                logger.error("Account '%s' not found in config", account_name)
                self.status.set_error(f"Account '{account_name}' not found")
                return

        total_indexed = 0
        total_cleaned = 0

        for account in accounts:
            # Honor manual pause. A manually-triggered reindex (account_name set)
            # bypasses the pause — clicking Reindex on a paused account should
            # work as an explicit user override.
            paused, paused_until = self.is_account_paused(account.name)
            if paused and not account_name:
                logger.info(
                    "Account '%s' is paused until %s — skipping this cycle",
                    account.name, paused_until,
                )
                continue

            was_aborted = False
            try:
                indexed, was_aborted = self._index_account(account, loop)
                total_indexed += indexed
            except Exception as e:
                logger.error("Index failed for account '%s': %s", account.name, e)
                self.status.set_error(f"Index failed: {e}")
                was_aborted = True  # Don't waste rate-limit budget on cleanup

            if self.config.indexer.cleanup_enabled and not was_aborted:
                try:
                    cleaned = self._cleanup_account(account)
                    total_cleaned += cleaned
                except Exception as e:
                    logger.error("Cleanup failed for account '%s': %s", account.name, e)
            elif was_aborted:
                logger.info(
                    "Skipping cleanup for account '%s' — fetch was rate-limited, "
                    "saving remaining quota for next indexing cycle",
                    account.name,
                )

        self.status.set_done()
        logger.info(
            "Index complete: %d new emails indexed, %d stale entries removed",
            total_indexed, total_cleaned,
        )

    def start_background(
        self,
        loop: asyncio.AbstractEventLoop,
        account_name: Optional[str] = None,
    ) -> threading.Thread:
        """Start indexing in a background thread."""
        thread = threading.Thread(
            target=self.run_full_index,
            args=(loop, account_name),
            daemon=True,
        )
        thread.start()
        return thread

    def _next_eligible_cleanup(self, last_cleanup_iso: str) -> str:
        """Compute when the next cleanup pass becomes eligible given the throttle window."""
        if not last_cleanup_iso:
            return ""
        try:
            last = datetime.fromisoformat(last_cleanup_iso)
        except Exception:
            return ""
        return (last + timedelta(hours=self.config.indexer.cleanup_interval_hours)).isoformat()

    def _next_run_estimate(self, last_run_iso: str) -> str:
        """Compute estimated next-run time from last run + scheduler interval."""
        if not last_run_iso or self.config.indexer.schedule_minutes <= 0:
            return ""
        try:
            last = datetime.fromisoformat(last_run_iso)
        except Exception:
            return ""
        return (last + timedelta(minutes=self.config.indexer.schedule_minutes)).isoformat()

    def _folder_summary(self, folder_state: dict) -> list[dict]:
        """Sort folders by remaining work descending so the addon can show the busiest folders first."""
        rows = []
        for name, fs in folder_state.items():
            if not isinstance(fs, dict):
                continue
            last_uid = int(fs.get("last_uid", 0))
            uidnext = fs.get("uidnext")
            try:
                uidnext_int = int(uidnext) if uidnext is not None else None
            except (TypeError, ValueError):
                uidnext_int = None
            total = (uidnext_int - 1) if uidnext_int and uidnext_int > 0 else None
            remaining = max(0, total - last_uid) if total is not None else None
            pct = None
            if total is not None and total > 0:
                pct = round(min(100.0, last_uid / total * 100), 1)
            rows.append({
                "name": name,
                "last_uid": last_uid,
                "uidnext": uidnext_int,
                "approx_total": total,
                "remaining": remaining,
                "indexed_pct": pct,
                "uidvalidity": fs.get("uidvalidity", ""),
                "last_fetched_at": fs.get("last_fetched_at", ""),
                "last_fetched_count": fs.get("last_fetched_count", 0),
            })
        # Sort: folders with remaining work first (largest first), then settled folders
        rows.sort(key=lambda r: (r["remaining"] is None, -(r["remaining"] or 0), r["name"]))
        return rows

    def get_account_stats(self) -> list[dict]:
        """Get per-account statistics enriched with cycle history and per-folder progress."""
        stats = []
        for account in self.config.accounts:
            count = self.store.count(account=account.name)
            acct_state = self._state.get(account.name, {})
            # Prefer new ISO timestamp, fall back to legacy date string for one release
            last_run = acct_state.get("last_run_at") or acct_state.get("last_run_date", "")
            status_snap = self.status.snapshot()
            if status_snap["running"] and status_snap["current_account"] == account.name:
                acct_status = "indexing"
            elif acct_state.get("last_run_outcome", "completed") not in ("completed", ""):
                acct_status = "error"
            else:
                acct_status = "idle"

            folder_state = acct_state.get("folders", {}) or {}
            folders_detail = self._folder_summary(folder_state)

            last_cleanup = acct_state.get("last_cleanup_at", "")

            stats.append({
                "name": account.name,
                "email_count": count,
                "last_index_time": last_run,
                "folders": account.folders or ["(all)"],
                "status": acct_status,
                # Enriched cycle info
                "last_run": {
                    "started_at": acct_state.get("last_run_started_at", ""),
                    "ended_at": acct_state.get("last_run_at", ""),
                    "duration_seconds": acct_state.get("last_run_duration_seconds", 0),
                    "indexed": acct_state.get("last_run_indexed", 0),
                    "outcome": acct_state.get("last_run_outcome", ""),
                    "error": acct_state.get("last_run_error", ""),
                },
                "next_run_at": self._next_run_estimate(last_run),
                "cleanup": {
                    "last_at": last_cleanup,
                    "next_eligible_at": self._next_eligible_cleanup(last_cleanup),
                    "last_removed": acct_state.get("last_cleanup_removed", 0),
                    "last_outcome": acct_state.get("last_cleanup_outcome", ""),
                    "interval_hours": self.config.indexer.cleanup_interval_hours,
                },
                "folders_detail": folders_detail,
                "folders_total": len(folders_detail),
                "history": list(acct_state.get("history", [])),
                "last_abort_detail": acct_state.get("last_abort_detail", {}),
                "usage_24h": self._build_usage_block(account, acct_state),
                "lifetime": dict(acct_state.get("lifetime", {})),
                "paused": self._build_pause_block(account.name),
            })
        return stats

    def _build_pause_block(self, account_name: str) -> dict:
        """Manual-pause state for the addon ({} when not paused)."""
        is_paused, paused_until = self.is_account_paused(account_name)
        if not is_paused:
            return {}
        return {"until": paused_until}

    def _build_usage_block(self, account: ImapAccount, acct_state: dict) -> dict:
        """Per-account 24h IMAP usage block with provider-specific limit hints."""
        is_gmail = "gmail" in account.host.lower()
        usage = self._get_24h_usage(acct_state)
        # Gmail's actual limits aren't published; these are commonly observed thresholds
        # (~2.5 GB downloads/day, ~1 GB uploads/day, per-session command cap ~5000-7500).
        # Surfaced here as hints for the addon to draw progress bars against.
        usage["is_gmail"] = is_gmail
        if is_gmail:
            usage["gmail_bandwidth_limit"] = 2_500_000_000  # 2.5 GB / day, downloads
            usage["gmail_session_command_limit"] = 7500     # commands per session
        return usage

    def get_scheduler_info(self) -> dict:
        """Expose scheduler config + computed next-run time for the indexer status endpoint."""
        snap = self.status.snapshot()
        last_run = snap.get("last_run", "")
        # Prefer the backoff-aware override set by the scheduler loop (accurate
        # next-run including rate-limit backoff). Fall back to last_run + interval
        # for the case where the scheduler hasn't set it yet (first cycle).
        next_run = snap.get("next_run_at_override") or self._next_run_estimate(last_run)
        return {
            "interval_minutes": self.config.indexer.schedule_minutes,
            "cleanup_interval_hours": self.config.indexer.cleanup_interval_hours,
            "next_run_at": next_run,
            "backoff_multiplier": snap.get("backoff_multiplier", 1.0),
            "consecutive_rate_limit_aborts": snap.get("consecutive_rate_limit_aborts", 0),
        }
