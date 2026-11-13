"""
Offline tests for the Workable feed parser (parse_workable_feed). No network:
each test parses a small XML document shaped like
https://www.workable.com/boards/workable.xml.

Run from the repository root:
    python -m scripts.tests.test_suite.test_workable
"""

import io
import sys
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scraper import parse_workable_feed


def _job(title, url, company, date, city="", state="", country="", remote="false", category=""):
    def field(name, value):
        return f"<{name}>\n  <![CDATA[{value}]]>\n</{name}>"

    return "<job>" + "".join(
        field(n, v)
        for n, v in (
            ("title", title),
            ("date", date),
            ("referencenumber", url.rsplit("/", 1)[-1]),
            ("url", url),
            ("company", company),
            ("city", city),
            ("state", state),
            ("country", country),
            ("remote", remote),
            ("postalcode", ""),
            ("description", "<p>Text with </source> and <job> inside CDATA</p>"),
            ("category", category),
            ("website", "https://example.com"),
        )
    ) + "</job>"


def _feed(*jobs, close=True):
    text = (
        '<?xml version="1.0" encoding="utf-8"?>\n<source>\n'
        "<publisher>Workable</publisher>\n"
        "<publisherurl>https://www.workable.com</publisherurl>\n"
        + "\n".join(jobs)
        + ("\n</source>\n" if close else "\n")
    )
    return io.BytesIO(text.encode("utf-8"))


def _by_url(jobs):
    return {j["url"]: j for j in jobs}


# A posting listed in two locations: the newer row comes first in the feed.
MULTI = [
    _job("Senior Backend Engineer", "https://apply.workable.com/j/AAA111", "Acme GmbH",
         "Tue, 03 Nov 2026 09:00:00 UTC", city="New York", state="NY", country="US",
         category="Engineering"),
    _job("Senior Backend Engineer", "https://apply.workable.com/j/AAA111", "Acme GmbH",
         "Mon, 02 Nov 2026 08:30:00 UTC", city="Berlin", state="Berlin", country="DE",
         category="Engineering"),
]
REMOTE = _job("Support Agent", "https://apply.workable.com/j/BBB222", "Beta Ltd",
              "Wed, 04 Nov 2026 10:00:00 UTC", country="GB", remote="true")
NO_URL = _job("Ghost", "", "Beta Ltd", "Wed, 04 Nov 2026 10:00:00 UTC")
NOT_HTTPS = _job("Ghost", "javascript:alert(1)", "Beta Ltd", "Wed, 04 Nov 2026 10:00:00 UTC")
# Two rows of one posting whose first location is longer than the 50-character cell.
LONG = [
    _job("Analyst", "https://apply.workable.com/j/CCC333", "Gamma",
         "Mon, 02 Nov 2026 08:00:00 UTC", city="Al Khobar", state="Eastern Province",
         country="SA"),
    _job("Analyst", "https://apply.workable.com/j/CCC333", "Gamma",
         "Mon, 02 Nov 2026 09:00:00 UTC", city="Riyadh", country="SA"),
]


def test_folds_rows_of_one_posting():
    active, jobs = parse_workable_feed(_feed(*MULTI, REMOTE))
    assert len(jobs) == 2, f"expected 2 postings, got {len(jobs)}"
    assert active == {"Acme GmbH": 1, "Beta Ltd": 1}, f"got {active!r}"


def test_posting_takes_oldest_row():
    _, jobs = parse_workable_feed(_feed(*MULTI))
    job = _by_url(jobs)["https://apply.workable.com/j/AAA111"]
    assert job["updated_at"] == "2026-11-02T08:30:00Z", f"got {job['updated_at']!r}"
    assert job["location"] == "Berlin, Germany (+1 more)", f"got {job['location']!r}"
    datetime.fromisoformat(job["updated_at"].replace("Z", "+00:00"))


def test_country_code_is_not_read_as_us_state():
    """DE is Germany here, never Delaware: coords come from the city in DE."""
    _, jobs = parse_workable_feed(_feed(*MULTI))
    job = _by_url(jobs)["https://apply.workable.com/j/AAA111"]
    assert job["coords"], "expected coordinates for Berlin, DE"
    lat, lng = job["coords"]
    assert 52 < lat < 53 and 13 < lng < 14, f"Berlin coords expected, got {job['coords']!r}"


def test_job_fields():
    _, jobs = parse_workable_feed(_feed(*MULTI))
    job = jobs[0]
    assert job["ats"] == "Workable"
    assert job["company"] == "Acme GmbH"
    assert job["title"] == "Senior Backend Engineer"
    assert job["url"] == "https://apply.workable.com/j/AAA111"
    assert job["id"] == "AAA111"
    assert "departments" not in job, "the feed's category is a job function, not a department"
    assert job["skill_level"] == "senior"
    assert job["remote"] is False
    assert "workplaceType" not in job


def test_remote_posting():
    _, jobs = parse_workable_feed(_feed(REMOTE))
    job = jobs[0]
    assert job["remote"] is True
    assert job["workplaceType"] == "Remote"
    assert job["location"] == "Remote, United Kingdom", f"got {job['location']!r}"
    assert job["coords"] is None


def test_row_without_https_url_is_skipped():
    _, jobs = parse_workable_feed(_feed(NO_URL, NOT_HTTPS, REMOTE))
    assert [j["url"] for j in jobs] == ["https://apply.workable.com/j/BBB222"]


def test_long_location_keeps_the_count():
    _, jobs = parse_workable_feed(_feed(*LONG))
    location = jobs[0]["location"]
    assert len(location) <= 50, f"longer than 50: {location!r}"
    assert location.endswith(" (+1 more)"), f"count was cut: {location!r}"


def test_cut_short_feed_raises():
    try:
        parse_workable_feed(_feed(*MULTI, REMOTE, close=False))
    except ET.ParseError:
        return
    raise AssertionError("a feed without </source> must raise ParseError")


def test_wrong_root_raises():
    try:
        parse_workable_feed(io.BytesIO(b"<html><body>Too Many Requests</body></html>"))
    except ET.ParseError:
        return
    raise AssertionError("a document that is not the feed must raise ParseError")


def run_unit_tests():
    tests = [
        test_folds_rows_of_one_posting,
        test_posting_takes_oldest_row,
        test_country_code_is_not_read_as_us_state,
        test_job_fields,
        test_remote_posting,
        test_row_without_https_url_is_skipped,
        test_long_location_keeps_the_count,
        test_cut_short_feed_raises,
        test_wrong_root_raises,
    ]
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
    print("── Workable feed parser (unit tests) ──")
    passed, failed = run_unit_tests()
    print(f"Workable unit tests: {passed} passed, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
