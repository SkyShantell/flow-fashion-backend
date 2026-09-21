from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from backend.config import settings
from backend.db import init_db, session_scope
from backend.models import ProductJob, QueueTask, utcnow
from backend.tasks import claim_next_task, enqueue_missing_product_names, run_task_by_id
from backend.flow_account_affinity import install_flow_account_affinity
from backend.video_provider import install_video_provider_handlers
from backend.text_overlay import install_text_overlay_handler
from backend.manual_ffmpeg import install_manual_ffmpeg_handler
import backend.shoe_o1 as shoe_o1
from backend.shoe_o1 import install_shoe_o1_handlers
from backend.shoe_o1_prompt import shoe_o1_video_prompt

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
    install_flow_account_affinity()
    install_video_provider_handlers()
    install_text_overlay_handler()
    # Keep the established shoe product and visibility rules for Seedance.
    shoe_o1.shoe_o1_video_prompt = shoe_o1_video_prompt
    # Install Shoe Showcase after provider/text guards so only shoe generation is replaced.
    install_shoe_o1_handlers()
    # Install last: returned videos become visible immediately and FFmpeg text waits for
    # the user's manual button on both Fashion Try-On and Shoe Showcase jobs.
    install_manual_ffmpeg_handler()
    with session_scope() as db:
        resumed_names = db.query(QueueTask).filter(QueueTask.task_type == "repair_product_name", QueueTask.status == "running").update({"status": "queued", "locked_at": None, "run_after": utcnow()}, synchronize_session=False)
        queued_names = enqueue_missing_product_names(db)
        repair_states = {state: db.query(QueueTask).filter(QueueTask.task_type == "repair_product_name", QueueTask.status == state).count() for state in ("queued", "running", "done", "failed")}
        unknown_count = db.query(ProductJob).filter(ProductJob.product_name == "Unknown Product").count()
        failure_examples = [row[0] for row in db.query(QueueTask.error).filter(QueueTask.task_type == "repair_product_name", QueueTask.status == "failed").distinct().limit(3)]
    log.info("Product title repair status · unknown=%s · queued=%s · running=%s · done=%s · failed=%s", unknown_count, repair_states["queued"], repair_states["running"], repair_states["done"], repair_states["failed"])
    for failure in failure_examples:
        log.warning("Product title repair failure example: %s", str(failure or "")[:240])
    if resumed_names:
        log.info("Resumed %s interrupted product title lookups", resumed_names)
    if queued_names:
        log.info("Queued %s missing product titles for recovery", queued_names)
    cfg = settings()
    concurrency = max(1, int(cfg.worker_concurrency or 1))
    log.info(
        "Flow Phase 1 worker started · concurrency=%s · image=%s · Flow video=%s · fashion Kling=3.0/8s · shoes=Seedance 2.0 via Enhancor · final=%s · FFmpeg text=manual",
        concurrency,
        cfg.image_model,
        cfg.video_model,
        cfg.video_final_resolution,
    )
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
