"""Tiny local smoke test. Run after starting the API:

python scripts/smoke_test.py http://localhost:8000 YOUR_PHASE1_API_KEY
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sys
import requests

base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
key = sys.argv[2] if len(sys.argv) > 2 else ""
headers = {"X-API-Key": key} if key else {}
print(requests.get(f"{base}/health", headers=headers, timeout=30).json())
