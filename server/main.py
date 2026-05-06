"""Entry point for Thunderbird AI Search server."""

import asyncio
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager

import uvicorn

from server.api import create_app
from server.config import load_config
from server.embeddings import OllamaEmbedding
from server.indexer import EmailIndexer
from server.vector_store import VectorStore

logger = logging.getLogger("server")

CONFIG_PATH = "/app/config.yaml"


def main():
    config = load_config(CONFIG_PATH)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if not config.accounts:
        logger.error("No IMAP accounts configured. Add at least one account to config.yaml")
        sys.exit(1)

    embedder = OllamaEmbedding(
        base_url=config.ollama.base_url,
        model=config.ollama.model,
    )
    store = VectorStore(
        url=config.qdrant.url,
        collection=config.qdrant.collection,
    )

    if not store.is_healthy():
        logger.error("Cannot connect to Qdrant at %s", config.qdrant.url)
        sys.exit(1)
    logger.info("Qdrant connection OK")

    store.ensure_collection(dimensions=embedder.dimensions)

    indexer = EmailIndexer(config, embedder, store)

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_event_loop()
        try:
            await embedder.embed(["startup check"])
            logger.info("Ollama connection OK (%s)", config.ollama.model)
        except Exception as e:
            logger.error("Cannot connect to Ollama at %s: %s", config.ollama.base_url, e)
            logger.warning("Server will start but indexing will fail until Ollama is available")

        thread = threading.Thread(target=_schedule_loop, args=(loop,), daemon=True)
        thread.start()

        yield

        await embedder.close()

    app = create_app(lifespan=lifespan)
    app.state.config = config
    app.state.embedder = embedder
    app.state.store = store
    app.state.indexer = indexer

    def _schedule_loop(loop: asyncio.AbstractEventLoop):
        # Adaptive backoff state — persisted to state.json so a docker restart
        # doesn't reset us to 0 and have us hammer Gmail at base cadence while
        # cycles are still failing. (Cleared as soon as a cycle completes cleanly.)
        sched_state = indexer.get_scheduler_persisted_state()
        consecutive_rate_aborts = int(sched_state.get("consecutive_rate_limit_aborts", 0))
        if consecutive_rate_aborts > 0:
            logger.info(
                "Restored adaptive-backoff state from state.json: "
                "%d consecutive rate-limited cycles",
                consecutive_rate_aborts,
            )
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        while True:
            try:
                indexer.run_full_index(loop)
            except Exception as e:
                logger.error("Indexer run failed: %s", e)
            if config.indexer.schedule_minutes <= 0:
                break

            # Adaptive-backoff: if the last cycle hit rate_limit, sleep longer
            # before the next attempt. Reset to 1× as soon as a cycle completes
            # cleanly. Capped at rate_limit_max_backoff_minutes.
            base = config.indexer.schedule_minutes
            factor = config.indexer.rate_limit_backoff_factor
            max_min = config.indexer.rate_limit_max_backoff_minutes

            if indexer.last_cycle_was_rate_limited() and factor > 1.0:
                consecutive_rate_aborts += 1
                multiplier = factor ** consecutive_rate_aborts
                sleep_min = min(base * multiplier, max_min)
                logger.warning(
                    "Rate-limited %d cycle(s) in a row — backing off, next cycle in %.1f min (%.1fx base)",
                    consecutive_rate_aborts, sleep_min, sleep_min / base,
                )
            else:
                if consecutive_rate_aborts > 0:
                    logger.info("Cycle completed cleanly — resetting rate-limit backoff to 1x")
                consecutive_rate_aborts = 0
                multiplier = 1.0
                sleep_min = base

            # Surface backoff state on /indexer/status so the dashboard can
            # show "Next run: X (backoff Yx after Z aborts)".
            next_run = (_dt.now(_tz.utc) + _td(minutes=sleep_min)).isoformat()
            indexer.status.set_backoff_state(multiplier, consecutive_rate_aborts, next_run)
            # Persist so backoff survives container restarts
            indexer.save_scheduler_persisted_state({
                "consecutive_rate_limit_aborts": consecutive_rate_aborts,
            })

            time.sleep(sleep_min * 60)

    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_level=config.log_level.lower(),
    )


main()
