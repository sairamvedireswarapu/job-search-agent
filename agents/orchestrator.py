"""Orchestrator — builds query matrix, fans out fetches, coordinates all agents."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from agents.fetch_jsearch import fetch_jsearch
from agents.fetch_adzuna import fetch_adzuna
from agents.dedup_filter import dedup_and_filter
from agents.scorer import score_jobs
from agents.exporter import build_excel, upload_to_gcs
from config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def run_job_search(
    user_id: str,
    cv_text: str,
    keywords: list[str],
    countries: list[str],
    api_keys: dict,             # {jsearch_key, adzuna_app_id, adzuna_api_key, anthropic_key}
    model: str = "haiku",
    job_types: Optional[list[str]] = None,
    work_modes: Optional[list[str]] = None,
    posted_within_days: int = 7,
    min_score: int = 40,
    max_results: int = 20,
    on_status: Optional[callable] = None,  # async callback(status: str)
) -> dict:
    """
    Full pipeline run. Returns run summary dict.
    on_status is called at each pipeline stage so the caller can update Firestore.
    """
    run_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now(tz=timezone.utc).isoformat()

    async def _status(s: str):
        logger.info("[run %s] %s", run_id, s)
        if on_status:
            await on_status(s)

    # ── 1. Build query matrix ──────────────────────────────────────────────
    await _status("fetching")
    apis_used = []
    fetch_tasks = []

    jsearch_key = api_keys.get("jsearch_key")
    adzuna_app_id = api_keys.get("adzuna_app_id")
    adzuna_api_key = api_keys.get("adzuna_api_key")

    for keyword in keywords:
        for country in countries:
            if jsearch_key:
                apis_used.append("JSearch")
                fetch_tasks.append(fetch_jsearch(
                    keyword=keyword,
                    country=country,
                    api_key=jsearch_key,
                    max_results=max_results,
                    job_type=job_types[0].upper().replace("-", "") if job_types else None,
                    work_mode=work_modes[0].lower() if work_modes else None,
                    posted_within_days=posted_within_days,
                ))
            if adzuna_app_id and adzuna_api_key:
                apis_used.append("Adzuna")
                fetch_tasks.append(fetch_adzuna(
                    keyword=keyword,
                    country=country,
                    app_id=adzuna_app_id,
                    api_key=adzuna_api_key,
                    max_results=max_results,
                    job_type=job_types[0] if job_types else None,
                    work_mode=work_modes[0].lower() if work_modes else None,
                    posted_within_days=posted_within_days,
                ))

    # ── 2. Fan out fetches concurrently ───────────────────────────────────
    logger.info("[run %s] %d fetch tasks", run_id, len(fetch_tasks))
    fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    all_jobs: list[dict] = []
    for result in fetch_results:
        if isinstance(result, Exception):
            logger.error("Fetch error: %s", result)
        else:
            all_jobs.extend(result)

    total_raw = len(all_jobs)
    logger.info("[run %s] total raw jobs: %d", run_id, total_raw)

    # ── 3. Dedup + filter ─────────────────────────────────────────────────
    await _status("deduplicating")
    clean_jobs, dupe_count = dedup_and_filter(
        all_jobs,
        job_types=job_types,
        work_modes=work_modes,
        posted_within_days=posted_within_days,
    )
    after_dedup = len(clean_jobs)

# ── 4. Gemini scoring ─────────────────────────────────────────────────
    await _status("scoring")
    gemini_key = api_keys.get("gemini_key", "")
    scored_jobs, dropped_by_score = await score_jobs(
        jobs=clean_jobs,
        cv_text=cv_text,
        keywords=keywords,
        gemini_key=api_keys.get("gemini_key", ""),
        groq_key=api_keys.get("groq_key", ""),
        anthropic_key=api_keys.get("anthropic_key", ""),
        model=model,
        min_score=min_score,
    )
    after_score_filter = len(scored_jobs)
        # ── 5. Build run meta ─────────────────────────────────────────────────
    est_cost = _estimate_cost(len(clean_jobs), model)
    run_meta = {
        "run_id": run_id,
        "user_id": user_id,
        "timestamp": timestamp,
        "keywords": keywords,
        "countries": countries,
        "apis_used": list(set(apis_used)),
        "model_used": model,
        "total_raw_jobs": total_raw,
        "after_dedup": after_dedup,
        "after_score_filter": after_score_filter,
        "min_score": min_score,
        "max_results": max_results,
        "job_types": job_types,
        "work_modes": work_modes,
        "posted_within_days": posted_within_days,
        "est_token_cost": est_cost,
    }

    # ── 6. Export + upload ────────────────────────────────────────────────
    await _status("exporting")
    excel_bytes = build_excel(scored_jobs, run_meta)
    gcs_path, signed_url = upload_to_gcs(excel_bytes, run_meta)

    run_meta["gcs_path"] = gcs_path
    run_meta["signed_url"] = signed_url

    await _status("done")
    return run_meta


def _estimate_cost(num_jobs: int, model: str) -> str:
    """Very rough token cost estimate."""
    # ~430 tokens/job input + 80 tokens output
    input_tokens = num_jobs * 430
    output_tokens = num_jobs * 80
    if model == "sonnet":
        cost = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)
    else:
        cost = (input_tokens / 1_000_000 * 0.25) + (output_tokens / 1_000_000 * 1.25)
    return f"~${cost:.4f}"
