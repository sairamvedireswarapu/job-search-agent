"""Tests for scorer agent — mocks Anthropic API."""
import json, sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _make_jobs(n):
    return [{"title": f"Job {i}", "company": "Co", "location": "Remote", "jd_full_text": "Python ML AI"} for i in range(n)]


@pytest.mark.asyncio
async def test_score_jobs_filters_low_scores(monkeypatch):
    """Jobs below min_score should be dropped."""
    import anthropic
    from agents import scorer

    mock_response_data = [
        {"job_index": i, "score": 20 if i == 0 else 75, "match_summary": "ok",
         "missing_keywords": [], "skill_gaps": "none", "apply_priority": "High"}
        for i in range(3)
    ]

    class FakeContent:
        text = json.dumps(mock_response_data)

    class FakeResponse:
        content = [FakeContent()]

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: FakeClient())

    jobs = _make_jobs(3)
    passed, dropped = await scorer.score_jobs(
        jobs=jobs, cv_text="Python ML", keywords=["ML Engineer"],
        anthropic_key="fake", model="haiku", min_score=40,
    )
    assert dropped == 1
    assert all(j["score"] >= 40 for j in passed)


@pytest.mark.asyncio
async def test_score_jobs_sorted_descending(monkeypatch):
    import anthropic
    from agents import scorer

    scores_data = [
        {"job_index": 0, "score": 55, "match_summary": "ok", "missing_keywords": [], "skill_gaps": "", "apply_priority": "Medium"},
        {"job_index": 1, "score": 90, "match_summary": "ok", "missing_keywords": [], "skill_gaps": "", "apply_priority": "High"},
        {"job_index": 2, "score": 70, "match_summary": "ok", "missing_keywords": [], "skill_gaps": "", "apply_priority": "High"},
    ]

    class FakeContent:
        text = json.dumps(scores_data)

    class FakeResponse:
        content = [FakeContent()]

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: FakeClient())

    jobs = _make_jobs(3)
    passed, _ = await scorer.score_jobs(
        jobs=jobs, cv_text="Python ML", keywords=["ML Engineer"],
        anthropic_key="fake", model="haiku", min_score=40,
    )
    scores = [j["score"] for j in passed]
    assert scores == sorted(scores, reverse=True)
