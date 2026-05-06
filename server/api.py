"""FastAPI application for Thunderbird AI Search."""

import asyncio
import logging
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)
    account: Optional[str] = None


class SearchResult(BaseModel):
    message_id: str
    subject: str
    from_: str = Field(alias="from")
    to: str
    date: str
    folder: str
    account: str
    snippet: str
    score: float

    model_config = {"populate_by_name": True}


class SearchResponse(BaseModel):
    results: list[dict]


def create_app(lifespan=None) -> FastAPI:
    """Create the FastAPI app. Dependencies are attached via app.state after creation."""

    def check_api_key(x_api_key: Optional[str] = Header(None)):
        # app.state is set before any request can arrive (attached in main.py)
        required_key = app.state.config.api.api_key
        if required_key and x_api_key != required_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    app = FastAPI(
        title="Thunderbird AI Search",
        version="1.0.0",
        lifespan=lifespan,
        dependencies=[Depends(check_api_key)],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/search")
    async def search(req: SearchRequest):
        embedder = app.state.embedder
        store = app.state.store

        vectors = await embedder.embed([req.query])
        if not vectors or vectors[0] is None:
            raise HTTPException(status_code=502, detail="Embedding service rejected the query")
        results = store.search(
            vector=vectors[0],
            limit=req.limit,
            account=req.account,
        )

        # Remove 'body' from results (only return snippet)
        for r in results:
            r.pop("body", None)
            r.pop("id", None)

        return {"results": results}

    @app.get("/health")
    async def health():
        store = app.state.store
        embedder = app.state.embedder

        qdrant_ok = store.is_healthy()
        ollama_ok = True
        try:
            await embedder.embed(["health check"])
        except Exception:
            ollama_ok = False

        return {
            "qdrant": "ok" if qdrant_ok else "error",
            "ollama": "ok" if ollama_ok else "error",
        }

    @app.get("/stats")
    async def stats():
        store = app.state.store
        indexer = app.state.indexer
        config = app.state.config

        total = store.count()
        status = indexer.status.snapshot()
        account_names = [a.name for a in config.accounts]

        return {
            "total_emails": total,
            "last_index_time": status["last_run"],
            "accounts": account_names,
        }

    @app.get("/accounts")
    async def accounts():
        indexer = app.state.indexer
        return {"accounts": indexer.get_account_stats()}

    @app.post("/reindex", status_code=202)
    async def reindex():
        indexer = app.state.indexer
        if indexer.status.running:
            raise HTTPException(status_code=409, detail="Indexer is already running")
        loop = asyncio.get_event_loop()
        indexer.start_background(loop)
        return {"status": "reindex started"}

    @app.post("/reindex/{account_name}", status_code=202)
    async def reindex_account(account_name: str):
        indexer = app.state.indexer
        config = app.state.config

        account_names = [a.name for a in config.accounts]
        if account_name not in account_names:
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

        if indexer.status.running:
            raise HTTPException(status_code=409, detail="Indexer is already running")

        loop = asyncio.get_event_loop()
        indexer.start_background(loop, account_name=account_name)
        return {"status": f"reindex started for '{account_name}'"}

    @app.post("/accounts/{account_name}/pause", status_code=200)
    async def pause_account(account_name: str, hours: float = 6.0):
        """Pause cycles for this account for N hours (default 6).

        The scheduled cycle loop will skip this account until the pause expires.
        A manual /reindex/{name} bypasses the pause — useful as an "I know what
        I'm doing, run anyway" override.
        """
        indexer = app.state.indexer
        if hours <= 0 or hours > 168:  # cap at 1 week to avoid forgotten pauses
            raise HTTPException(status_code=400, detail="hours must be between 0 and 168")
        try:
            until = indexer.pause_account(account_name, hours=hours)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "paused", "until": until, "hours": hours}

    @app.post("/accounts/{account_name}/resume", status_code=200)
    async def resume_account(account_name: str):
        indexer = app.state.indexer
        try:
            indexer.resume_account(account_name)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "resumed"}

    @app.get("/indexer/status")
    async def indexer_status():
        indexer = app.state.indexer
        snap = indexer.status.snapshot()
        snap["scheduler"] = indexer.get_scheduler_info()
        return snap

    return app
