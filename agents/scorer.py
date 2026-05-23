"""Multi-provider scoring agent — Gemini, Grok, or Anthropic. Auto-detects which key is present."""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

SYSTEM_PROMPT = """\
You are a job-fit scoring agent. Score each job against the candidate's CV.

Return ONLY a valid JSON array — no prose, no markdown fences.
Each element must be an object with exactly these keys:
  job_index        : int   (0-based, matches input order)
  score            : int   (0-100)
  match_summary    : str   (2-3 sentences)
  missing_keywords : list[str]
  skill_gaps       : str   (1-2 sentences)
  apply_priority   : "High" | "Medium" | "Low"

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
            f"  Title: {job.get('title', '')}\n"
            f"  Company: {job.get('company', '')}\n"
            f"  Location: {job.get('location', '')}\n"
            f"  JD: {(job.get('jd_full_text') or '')[:3000]}\n"
        )
    return "\n".join(lines)


def _parse_scores(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _merge(batch: list, scores: list) -> list:
    score_map = {s["job_index"]: s for s in scores if isinstance(s, dict)}
    result = []
    for i, job in enumerate(batch):
        s = score_map.get(i, {})
        result.append({
            **job,
            "score": s.get("score", 0),
            "match_summary": s.get("match_summary", ""),
            "missing_keywords": ", ".join(s.get("missing_keywords", [])),
            "skill_gaps": s.get("skill_gaps", ""),
            "apply_priority": s.get("apply_priority", "Low"),
        })
    return result


# ── Gemini ────────────────────────────────────────────────────────────────────
async def _score_batch_gemini(client, batch: list, cv_text: str, keywords: list, model_id: str) -> list:
    prompt = SYSTEM_PROMPT + "\n\n" + _build_prompt(cv_text, keywords, batch)
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: client.models.generate_content(model=model_id, contents=prompt)
        )
        scores = _parse_scores(response.text)
    except Exception as e:
        logger.error("Gemini batch error: %s", e)
        scores = []
    return _merge(batch, scores)


# ── Grok (OpenAI-compatible) ──────────────────────────────────────────────────
async def _score_batch_grok(client, batch: list, cv_text: str, keywords: list, model_id: str) -> list:
    prompt = _build_prompt(cv_text, keywords, batch)
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        )
        scores = _parse_scores(response.choices[0].message.content)
    except Exception as e:
        logger.error("Grok batch error: %s", e)
        scores = []
    return _merge(batch, scores)


# ── Anthropic ─────────────────────────────────────────────────────────────────
async def _score_batch_anthropic(client, batch: list, cv_text: str, keywords: list, model_id: str) -> list:
    prompt = _build_prompt(cv_text, keywords, batch)
    try:
        response = await client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        scores = _parse_scores(response.content[0].text)
    except Exception as e:
        logger.error("Anthropic batch error: %s", e)
        scores = []
    return _merge(batch, scores)


# ── Main entry ────────────────────────────────────────────────────────────────
async def score_jobs(
    jobs: list,
    cv_text: str,
    keywords: list,
    gemini_key: str = "",
    grok_key: str = "",
    anthropic_key: str = "",
    model: str = "flash",
    min_score: int = 40,
) -> tuple:
    """
    Score jobs using whichever AI key is available.
    Priority: Gemini → Grok → Anthropic
    """
    batches = [jobs[i:i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]

   

    # if grok_key:
    #     from openai import OpenAI
    #     provider = "Grok"
    #     model_id = "grok-3-mini" if model == "flash" else "grok-3"
    #     client = OpenAI(api_key=grok_key, base_url="https://api.x.ai/v1")
    #     tasks = [_score_batch_grok(client, b, cv_text, keywords, model_id) for b in batches]
    
    if grok_key:
        from openai import OpenAI
        provider = "Groq"
        model_id = "llama-3.3-70b-versatile" if model == "flash" else "llama-3.3-70b-versatile"
        client = OpenAI(api_key=grok_key, base_url="https://api.groq.com/openai/v1")
        tasks = [_score_batch_grok(client, b, cv_text, keywords, model_id) for b in batches]
    
    elif gemini_key:
        from google import genai
        provider = "Gemini"
        model_id = "gemini-2.0-flash" if model == "flash" else "gemini-1.5-pro"
        client = genai.Client(api_key=gemini_key)
        tasks = [_score_batch_gemini(client, b, cv_text, keywords, model_id) for b in batches]

    elif anthropic_key:
        import anthropic
        provider = "Anthropic"
        model_id = "claude-haiku-4-5-20251001" if model == "flash" else "claude-sonnet-4-6"
        client = anthropic.AsyncAnthropic(api_key=anthropic_key)
        tasks = [_score_batch_anthropic(client, b, cv_text, keywords, model_id) for b in batches]

    else:
        raise ValueError("No AI key provided. Add a Gemini, Grok, or Anthropic key in your profile.")

    logger.info("Scoring %d jobs in %d batches using %s (%s)", len(jobs), len(batches), provider, model_id)

    batch_results = await asyncio.gather(*tasks, return_exceptions=True)

    all_scored = []
    for result in batch_results:
        if isinstance(result, Exception):
            logger.error("Batch exception: %s", result)
        else:
            all_scored.extend(result)

    passed = [j for j in all_scored if j.get("score", 0) >= min_score]
    dropped = len(all_scored) - len(passed)
    passed.sort(key=lambda j: j.get("score", 0), reverse=True)

    logger.info("Scoring done: %d scored, %d passed threshold (%d), %d dropped",
                len(all_scored), len(passed), min_score, dropped)
    return passed, dropped