"""Fetch agent — JSearch via RapidAPI."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
MAX_RETRIES = 2


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Try to normalise various date strings to ISO 8601."""
    if not raw:
        return None
    try:
        # JSearch returns Unix timestamps
        ts = int(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(raw)[:10]


async def _enrich_jd(url: str, client: httpx.AsyncClient) -> str:
    """Scrape the job page to get the full JD if the API returned <200 chars."""
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
        # Very naive extraction — get the biggest <p> block cluster
        text = resp.text
        # Strip HTML tags simply
        import re
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[:8000]  # cap to ~2k tokens
    except Exception as e:
        logger.warning("JD enrichment failed for %s: %s", url, e)
        return ""


async def fetch_jsearch(
    keyword: str,
    country: str,
    api_key: str,
    max_results: int = 20,
    job_type: Optional[str] = None,         # "FULLTIME" | "PARTTIME" | "CONTRACTOR"
    work_mode: Optional[str] = None,         # "remote" | "hybrid" | "onsite"
    posted_within_days: int = 7,
) -> list[dict]:
    """
    Fetch jobs from JSearch for one keyword × country combination.
    Returns a list of normalised job dicts.
    """
    params = {
        "query": f"{keyword} {country}",
        "page": "1",
        "num_pages": str(max(1, max_results // 10)),
        "date_posted": _days_to_jsearch_filter(posted_within_days),
    }
    if job_type:
        params["employment_types"] = job_type
    if work_mode == "remote":
        params["remote_jobs_only"] = "true"

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    jobs: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.get(JSEARCH_URL, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("JSearch rate limit hit — backing off")
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error("JSearch HTTP error: %s", e)
                    return []
            except Exception as e:
                logger.error("JSearch fetch error: %s", e)
                return []
        else:
            return []

        raw_jobs = data.get("data", [])[:max_results]
        enrich_tasks = []

        for raw in raw_jobs:
            jd_text = raw.get("job_description") or ""
            url = raw.get("job_apply_link") or raw.get("job_google_link") or ""
            needs_enrichment = len(jd_text) < 200 and url

            job = {
                "title": raw.get("job_title", "").strip(),
                "company": raw.get("employer_name", "").strip(),
                "location": _build_location(raw),
                "country": country,
                "salary": _build_salary(raw),
                "job_type": _normalise_job_type(raw.get("job_employment_type")),
                "source": "JSearch",
                "posted_date": _parse_date(str(raw.get("job_posted_at_timestamp", ""))),
                "url": url,
                "jd_full_text": jd_text,
                "_needs_enrichment": needs_enrichment,
            }
            jobs.append(job)
            if needs_enrichment:
                enrich_tasks.append((len(jobs) - 1, url))

        # Enrich truncated JDs concurrently
        if enrich_tasks:
            async with httpx.AsyncClient(timeout=15) as eclient:
                results = await asyncio.gather(
                    *[_enrich_jd(url, eclient) for _, url in enrich_tasks],
                    return_exceptions=True,
                )
                for (idx, _), enriched in zip(enrich_tasks, results):
                    if isinstance(enriched, str) and enriched:
                        jobs[idx]["jd_full_text"] = enriched

        # Clean up internal flag
        for job in jobs:
            job.pop("_needs_enrichment", None)

    logger.info("JSearch: fetched %d jobs for '%s' / %s", len(jobs), keyword, country)
    return jobs


def _build_location(raw: dict) -> str:
    parts = [raw.get("job_city"), raw.get("job_state"), raw.get("job_country")]
    return ", ".join(p for p in parts if p)


def _build_salary(raw: dict) -> str:
    min_s = raw.get("job_min_salary")
    max_s = raw.get("job_max_salary")
    currency = raw.get("job_salary_currency", "")
    period = raw.get("job_salary_period", "")
    if min_s and max_s:
        return f"{currency} {min_s}–{max_s} / {period}".strip()
    elif min_s or max_s:
        return f"{currency} {min_s or max_s} / {period}".strip()
    return ""


def _normalise_job_type(raw: Optional[str]) -> str:
    mapping = {
        "FULLTIME": "Full-time",
        "PARTTIME": "Part-time",
        "CONTRACTOR": "Contract",
        "INTERN": "Internship",
    }
    return mapping.get((raw or "").upper(), raw or "")


def _days_to_jsearch_filter(days: int) -> str:
    if days <= 1:
        return "today"
    elif days <= 3:
        return "3days"
    elif days <= 7:
        return "week"
    return "month"
