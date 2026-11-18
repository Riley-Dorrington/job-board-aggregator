import sys
import time
import random
import requests
import os

from pathlib import Path
import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2])
)

 
from scraper import (
    fetch_company_jobs_greenhouse,
    fetch_company_jobs_ashby,
    fetch_company_jobs_bamboohr,
    fetch_company_jobs_lever,
    fetch_company_jobs_workday,
    fetch_company_jobs_icims,
    fetch_company_jobs_paylocity,
    load_paylocity,
    PAYLOCITY_FILE,
    USER_AGENTS,
    fetch_workable_jobs,
    JoinClient,
    load_join_cache,
    join_company_lookup,
    fetch_join_company_jobs,
)

load_paylocity(PAYLOCITY_FILE)   # populates PAYLOCITY_NAMES for the fetcher
 
SAMPLE_SIZE = 10


def fetch_workable_sample(_slug):
    """Workable has no per-company API: one feed carries every company. This
    runs the scraper's real download and parse (about a minute), so a broken
    download fails here too. Workable refuses a second full download within
    an hour, so don't run this within an hour before a scrape."""
    _, jobs = fetch_workable_jobs()
    return _slug, jobs, None


_JOIN_CLIENT = JoinClient()
_JOIN_IDS = load_join_cache()


def fetch_join_sample(slug):
    """Join's fetcher needs the numeric company id: from the cache file, or
    looked up from the careers page if the company is not cached."""
    company_id = _JOIN_IDS.get(slug) or join_company_lookup(_JOIN_CLIENT, slug)
    if not company_id:
        print(f"    no Join company id for {slug}")
        return slug, [], None
    return slug, fetch_join_company_jobs(_JOIN_CLIENT, slug, company_id), 200


SAMPLES = {
    "Greenhouse": (fetch_company_jobs_greenhouse, ["accenturefederalservices", "canonical"]),

    "Ashby":      (fetch_company_jobs_ashby,      ["confluent", "zip"]),

    "BambooHR":   (fetch_company_jobs_bamboohr,   ["agilebridge", "legato"]),

    "Lever":      (fetch_company_jobs_lever,      ["hermeus", "wyetechllc"]),

    "Workday":    (fetch_company_jobs_workday,    ["abbott|wd5|abbottcareers","accenture|wd1|accenturecareers"]),

    "iCIMS":      (fetch_company_jobs_icims,      ["orange", "libertymutual"]),

    "Paylocity":  (fetch_company_jobs_paylocity, ["9e9da9e8-1ca7-47a5-a18e-20ce401db8be",   # YMCA of Greater Oklahoma City | 142 jobs
                                              "87e48064-1e87-4bbe-abe0-6d65e861917a"]),  # YMCA of Central New York, Inc. | 100 jobs

    "Workable":   (fetch_workable_sample,        ["feed"]),   # one feed for all companies

    "Join":       (fetch_join_sample,            ["1komma5grad", "remmert"]),  # 1KOMMA5° (145 jobs) | Remmert (144 jobs)
}
 
# ATSs where the url string is constructed. A PATTERN_BROKEN here points at
# or the others (pass-through), it points at the provider
# API shape changing so extraction returned an empty/missing URL.
CONSTRUCTED_URL_ATS = {"Ashby", "BambooHR", "Workday", "Paylocity", "Join"}
 
 
def collect_sample_urls(fetch_fn, slugs):
    """Run the real fetch path for each sample company, return up to
    SAMPLE_SIZE job URLs. Empty list means extraction yielded nothing."""
    urls = []
    for slug in slugs:
        try:
            _, jobs, status = fetch_fn(slug)
        except Exception as e:
            print(f"    fetch failed for {slug}: {e}")
            continue
        if not jobs:
            # The fetchers swallow errors; the status says which kind it was.
            print(f"    no jobs for {slug} (last HTTP status: {status})")
            if fetch_fn is fetch_company_jobs_workday:
                probe_workday(slug)
        for job in jobs:
            url = job.get("url")
            if url:
                urls.append(url)
            if len(urls) >= SAMPLE_SIZE:
                return urls
    return urls
 
 
def probe_workday(slug):
    """One raw request to a Workday sample, printing what came back, so an
    empty result shows whether Workday refused, changed its response, or
    returned an empty list."""
    company, wd, site_id = slug.split("|")
    base_url = f"https://{company}.{wd}.myworkdayjobs.com"
    try:
        resp = requests.post(
            f"{base_url}/wday/cxs/{company}/{site_id}/jobs",
            json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": random.choice(USER_AGENTS),
                "Origin": base_url,
                "Referer": f"{base_url}/{site_id}",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"    probe {slug}: {e}")
        return
    body = " ".join(resp.text[:300].split())
    print(
        f"    probe {slug}: HTTP {resp.status_code},"
        f" {resp.headers.get('Content-Type', '?')}, body: {body}"
    )


def url_resolves(url):
    """HEAD first (cheap); fall back to GET on anything HEAD can't answer cleanly.
    Paylocity's CDN returns 404 to HEAD but serves the page on GET, so a failed
    HEAD is not proof the URL is dead. Verify with GET before judging.
    
    The other ATS platforms dont have this problem.
    """
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.head(url, allow_redirects=True, timeout=15, headers=headers)
        if resp.status_code < 400:
            return True
        resp = requests.get(url, allow_redirects=True, timeout=15, headers=headers)
        return resp.status_code < 400
    except requests.RequestException:
        try:
            resp = requests.get(url, allow_redirects=True, timeout=15, headers=headers)
            return resp.status_code < 400
        except requests.RequestException:
            return False
 
 
def check_ats(name, fetch_fn, slugs):
    urls = collect_sample_urls(fetch_fn, slugs)
    if not urls:
        # Extraction produced no URLs at all - parse path may have broken,
        # or every sample company is genuinely empty. Either way, flag it.
        return name, 0, 0, "NO_URLS"
 
    alive = 0
    for url in urls:
        if url_resolves(url):
            alive += 1
        time.sleep(random.uniform(0.5, 1.5))  # polite spacing to avoid blocks
 
    total = len(urls)
    if alive == 0:
        status = "PATTERN_BROKEN"
    elif alive < total * 0.5:
        status = "DEGRADED"  # informational - unusually high failure rate
    else:
        status = "OK"
    return name, alive, total, status
 
 
def diagnose(name, status):
    if status == "PATTERN_BROKEN":
        if name in CONSTRUCTED_URL_ATS:
            return "The built URL is likely stale (you construct this URL)"
        return "The provider API shape likely changed (URL is pass-through)"
    if status == "NO_URLS":
        return "Extraction returned nothing - parse path or all samples empty"
    return ""
 
 
def main():
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    summary_lines = []
    failures = []
    for name, (fetch_fn, slugs) in SAMPLES.items():
        name, alive, total, status = check_ats(name, fetch_fn, slugs)
        note = diagnose(name, status)
        line = f"{name:12} {alive}/{total} alive  -> {status}"
        if note:
            line += f"  ({note})"
        print(line)
        summary_lines.append(line)
        if status in ("PATTERN_BROKEN", "NO_URLS"):
            failures.append(name)
 
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("\n".join(summary_lines) + "\n")

    if failures:
        alert = f"\n[ALERT] investigate before the scrape: {', '.join(failures)}"
        print(alert)
        sys.exit(1)

    print("\nAll ATSs healthy.")
 
 
if __name__ == "__main__":
    main()
 