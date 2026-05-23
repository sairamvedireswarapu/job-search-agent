# Job Search Agent

Agentic job search tool: searches JSearch + Adzuna in parallel, scores every listing against your CV using Claude, exports results to Excel. Hosted on Cloud Run.

## Stack
FastAPI · Google Cloud Run · Firestore · GCS · KMS · Google OAuth · Claude (Haiku / Sonnet)

## Local Dev

```bash
cp .env.example .env
# Fill in .env

pip install -e ".[dev]"
uvicorn main:app --reload
# Open http://localhost:8080
```

## GCP Setup (one-time)

```bash
# 1. Create GCS bucket
gsutil mb gs://job-search-agent-bucket

# 2. Create Firestore database (Native mode) in GCP console

# 3. KMS keyring + key
gcloud kms keyrings create job-search --location=global
gcloud kms keys create api-keys \
  --keyring=job-search --location=global --purpose=encryption

# 4. Workload Identity for GitHub Actions
# See: https://cloud.google.com/blog/products/identity-security/enabling-keyless-authentication-from-github-actions

# 5. OAuth 2.0 credentials in GCP console → APIs & Services → Credentials
#    Add authorized redirect URI: https://<your-cloud-run-url>/auth/google/callback
```

## GitHub Secrets required
See `deploy.yml` for the full list.

## Project Structure
```
agents/
  orchestrator.py   — fan-out, asyncio coordination
  fetch_jsearch.py  — JSearch API
  fetch_adzuna.py   — Adzuna API
  dedup_filter.py   — merge, dedup, filter
  scorer.py         — Claude batched scoring
  exporter.py       — Excel + GCS upload
auth/
  google_oauth.py   — Google OAuth + session management
api.py              — FastAPI routes
main.py             — entrypoint
index.html          — single-page UI
```

## Cost estimates
- Haiku: ~$0.003 / run (60 jobs)
- Sonnet: ~$0.08 / run (60 jobs)
- 10 users × daily on Sonnet ≈ $24/month
