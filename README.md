# Flow Try-On Factory — Phase 1 Backend Workers

This is the Phase 1 migration away from a single Streamlit app doing everything.

The goal is speed and reliability for 40–50+ videos/day:

- Vercel/Next.js can become the UI later.
- Railway runs the API and background worker.
- Postgres stores batches, product jobs, task state, retries, media IDs, Drive links, and Sheet rows.
- useapi still runs Google Flow image/video/upscale.
- SociaVault still imports TikTok Shop product details/photos.
- Google Drive + Google Sheets still archive and track output.

## What changed vs Streamlit

The old Streamlit app handled UI, importing, generating, polling, upscaling, Drive archiving, and Sheet updates inside reruns. This backend separates that work:

```text
UI or Scanner Queue
    ↓
FastAPI backend
    ↓
Postgres task queue
    ↓
Railway worker(s)
    ↓
SociaVault → useapi images → useapi Omni video → useapi upscale → Drive → Sheets
```

The browser can be closed. The worker keeps running.

## Services to create on Railway

Create one Railway project with:

1. **Postgres** plugin/service.
2. **API service** from this repo.
3. **Worker service** from the same repo.

Use the same environment variables on both API and Worker services.

### API start command

```bash
uvicorn backend.api:app --host 0.0.0.0 --port $PORT
```

### Worker start command

```bash
python -m backend.worker
```

Start with one worker service and `WORKER_CONCURRENCY=4`. Once stable, scale to more replicas or raise concurrency.

## Required environment variables

Copy `.env.example` and fill these:

```bash
USEAPI_TOKEN=
SOCIAVAULT_API_KEY=
GOOGLE_SERVICE_ACCOUNT_JSON=
GOOGLE_SHEET_URL=
GOOGLE_DRIVE_ARCHIVE_WEBHOOK_URL=
GOOGLE_DRIVE_ARCHIVE_SECRET=
PHASE1_API_KEY=
```

Optional but recommended:

```bash
GOOGLE_FLOW_EMAIL=
WORKER_CONCURRENCY=4
IMAGE_MODEL=nano-banana-pro
VIDEO_MODEL=omni-flash
VIDEO_NATIVE_RESOLUTION=720p
VIDEO_FINAL_RESOLUTION=1080p
```

## API quick flow

### 1. Health check

```bash
curl https://YOUR-API.up.railway.app/health
```

### 2. Create a batch

Send `avatar_b64` as a base64 image string. The future Vercel UI will do this automatically.

```bash
curl -X POST https://YOUR-API.up.railway.app/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_PHASE1_API_KEY' \
  -d '{
    "name":"Today try-ons",
    "scene":"Modern apartment mirror",
    "creator_profile":"Male",
    "video_style":"Calm",
    "auto_approve":false,
    "avatar_b64":"BASE64_IMAGE_HERE",
    "avatar_mime":"image/jpeg"
  }'
```

### 3. Add product links manually

```bash
curl -X POST https://YOUR-API.up.railway.app/batches/BATCH_ID/products \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_PHASE1_API_KEY' \
  -d '{"links":["https://www.tiktok.com/shop/pdp/..."], "start_generation":true}'
```

### 4. Pull from the Creator Scanner queue

First check what is waiting:

```bash
curl -H 'X-API-Key: YOUR_PHASE1_API_KEY' \
  https://YOUR-API.up.railway.app/scanner/pending
```

Then import the top pending rows:

```bash
curl -X POST https://YOUR-API.up.railway.app/batches/BATCH_ID/scanner/import \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_PHASE1_API_KEY' \
  -d '{"max_items":10, "start_generation":true}'
```

The backend marks Scanner Queue rows as `Importing` in columns J:L. When product import starts, the worker pulls the full details/photos with SociaVault.

### 5. Approve an image and start video

```bash
curl -X POST https://YOUR-API.up.railway.app/jobs/JOB_ID/approve \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_PHASE1_API_KEY' \
  -d '{"approved":true, "start_video":true}'
```

The worker then:

1. submits Omni 1.1 Flash through useapi,
2. polls the job,
3. submits the 1080p upscale through useapi,
4. polls the upscale,
5. archives final media to Drive,
6. syncs the tracker Sheet.

## Important defaults

```bash
IMAGE_MODEL=nano-banana-pro
VIDEO_MODEL=omni-flash
VIDEO_NATIVE_RESOLUTION=720p
VIDEO_FINAL_RESOLUTION=1080p
```

For cost testing later, you can try:

```bash
VIDEO_NATIVE_RESOLUTION=360p
VIDEO_FINAL_RESOLUTION=1080p
```

Do that only after checking output quality.

## Worker tuning for 40–50/day

Recommended starting point:

```bash
WORKER_CONCURRENCY=4
POLL_SECONDS=15
TASK_BACKOFF_SECONDS=45
```

Then test:

- If useapi starts rate-limiting: lower concurrency.
- If jobs are stable but queue waits too long: raise concurrency to 5–6 or add a second worker replica.
- If Drive uploads are slow: keep generation workers separate from archive workers in Phase 1B.

## Phase 1A limitations

This is the backend conversion starter. It is intentionally not the final Vercel UI.

Still to add in Phase 1B / Phase 2:

- polished Next.js dashboard,
- visual image approval grid,
- realtime websocket progress,
- separate queues for images/videos/upscales/archive,
- advanced rate-limit governor per provider,
- batch-level Drive folders for reference images,
- admin/VA role controls.

## Local dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/create_tables.py
uvicorn backend.api:app --reload --port 8000
```

In another terminal:

```bash
python -m backend.worker
```

Open API docs:

```text
http://localhost:8000/docs
```
