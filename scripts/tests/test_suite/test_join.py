"""
Offline tests for the Join source. No network: a fake client or a fake
session answers every request.

Run from the repository root:
    python -m scripts.tests.test_suite.test_join
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests

from scraper import (
    JOIN_GRAPHQL_URL,
    JOIN_RETRIES,
    JOIN_SITEMAP_URL,
    JoinClient,
    JoinRateLimitError,
    _RateLimiter,
    fetch_join_company_jobs,
    fetch_join_jobs,
    format_join_location,
    load_join_cache,
    parse_join_company_id,
    parse_join_sitemap,
    save_join_cache,
)
from merge_data import get_dedup_key


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)
        self.headers = {"Content-Type": "application/json" if body is not None else "text/html"}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeClient:
    """Answers request() from a handler; records the calls."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


def sitemap(*slugs):
    locs = "".join(f"<url><loc>https://join.com/companies/{s}</loc></url>" for s in slugs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def careers_page(company_id, shape="pageData"):
    data = {"props": {"pageProps": {shape: {"company": {"id": company_id, "name": "Acme GmbH"}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def job(jid, title="Backend Engineer", **extra):
    j = {
        "id": jid,
        "title": title,
        "createdAt": "2026-11-02T08:30:00.000Z",
        "workplaceType": "ONSITE",
        "remoteType": None,
        "remoteRegion": None,
        "city": {"cityName": "Berlin", "countryName": "Germany"},
        "country": {"name": "Germany"},
        "company": {"name": "Acme GmbH"},
    }
    j.update(extra)
    return j


def gql(items, page_count=1):
    return {"data": {"publicJobs": {"items": items, "pageInfo": {"pageCount": page_count}}}}


def gql_page(kwargs):
    return kwargs["json"]["variables"]["input"]["paginationPage"]["page"]


def expect_raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# ── Sitemap ───────────────────────────────────────────────────────────────────

def test_sitemap_reads_company_pages_once():
    xml = sitemap("acme", "beta", "acme").replace(
        "</urlset>",
        "<url><loc>https://join.com/companies/acme/123-engineer</loc></url>"
        "<url><loc>https://example.com/companies/other</loc></url>"
        "<url><loc>https://join.com/companies/a&amp;b</loc></url></urlset>",
    )
    assert parse_join_sitemap(xml) == ["acme", "beta", "a&b"], parse_join_sitemap(xml)


def test_sitemap_empty_urlset_is_empty():
    assert parse_join_sitemap('<urlset xmlns="x"></urlset>') == []
    assert parse_join_sitemap('<urlset xmlns="x"/>') == []


def test_sitemap_cut_short_raises():
    expect_raises(lambda: parse_join_sitemap(sitemap("acme").replace("</urlset>", "")), ValueError)


def test_sitemap_without_company_pages_raises():
    xml = '<urlset><url><loc>https://join.com/companies/acme/1-job</loc></url></urlset>'
    expect_raises(lambda: parse_join_sitemap(xml), ValueError)


# ── Careers page ──────────────────────────────────────────────────────────────

def test_company_id_from_both_page_shapes():
    assert parse_join_company_id(careers_page(36)) == 36
    assert parse_join_company_id(careers_page(36, shape="initialState")) == 36


def test_company_id_missing_or_invalid():
    assert parse_join_company_id("<html>no data</html>") is None
    assert parse_join_company_id(careers_page(None)) is None
    assert parse_join_company_id(careers_page("36")) is None
    assert parse_join_company_id(careers_page(True)) is None


# ── Location ──────────────────────────────────────────────────────────────────

def test_location_labels():
    assert format_join_location(job(1)) == "Berlin, Germany"
    assert format_join_location(job(1, workplaceType="HYBRID")) == "Berlin, Germany (hybrid)"
    assert format_join_location(job(1, workplaceType="REMOTE", remoteType="ANYWHERE")) == "Remote (worldwide)"
    assert format_join_location(
        job(1, workplaceType="REMOTE", remoteType="REGION", remoteRegion={"name": "EU"})
    ) == "Remote (EU)"
    assert format_join_location(job(1, workplaceType="REMOTE", remoteType="COUNTRY")) == "Remote (Germany)"


# ── Jobs of one company ───────────────────────────────────────────────────────

def test_jobs_fields():
    client = FakeClient(lambda m, u, kw: FakeResponse(body=gql([job(7, "Senior Backend Engineer")])))
    jobs = fetch_join_company_jobs(client, "acme", 36)
    j = jobs[0]
    assert j["url"] == "https://join.com/companies/acme/7", j["url"]
    assert j["company"] == "Acme GmbH" and j["company_slug"] == "acme"
    assert j["ats"] == "Join" and j["id"] == 7
    assert j["updated_at"] == "2026-11-02T08:30:00.000Z"
    assert j["skill_level"] == "senior"
    assert j["workplaceType"] == "On-site" and j["remote"] is False
    assert j["coords"], "expected coordinates for Berlin, Germany"
    body = client.calls[0][2]["json"]
    assert body["variables"]["input"]["companyId"] == 36


def test_remote_job_has_no_coords():
    item = job(1, workplaceType="REMOTE", remoteType="ANYWHERE")
    client = FakeClient(lambda m, u, kw: FakeResponse(body=gql([item])))
    j = fetch_join_company_jobs(client, "acme", 36)[0]
    assert j["remote"] is True and j["coords"] is None and j["workplaceType"] == "Remote"


def test_company_name_falls_back_to_slug():
    client = FakeClient(lambda m, u, kw: FakeResponse(body=gql([job(1, company=None)])))
    assert fetch_join_company_jobs(client, "acme", 36)[0]["company"] == "acme"


def test_paginates_by_page_count():
    pages = {1: gql([job(1)], 2), 2: gql([job(2)], 2)}
    client = FakeClient(lambda m, u, kw: FakeResponse(body=pages[gql_page(kw)]))
    assert [j["id"] for j in fetch_join_company_jobs(client, "acme", 36)] == [1, 2]
    assert len(client.calls) == 2


def test_empty_board_is_one_request():
    client = FakeClient(lambda m, u, kw: FakeResponse(body=gql([], 0)))
    assert fetch_join_company_jobs(client, "acme", 36) == []
    assert len(client.calls) == 1


def test_malformed_rows_are_dropped():
    items = [job(1), job("2"), job(3, title="  "), job(4, title=None)]
    client = FakeClient(lambda m, u, kw: FakeResponse(body=gql(items)))
    assert [j["id"] for j in fetch_join_company_jobs(client, "acme", 36)] == [1]


def test_bad_responses_raise():
    cases = [
        lambda kw: FakeResponse(body={"errors": [{"message": "Server error"}]}),
        lambda kw: FakeResponse(body=gql({"not": "a list"})),
        lambda kw: FakeResponse(body={"data": {"publicJobs": {"items": [job(1)]}}}),
        lambda kw: FakeResponse(body=gql([job(1)], "2")),
        lambda kw: FakeResponse(body=gql([job(1)], 0)),
        lambda kw: FakeResponse(body=gql([job(1)], 2) if gql_page(kw) == 1 else gql([job(2)], 1)),
        lambda kw: FakeResponse(status=500, body={}),
    ]
    for i, case in enumerate(cases):
        client = FakeClient(lambda m, u, kw, case=case: case(kw))
        try:
            fetch_join_company_jobs(client, "acme", 36)
        except Exception:
            continue
        raise AssertionError(f"case {i} should raise")


# ── Shared client: rate limit and retries ─────────────────────────────────────

class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeLimiter:
    def __init__(self):
        self.waits = 0
        self.pauses = []

    def wait(self):
        self.waits += 1

    def pause(self, seconds):
        self.pauses.append(seconds)


RATE_LIMITED = FakeResponse(body={"errors": [{"message": "x", "extensions": {"code": "TOO_MANY_REQUESTS"}}]})


def test_rate_limit_in_body_is_retried_with_backoff():
    session = FakeSession([RATE_LIMITED, RATE_LIMITED, FakeResponse(body=gql([]))])
    limiter = FakeLimiter()
    response = JoinClient(session=session, limiter=limiter, sleep=lambda s: None).request("POST", JOIN_GRAPHQL_URL)
    assert response.json() == gql([])
    assert limiter.pauses == [2.0, 4.0], limiter.pauses
    assert limiter.waits == 3


def test_rate_limit_that_never_clears_raises():
    session = FakeSession([RATE_LIMITED] * (JOIN_RETRIES + 1))
    limiter = FakeLimiter()
    client = JoinClient(session=session, limiter=limiter, sleep=lambda s: None)
    expect_raises(lambda: client.request("POST", JOIN_GRAPHQL_URL), JoinRateLimitError)
    assert limiter.pauses == [2.0, 4.0, 8.0, 16.0, 16.0], limiter.pauses


def test_http_429_is_retried():
    session = FakeSession([FakeResponse(status=429, text=""), FakeResponse(text="<html></html>")])
    limiter = FakeLimiter()
    JoinClient(session=session, limiter=limiter, sleep=lambda s: None).request("GET", JOIN_SITEMAP_URL)
    assert limiter.pauses == [2.0]


def test_network_error_is_retried():
    session = FakeSession([requests.ConnectionError("reset"), FakeResponse(text="ok")])
    slept = []
    JoinClient(session=session, limiter=FakeLimiter(), sleep=slept.append).request("GET", JOIN_SITEMAP_URL)
    assert slept == [2.0], slept


def test_redirects_are_not_followed():
    session = FakeSession([FakeResponse(text="ok")])
    JoinClient(session=session, limiter=FakeLimiter(), sleep=lambda s: None).request("GET", JOIN_SITEMAP_URL)
    assert session.calls[0]["allow_redirects"] is False


def test_limiter_spaces_requests_and_pause_holds_everyone():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(round(s, 3))
        now[0] += s

    limiter = _RateLimiter(10, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    limiter.wait()
    assert slept == [0.1], slept
    limiter.pause(2.0)
    limiter.wait()
    assert slept[-1] == 2.0, slept


# ── Cache file ────────────────────────────────────────────────────────────────

def test_cache_file_is_sorted_one_object_per_line():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "join_company_ids.json")
        save_join_cache({"beta": 2, "acme": 1}, path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert text == '[\n{"slug": "acme", "id": 1},\n{"slug": "beta", "id": 2}\n]\n', repr(text)
        assert load_join_cache(path) == {"acme": 1, "beta": 2}
        save_join_cache({}, path)
        assert load_join_cache(path) == {}


def test_cache_missing_or_broken_reads_empty():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "join_company_ids.json")
        assert load_join_cache(path) == {}
        with open(path, "w", encoding="utf-8") as f:
            f.write("[{")
        assert load_join_cache(path) == {}


# ── Whole run ─────────────────────────────────────────────────────────────────

def run_join(cache, handler):
    """fetch_join_jobs with a fake client and a temporary cache file."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "join_company_ids.json")
        save_join_cache(cache, path)
        client = FakeClient(handler)
        active, jobs = fetch_join_jobs(client=client, cache_path=path, workers=2)
        return active, jobs, load_join_cache(path), client


def test_run_looks_up_new_companies_and_drops_gone_ones():
    def handler(method, url, kw):
        if url == JOIN_SITEMAP_URL:
            return FakeResponse(text=sitemap("acme", "newco", "broken"))
        if url == "https://join.com/companies/newco":
            return FakeResponse(text=careers_page(99))
        if url == "https://join.com/companies/broken":
            return FakeResponse(status=404, text="not found")
        cid = kw["json"]["variables"]["input"]["companyId"]
        return FakeResponse(body=gql([job(cid * 10)]))

    active, jobs, cache, client = run_join({"acme": 1, "gone": 2}, handler)
    assert cache == {"acme": 1, "newco": 99}, cache
    assert sorted(j["url"] for j in jobs) == [
        "https://join.com/companies/acme/10",
        "https://join.com/companies/newco/990",
    ]
    assert active == {"acme": 1, "newco": 1}, active
    urls = [c[1] for c in client.calls]
    assert "https://join.com/companies/acme" not in urls, "a cached company must not be looked up again"


def test_run_keeps_cached_list_when_sitemap_fails():
    def handler(method, url, kw):
        if url == JOIN_SITEMAP_URL:
            return FakeResponse(text=sitemap("acme").replace("</urlset>", ""))
        return FakeResponse(body=gql([job(1)]))

    active, jobs, cache, _ = run_join({"acme": 1, "beta": 2}, handler)
    assert cache == {"acme": 1, "beta": 2}, "a failed sitemap must not prune the cache"
    assert len(jobs) == 2


def test_run_failed_company_returns_no_partial_jobs():
    def handler(method, url, kw):
        if url == JOIN_SITEMAP_URL:
            return FakeResponse(text=sitemap("acme"))
        if gql_page(kw) == 1:
            return FakeResponse(body=gql([job(1)], 2))
        return FakeResponse(status=500, body={})

    active, jobs, cache, _ = run_join({"acme": 1}, handler)
    assert jobs == [] and active == {}
    assert cache == {"acme": 1}


# ── Dedup key ─────────────────────────────────────────────────────────────────

def test_dedup_key_is_the_job_id():
    a = {"ats": "Join", "url": "https://join.com/companies/acme/123"}
    b = {"ats": "Join", "url": "https://join.com/companies/acme-renamed/123"}
    assert get_dedup_key(a) == get_dedup_key(b) == "join:123"


def run_unit_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"  ERROR {t.__name__}: {e!r}")
            failed.append(t.__name__)
    return passed, failed


def main():
    print("── Join source (unit tests) ──")
    passed, failed = run_unit_tests()
    print(f"Join unit tests: {passed} passed, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
