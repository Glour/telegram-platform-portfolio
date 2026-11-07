"""Purpose-limited worker. Never changes campaign or general execution gates."""

import argparse
import asyncio
import logging

import app.db.import_models  # noqa: F401
from app.core.config import get_settings
from app.modules.telegram_safety.appeals import process_pending_appeals


async def run(workspace_id: str, loop: bool):
    from redis.asyncio import Redis

    key = f"spambot-appeals:worker:{workspace_id}"
    async with Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2) as redis:
        try:
            while True:
                try:
                    await redis.set(key, "ready", ex=120)
                    await process_pending_appeals(workspace_id)
                except Exception:
                    logging.exception("SpamBot appeal worker cycle failed")
                if not loop:
                    return
                await asyncio.sleep(2)
        finally:
            await redis.delete(key)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if not get_settings().spambot_appeals_enabled:
        raise SystemExit("SpamBot appeals feature is disabled; no actions performed.")
    asyncio.run(run(args.workspace_id, args.loop))


if __name__ == "__main__":
    main()
