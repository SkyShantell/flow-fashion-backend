from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from backend.config import settings
from backend.db import init_db, session_scope
from backend.tasks import claim_next_task, run_task_by_id

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flow-worker")
stop = False


def _handle_stop(signum, frame):
    global stop
    stop = True
    log.info("Stop requested (%s). Finishing in-flight tasks…", signum)


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def _claim_task_id() -> str | None:
    with session_scope() as db:
        task = claim_next_task(db)
        return task.id if task else None


def main():
    init_db()
    cfg = settings()
    concurrency = max(1, int(cfg.worker_concurrency or 1))
    log.info("Flow Phase 1 worker started · concurrency=%s · image=%s · video=%s · final=%s", concurrency, cfg.image_model, cfg.video_model, cfg.video_final_resolution)
    in_flight = set()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        while not stop:
            while len(in_flight) < concurrency:
                task_id = _claim_task_id()
                if not task_id:
                    break
                log.info("Claimed task %s", task_id)
                in_flight.add(ex.submit(run_task_by_id, task_id))

            if not in_flight:
                time.sleep(2)
                continue

            done, in_flight = wait(in_flight, timeout=2, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    result = fut.result()
                    log.info("Task finished: %s", result)
                except Exception:
                    log.exception("Worker task crashed outside task handler")

        if in_flight:
            wait(in_flight)


if __name__ == "__main__":
    main()
