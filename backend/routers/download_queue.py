"""Per-entry management routes for the local download_queue.

These endpoints only observe and mutate the local queue state. They never
connect to, start, or submit to qBittorrent, and never touch files or Jellyfin.
Business rules and sanitization live in DownloadService; this router stays thin.
"""

from typing import Optional

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/download/queue", tags=["download-queue"])


def _service():
    # Deferred import avoids a circular import: main imports this router at
    # startup, so we resolve the service only when a request is handled.
    from main import get_download_service

    return get_download_service()


@router.get("")
async def list_download_queue(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List download_queue rows with pagination, optional status filter and
    aggregate status counts. Read-only."""
    return await run_in_threadpool(_service().list_queue, status, limit, offset)


@router.post("/{task_id}/retry")
async def retry_download_task(task_id: str):
    """Force a waiting/failed task to be picked up on the next dispatch cycle."""
    return await run_in_threadpool(_service().retry_task, task_id)


@router.post("/{task_id}/recover")
async def recover_download_task(task_id: str):
    """Recover a failed/cancelled task back to pending."""
    return await run_in_threadpool(_service().recover_task, task_id)


@router.post("/{task_id}/cancel")
async def cancel_download_task(task_id: str):
    """Cancel a pending/waiting/failed task and free its dedup slot."""
    return await run_in_threadpool(_service().cancel_task, task_id)
