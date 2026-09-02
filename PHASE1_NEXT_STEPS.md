# Next steps

## Step 1 — Deploy API service

Deploy this repo to Railway as the API service.

Start command:

```bash
uvicorn backend.api:app --host 0.0.0.0 --port $PORT
```

Add Railway Postgres and make sure `DATABASE_URL` appears in the API service variables.

## Step 2 — Deploy Worker service

Create another Railway service using the same repo.

Start command:

```bash
python -m backend.worker
```

Connect it to the same Postgres `DATABASE_URL` and copy the same env vars.

## Step 3 — Test health

Open:

```text
https://YOUR-API.up.railway.app/health
```

You should see:

```json
{
  "ok": true,
  "useapi": true,
  "sociavault": true,
  "google_sheet": true,
  "drive_archive": true,
  "image_model": "nano-banana-pro",
  "video_model": "omni-flash"
}
```

## Step 4 — Connect old Streamlit or build Vercel UI

For the first test, use the API docs page at `/docs`.

After it works, Phase 2 is the Vercel/Next.js dashboard.
