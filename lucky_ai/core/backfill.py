from typing import Dict, Any
import time


def backfill_task_key(guild_id: int, channel_id: int) -> str:
    return f"{guild_id}:{channel_id}"


def format_backfill_status(channel_id: int, progress: Dict[str, Any], running: bool) -> str:
    started_at = progress.get("started_at")
    if not isinstance(started_at, (int, float)):
        started_at = time.time()
    elapsed = int(max(0, time.time() - float(started_at)))
    return (
        f"Backfill `{channel_id}` status: **{progress.get('status', 'unknown')}**\n"
        f"Processed: **{progress.get('processed', 0)}** | Synced: **{progress.get('synced', 0)}**\n"
        f"Elapsed: **{elapsed}s** | Running: **{'yes' if running else 'no'}**"
    )
