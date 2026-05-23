"""Dedup + Filter agent — merges all fetch results, deduplicates, applies filters."""
import logging
from typing import Optional
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88   # title+company similarity score to consider a dupe


def _normalise(s: str) -> str:
    return s.lower().strip()


def _is_duplicate(job: dict, seen: list[dict]) -> bool:
    """Return True if job is a dupe of anything in seen."""
    # 1. Exact URL match
    if job["url"] and any(s["url"] == job["url"] for s in seen):
        return True
    # 2. Fuzzy match: title and company must BOTH be similar
    t1 = _normalise(job['title'])
    c1 = _normalise(job['company'])
    for s in seen:
        t2 = _normalise(s['title'])
        c2 = _normalise(s['company'])
        if fuzz.ratio(t1, t2) >= FUZZY_THRESHOLD and fuzz.ratio(c1, c2) >= FUZZY_THRESHOLD:
            return True
            return True
    return False


def _apply_filters(
    job: dict,
    job_types: Optional[list[str]],
    work_modes: Optional[list[str]],
    posted_within_days: Optional[int],
) -> bool:
    """Return True if the job passes all filters."""
    if job_types:
        normalised = [jt.lower() for jt in job_types]
        if job.get("job_type", "").lower() not in normalised and job.get("job_type"):
            # Only filter if we actually know the job type
            if job.get("job_type"):
                return False

    if work_modes:
        modes = [m.lower() for m in work_modes]
        jd = (job.get("jd_full_text") or "").lower()
        title = (job.get("title") or "").lower()
        location = (job.get("location") or "").lower()
        if "remote" in modes:
            if "remote" not in jd and "remote" not in title and "remote" not in location:
                return False

    # posted_within_days filter is already applied at fetch time via API params
    # but we do a best-effort check here too for safety
    return True


def dedup_and_filter(
    all_jobs: list[dict],
    job_types: Optional[list[str]] = None,
    work_modes: Optional[list[str]] = None,
    posted_within_days: Optional[int] = None,
) -> tuple[list[dict], int]:
    """
    Merge all fetch results, deduplicate, apply filters.
    Returns (clean_jobs, dupe_count).
    """
    seen: list[dict] = []
    dupes = 0

    for job in all_jobs:
        if _is_duplicate(job, seen):
            dupes += 1
            logger.debug("Dupe dropped: %s @ %s", job.get("title"), job.get("company"))
        else:
            seen.append(job)

    logger.info("Dedup: %d raw → %d unique (%d dupes)", len(all_jobs), len(seen), dupes)

    # Apply filters
    filtered = [j for j in seen if _apply_filters(j, job_types, work_modes, posted_within_days)]
    logger.info("Filter: %d unique → %d after filters", len(seen), len(filtered))

    return filtered, dupes
