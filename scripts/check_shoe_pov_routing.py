#!/usr/bin/env python3
"""Zero-credit checks for the Shoe Showcase provider switch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend.shoe_o1 as shoe_o1
from backend.models import Batch, ProductJob


class FakeDb:
    def __init__(self, job, batch):
        self.job = job
        self.batch = batch

    def get(self, model, value):
        if model is ProductJob and value == self.job.id:
            return self.job
        if model is Batch and value == self.batch.id:
            return self.batch
        return None


def main() -> None:
    legacy = SimpleNamespace(mode="shoe_showcase", video_provider="omni", shoe_pov_format="")
    seedance = SimpleNamespace(mode="shoe_showcase", video_provider="enhancor", shoe_pov_format="held")
    pov = SimpleNamespace(mode="shoe_showcase", video_provider="shoe_pov", shoe_pov_format="worn")
    fashion = SimpleNamespace(mode="fashion_tryon", video_provider="omni", shoe_pov_format="held")

    assert shoe_o1.shoe_provider(legacy) == "enhancor"
    assert shoe_o1.shoe_provider(seedance) == "enhancor"
    assert shoe_o1.shoe_provider(pov) == "shoe_pov"
    assert shoe_o1.shoe_provider(fashion) == ""
    assert shoe_o1._pov_format(pov) == "worn"
    assert shoe_o1._pov_format(seedance) == "held"

    calls: list[str] = []
    original_pov = shoe_o1._run_submit_shoe_pov
    original_seedance = shoe_o1._run_submit_shoe_o1
    try:
        shoe_o1._run_submit_shoe_pov = lambda db, task, job, batch: calls.append("pov")
        shoe_o1._run_submit_shoe_o1 = lambda db, task, job, batch: calls.append("seedance")

        job = SimpleNamespace(id="job-1", batch_id="batch-1")
        task = SimpleNamespace(job_id="job-1")
        shoe_o1._dispatch_submit_video(FakeDb(job, SimpleNamespace(id="batch-1", mode="shoe_showcase", video_provider="shoe_pov")), task)
        shoe_o1._dispatch_submit_video(FakeDb(job, SimpleNamespace(id="batch-1", mode="shoe_showcase", video_provider="enhancor")), task)
    finally:
        shoe_o1._run_submit_shoe_pov = original_pov
        shoe_o1._run_submit_shoe_o1 = original_seedance

    assert calls == ["pov", "seedance"]
    print("PASS: legacy Shoe Showcase batches stay on Seedance")
    print("PASS: explicit shoe_pov batches route only to Flow Shoes POV")
    print("PASS: held/worn format selection is deterministic")
    print("PASS: fake database only — zero network calls and zero credits spent")


if __name__ == "__main__":
    main()
