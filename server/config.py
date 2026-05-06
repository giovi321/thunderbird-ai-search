"""Configuration loader for Thunderbird AI Search server."""

import os
import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ImapAccount(BaseModel):
    name: str
    host: str
    port: int = 993
    username: str
    password: str = ""
    use_ssl: bool = True
    folders: list[str] = Field(default_factory=list)


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    collection: str = "emails"


class ApiConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8342
    api_key: str = ""


class IndexerConfig(BaseModel):
    batch_size: int = 50
    max_body_chars: int = 4000
    schedule_minutes: int = 15
    cleanup_enabled: bool = True
    # IMAP rate-limiting. Tune these for strict providers like Gmail
    imap_fetch_batch: int = 25        # emails per FETCH command
    imap_folder_delay: float = 2.0    # seconds to wait between folders
    imap_batch_delay: float = 0.5     # seconds to wait between fetch batches
    cleanup_interval_hours: int = 24  # min hours between cleanup passes per account
    # Cap fetches per cycle to stay below provider rate limits (esp. Gmail during first sync).
    # 0 = unlimited (legacy behavior). When the cap is hit, the cycle ends gracefully with
    # outcome="cap_reached"; per-folder UID checkpoints resume on the next cycle.
    imap_max_fetches_per_cycle: int = 0
    # Adaptive backoff: when a cycle aborts with rate_limit, multiply the next sleep
    # by this factor. 1.0 = disabled (legacy: always wait `schedule_minutes`).
    # 2.0 = doubles each consecutive aborted cycle (15min, 30min, 60min, 2h, 4h…).
    # Resets to 1× as soon as a cycle completes cleanly.
    rate_limit_backoff_factor: float = 2.0
    # Hard ceiling on the backoff sleep. 240 = 4 hours.
    rate_limit_max_backoff_minutes: int = 240


class AppConfig(BaseModel):
    accounts: list[ImapAccount] = Field(default_factory=list)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    indexer: IndexerConfig = Field(default_factory=IndexerConfig)
    log_level: str = "INFO"


_ENV_MAP = {
    "TAIS_QDRANT_URL": ("qdrant", "url"),
    "TAIS_QDRANT_COLLECTION": ("qdrant", "collection"),
    "TAIS_OLLAMA_BASE_URL": ("ollama", "base_url"),
    "TAIS_OLLAMA_MODEL": ("ollama", "model"),
    "TAIS_API_HOST": ("api", "host"),
    "TAIS_API_PORT": ("api", "port"),
    "TAIS_API_API_KEY": ("api", "api_key"),
    "TAIS_INDEXER_BATCH_SIZE": ("indexer", "batch_size"),
    "TAIS_INDEXER_MAX_BODY_CHARS": ("indexer", "max_body_chars"),
    "TAIS_INDEXER_SCHEDULE_MINUTES": ("indexer", "schedule_minutes"),
    "TAIS_INDEXER_CLEANUP_ENABLED": ("indexer", "cleanup_enabled"),
    "TAIS_INDEXER_IMAP_FETCH_BATCH": ("indexer", "imap_fetch_batch"),
    "TAIS_INDEXER_IMAP_FOLDER_DELAY": ("indexer", "imap_folder_delay"),
    "TAIS_INDEXER_IMAP_BATCH_DELAY": ("indexer", "imap_batch_delay"),
    "TAIS_INDEXER_CLEANUP_INTERVAL_HOURS": ("indexer", "cleanup_interval_hours"),
    "TAIS_INDEXER_IMAP_MAX_FETCHES_PER_CYCLE": ("indexer", "imap_max_fetches_per_cycle"),
    "TAIS_LOG_LEVEL": ("log_level",),
}


def _apply_env_overrides(config_dict: dict) -> dict:
    """Apply environment variable overrides with TAIS_ prefix."""
    # Explicit mappings for known keys
    for env_key, path in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        d = config_dict
        for part in path[:-1]:
            if part not in d:
                d[part] = {}
            d = d[part]
        d[path[-1]] = val

    # Dynamic: TAIS_ACCOUNTS_<idx>_<field> (e.g. TAIS_ACCOUNTS_0_PASSWORD)
    prefix = "TAIS_ACCOUNTS_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        parts = rest.lower().split("_", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        idx = int(parts[0])
        field = parts[1]
        accounts = config_dict.setdefault("accounts", [])
        while len(accounts) <= idx:
            accounts.append({})
        accounts[idx][field] = value

    return config_dict


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file with env var overrides."""
    config_path = Path(path) if path else Path("config.yaml")

    config_dict = {}
    if config_path.exists():
        with open(config_path) as f:
            config_dict = yaml.safe_load(f) or {}
        logger.info("Loaded config from %s", config_path)
    else:
        logger.warning("Config file %s not found, using defaults", config_path)

    config_dict = _apply_env_overrides(config_dict)
    return AppConfig(**config_dict)
