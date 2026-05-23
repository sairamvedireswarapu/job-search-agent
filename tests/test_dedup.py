"""Tests for dedup_filter agent."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agents.dedup_filter import dedup_and_filter


def _job(title, company, url="", jd="some job description text"):
    return {"title": title, "company": company, "url": url, "jd_full_text": jd, "job_type": "Full-time", "location": ""}


def test_exact_url_dedup():
    jobs = [
        _job("ML Engineer", "Acme", url="https://example.com/job/1"),
        _job("ML Engineer", "Acme", url="https://example.com/job/1"),  # dupe
    ]
    clean, dupes = dedup_and_filter(jobs)
    assert len(clean) == 1
    assert dupes == 1


def test_fuzzy_title_company_dedup():
    jobs = [
        _job("Senior ML Engineer", "Acme Corp"),
        _job("Senior ML Engineer", "Acme Corp."),   # fuzzy dupe
        _job("Data Scientist", "Other Co"),
    ]
    clean, dupes = dedup_and_filter(jobs)
    assert len(clean) == 2
    assert dupes == 1


def test_no_false_positives():
    jobs = [
        _job("ML Engineer", "Google"),
        _job("ML Engineer", "Meta"),
        _job("LLM Engineer", "Google"),
    ]
    clean, dupes = dedup_and_filter(jobs)
    assert len(clean) == 3
    assert dupes == 0


def test_empty_input():
    clean, dupes = dedup_and_filter([])
    assert clean == []
    assert dupes == 0
