"""Gemini scoring agent — batches jobs, runs concurrent scoring, merges results."""
import asyncio
import json
import logging
from google import genai

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
MODEL_FLASH = "gemini-2.0-flash"
MODEL_PRO = "gemini-1.5-pro"

SYSTEM_PROMPT = """\
You are a job-fit scoring agent. Score each job against the candidate's CV.

Return ONLY a valid JSON array — no prose, no markdown fences.
Each element must be an object with exactly these keys:
  job_index   : int   (0-based, matches input order)
  score        : int   (0-100)
  match_summary: str   (2-3 sentences)
  missing_keywords: list[str]
  skill_gaps   : str   (1-2 sentences)
  apply_priority: "High" | "Medium" | "Low"

Scoring rubric:
  80-100 -> Strong match
  60-79  -> Good match
  40-59  -> Partial match
  0-39   -> Poor match
"""


def _build_prompt(cv_text: str, keywords: list, batch: list) -> str:
    lines = [
        f"CANDIDATE CV:\n{cv_text}\n",
        f"TARGET KEYWORDS: {', '.join(keywords)}\n",
        "Score these jobs:\n",
    ]
    for i, job in enumerate(batch):
        lines.append(
            f"JOB {i}:\n"
            f"  Title: {job.get('title','')}\n"
            f"  Company: {job.get('company','')}\n"
            f"  Location: {job.get('location','')}\n"
            f"  JD: {(job.get('jd_full_text') or '')[:3000]}\n"
        )
    return SYSTEM_PROMPT + "\n\n" + "\n".join(lines)


async def _score_batch(client, cv_text: str, keywords: list, batch: list, model_id: str) -> list:
    prompt = _build_prompt(cv_text, keywords, batch)
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(model=model_id, contents=prompt)
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.error("Score batch JSON parse failed: %s", e)
        scores = []
    except Exception as e:
        logger.error("Score batch error: %s", e)
        scores = []

    score_map = {s["job_index"]: s for s in scores if isinstance(s, dict)}
    scored = []
    for i, job in enumerate(batch):
        s = score_map.get(i, {})
        scored.append({
            **job,
            "score": s.get("score", 0),
            "match_summary": s.get("match_summary", ""),
            "missing_keywords": ", ".join(s.get("missing_keywords", [])),
            "skill_gaps": s.get("skill_gaps", ""),
            "apply_priority": s.get("apply_priority", "Low"),
        })
    return scored


async def score_jobs(
    jobs: list,
    cv_text: str,
    keywords: list,
    gemini_key: str,
    model: str = "flash",
    min_score: int = 40,
) -> tuple:
    model_id = MODEL_PRO if model == "pro" else MODEL_FLASH
    client = genai.Client(api_key=gemini_key)

    batches = [jobs[i:i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]
    logger.info("Scoring %d jobs in %d batches using %s", len(jobs), len(batches), model_id)

    batch_results = await asyncio.gather(
        *[_score_batch(client, cv_text, keywords, batch, model_id) for batch in batches],
        return_exceptions=True,
    )

    all_scored = []
    for result in batch_results:
        if isinstance(result, Exception):
            logger.error("Batch scoring exception: %s", result)
        else:
            all_scored.extend(result)

    passed = [j for j in all_scored if j.get("score", 0) >= min_score]
    dropped = len(all_scored) - len(passed)
    passed.sort(key=lambda j: j.get("score", 0), reverse=True)

    logger.info("Scoring done: %d scored, %d passed threshold (%d), %d dropped",
                len(all_scored), len(passed), min_score, dropped)
    return passed, dropped