from __future__ import annotations

import asyncio
import logging
import signal

from .bootstrap import initialize_database
from .collector import task_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """Run durable collection and AI queues independently from the web process."""
    initialize_database()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args) -> None:
        loop.call_soon_threadsafe(stopped.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except (OSError, ValueError):
            pass

    await task_worker.start(force=True)
    logger.info("后台执行器已启动")
    try:
        await stopped.wait()
    finally:
        logger.info("后台执行器正在停止")
        await task_worker.stop()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
