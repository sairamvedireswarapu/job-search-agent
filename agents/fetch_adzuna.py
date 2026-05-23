"""Fetch agent — Adzuna API."""
import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country_code}/search/{page}"
MAX_RETRIES = 2

# Adzuna uses 2-letter country codes
COUNTRY_MAP: dict[str, str] = {
    "india": "in",
    "uk": "gb",
    "united kingdom": "gb",
    "germany": "de",
    "united states": "us",
    "usa": "us",
    "australia": "au",
    "canada": "ca",
    "singapore": "sg",
    "remote": "gb",   # Adzuna doesn't have a "remote" country — default to gb + remote flag
}


def _get_country_code(country: str) -> str:
    return COUNTRY_MAP.get(country.lower(), "gb")


async def fetch_adzuna(
    keyword: str,
    country: str,
    app_id: str,
    api_key: str,
    max_results: int = 20,
    job_type: Optional[str] = None,
    work_mode: Optional[str] = None,
    posted_within_days: int = 7,
) -> list[dict]:
    """
    Fetch jobs from Adzuna for one keyword × country combination.
    Returns a list of normalised job dicts.
    """
    country_code = _get_country_code(country)
    pages = max(1, max_results // 10)
    jobs: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, pages + 1):
            params = {
                "app_id": app_id,
                "app_key": api_key,
                "results_per_page": 10,
                "what": keyword,
                "max_days_old": posted_within_days,
                "content-type": "application/json",
            }
            if work_mode == "remote":
                params["what_and"] = "remote"

            url = ADZUNA_URL.format(country_code=country_code, page=page)

            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        logger.warning("Adzuna rate limit — backing off")
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error("Adzuna HTTP error (page %d): %s", page, e)
                        data = {}
                        break
                except Exception as e:
                    logger.error("Adzuna fetch error: %s", e)
                    data = {}
                    break

            for raw in data.get("results", []):
                jd_text = raw.get("description") or ""
                job_url = raw.get("redirect_url") or ""
                jobs.append({
                    "title": (raw.get("title") or "").strip(),
                    "company": (raw.get("company", {}).get("display_name") or "").strip(),
                    "location": (raw.get("location", {}).get("display_name") or "").strip(),
                    "country": country,
                    "salary": _build_salary(raw),
                    "job_type": _infer_job_type(raw),
                    "source": "Adzuna",
                    "posted_date": (raw.get("created") or "")[:10],
                    "url": job_url,
                    "jd_full_text": jd_text,
                })

    logger.info("Adzuna: fetched %d jobs for '%s' / %s", len(jobs), keyword, country)
    return jobs[:max_results]


def _build_salary(raw: dict) -> str:
    min_s = raw.get("salary_min")
    max_s = raw.get("salary_max")
    if min_s and max_s:
        return f"{int(min_s):,}–{int(max_s):,}"
    elif min_s or max_s:
        return str(int(min_s or max_s))
    return ""


def _infer_job_type(raw: dict) -> str:
    contract_type = raw.get("contract_type") or ""
    contract_time = raw.get("contract_time") or ""
    mapping = {
        "permanent": "Full-time",
        "contract": "Contract",
        "part_time": "Part-time",
        "full_time": "Full-time",
    }
    return mapping.get(contract_time.lower(), mapping.get(contract_type.lower(), ""))
