import requests, threading, json, random, time, re, os, gzip, argparse, html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from urllib.parse import unquote
from geolocation import (
    build_lookup,
    lookup_location,
    normalize,
    COUNTRY_ALIASES,
    CITY_ALIASES,
)
from requests.adapters import HTTPAdapter

# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
GREENHOUSE_FILE = os.path.join(ROOT_DIR, "data", "greenhouse_companies.json")
ASHBY_FILE = os.path.join(ROOT_DIR, "data", "ashby_companies.json")
BAMBOOHR_FILE = os.path.join(ROOT_DIR, "data", "bamboohr_companies.json")
WORKDAY_FILE = os.path.join(ROOT_DIR, "data", "workday_companies.json")
LEVER_FILE = os.path.join(ROOT_DIR, "data", "lever_companies.json")
ICIMS_FILE = os.path.join(ROOT_DIR, "data", "icims_companies.json")
PAYLOCITY_FILE = os.path.join(ROOT_DIR, "data", "paylocity_companies_clean.json")

LOCATIONS_FILE = os.path.join(ROOT_DIR, "data", "locations.json")


PAGEDATA_RE = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", re.DOTALL)

# guid -> company name, filled by load_paylocity()
PAYLOCITY_NAMES = {}

# one requests.Session per worker thread, with a capped connection pool
_paylocity_local = threading.local()


def load_paylocity(filepath):
    """Paylocity clean file is [{guid, name, jobs}, ...], not a flat slug list.
    Returns a set of GUIDs and fills PAYLOCITY_NAMES (guid -> name)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        print(f"File not found: {filepath}")
        return set()
    guids = set()
    for r in rows:
        g = r.get("guid")
        if not g:
            continue
        guids.add(g)
        name = r.get("name") or g
        PAYLOCITY_NAMES[g] = html.unescape(name)
    print(f"Loaded {len(guids):,} Paylocity companies from {filepath}")
    return guids


def _paylocity_session():
    s = getattr(_paylocity_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        s.mount("https://", adapter)
        _paylocity_local.session = s
    return s


OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# GEOLOCATION LOOKUP (loaded once, shared across all workers)
# ============================================================

print("Loading location lookup from locations.json...")
LOCATION_MAPS = build_lookup(LOCATIONS_FILE)
print(f"  {len(LOCATION_MAPS['city']):,} city-only entries loaded")


def enrich_location(location_str):
    """Resolve a location string to (remote, coords). Safe to call from worker threads."""
    result = lookup_location(location_str, LOCATION_MAPS)
    return result["remote"], result["coords"]


RECRUITER_TERMS = [
    "recruit",
    "recruiting",
    "recruiter",
    "staffing",
    "staff",
    "talent",
    "talenthub",
    "talentgroup",
    "solutions",
    "consulting",
    "placement",
    "search",
    "resources",
    "agency",
]

USER_AGENTS = [
    # Chrome 144 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    # Chrome 144 - macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    # Chrome 144 - Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    # Firefox 147 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    # Firefox 147 - macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
    # Firefox 147 - Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
    # Safari 26 - macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
    # Edge 144 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
]

# ============================================================
# LOAD COMPANIES
# ============================================================


def load_companies(filepath):
    """Load companies from JSON file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            companies = set(json.load(f))
        print(f"Loaded {len(companies):,} companies from {filepath}")
        return companies
    except FileNotFoundError:
        print(f"File not found: {filepath}")
        return set()


# ============================================================
# VERIFY ACTIVE JOBS + FETCH ALL JOBS
# ============================================================

# API requests for testing in browser console
"""
fetch("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    operationName: "ApiJobBoardWithTeams",
    variables: {organizationHostedJobsPageName: "zip"},
    query: "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { jobPostings { id title locationName } } }"
  })
}).then(r => r.json()).then(console.log)

fetch("https://{slug}.bamboohr.com/careers/list"){
    method: "GET",
    headers: {"Content-Type": "application/json"},
}.then(r => r.json()).then(console.log)

}
"""

SOURCE_TYPE = "automated"


def get_job_metadata():
    """Generate consistent metadata for each job."""
    return {
        "scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": SOURCE_TYPE,
    }


def fetch_company_jobs_greenhouse(slug):
    """Fetch all jobs for a company."""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            data = response.json()
            jobs = data.get("jobs", [])

            if jobs:
                # Normalize job structure for frontend
                normalized = []
                for job in jobs:
                    location = job.get("location", {}).get("name", "Not specified")
                    remote, coords = enrich_location(location)
                    normalized.append(
                        {
                            "company": slug,
                            "company_slug": slug,
                            "title": job.get("title"),
                            "location": location,
                            "remote": remote,
                            "coords": coords,
                            "url": job.get("absolute_url"),
                            "absolute_url": job.get("absolute_url"),
                            "departments": [
                                d.get("name") for d in job.get("departments", [])
                            ],
                            "id": job.get("id"),
                            "updated_at": job.get("updated_at"),
                            "is_recruiter": is_recruiter_company(slug),
                            "ats": "Greenhouse",
                            "skill_level": job_tier_classification(
                                job.get("title", "")
                            ),
                            **get_job_metadata(),
                        }
                    )

                return slug, normalized, response.status_code

        return slug, [], response.status_code  # got a response, just not 200

    except Exception as e:
        print(f"Error fetching Greenhouse for {slug}: {e}")
    return slug, [], None


def fetch_company_jobs_ashby(slug):
    try:
        url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
        payload = {
            "operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": slug},
            "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { jobPostings { id title locationName } } }",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
        }

        # Jitter before request to spread out concurrent workers
        time.sleep(random.uniform(0.5, 2.0))

        max_retries = 2
        for attempt in range(max_retries + 1):
            response = requests.post(url, json=payload, headers=headers, timeout=30)

            if response.status_code == 200:
                break
            elif response.status_code in (429, 503, 502):
                if attempt < max_retries:
                    backoff = (2**attempt) + random.uniform(0.5, 1.5)
                    print(
                        f"  Ashby {slug}: {response.status_code}, retrying in {backoff:.1f}s"
                    )
                    time.sleep(backoff)
                    headers["User-Agent"] = random.choice(USER_AGENTS)
                    continue
            # Non-retryable status
            return slug, [], response.status_code

        if response.status_code != 200:
            return slug, [], response.status_code

        data = response.json()
        jobs = (data.get("data") or {}).get("jobBoard") or {}
        jobs = jobs.get("jobPostings") or []

        if jobs:
            normalized = []
            for job in jobs:
                normalized.append(
                    {
                        "company": slug,
                        "company_slug": slug,
                        "title": job.get("title", ""),
                        "location": job.get("locationName", "Not specified")[:50],
                        "url": f"https://jobs.ashbyhq.com/{slug}/{job.get('id')}",
                        "is_recruiter": is_recruiter_company(slug),
                        "ats": "Ashby",
                        "skill_level": job_tier_classification(job.get("title", "")),
                        **get_job_metadata(),
                    }
                )
            return slug, normalized, response.status_code

        return slug, [], response.status_code  # got a response, just not 200

    except Exception as e:
        print(f"Error fetching Ashby for {slug}: {e}")
    return slug, [], None


def fetch_company_jobs_bamboohr(slug):
    """https://{slug}.bamboohr.com/careers
    https://{slug}.bamboohr.com/careers/list

    """
    url = f"https://{slug}.bamboohr.com/careers/list"

    time.sleep(random.uniform(0.5, 2.0))

    max_retries = 2
    for attempt in range(max_retries + 1):
        headers = {
            "Accept": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
        }

        try:
            response = requests.get(url, timeout=30, headers=headers)

            if response.status_code == 200:
                if "application/json" not in response.headers.get("Content-Type", ""):
                    return slug, [], 404

                data = response.json()
                jobs = data.get("result", [])

                if jobs:
                    normalized = []
                    for job in jobs:
                        loc = job.get("location") or {}
                        if isinstance(loc, dict):
                            city = loc.get("city", "")
                            state = loc.get("state", "")
                            location = (
                                ", ".join(filter(None, [city, state]))
                                or "Not specified"
                            )
                        else:
                            location = str(loc) if loc else "Not specified"

                        remote, coords = enrich_location(location)
                        normalized.append(
                            {
                                "company": slug,
                                "company_slug": slug,
                                "title": job.get("jobOpeningName"),
                                "location": location[:50],
                                "remote": remote,
                                "coords": coords,
                                "url": f"https://{slug}.bamboohr.com/careers/{job.get('id')}",
                                "is_recruiter": is_recruiter_company(slug),
                                "ats": "BambooHR",
                                "skill_level": job_tier_classification(
                                    job.get("jobOpeningName", "")
                                ),
                                **get_job_metadata(),
                            }
                        )
                    return slug, normalized, response.status_code

                return slug, [], response.status_code

            if response.status_code in (429, 503, 502):
                if attempt < max_retries:
                    backoff = (2**attempt) + random.uniform(0.5, 1.5)
                    time.sleep(backoff)
                    continue

            return slug, [], response.status_code

        except requests.exceptions.SSLError:
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(0.5, 1.5))
                continue
            return slug, [], None
        except Exception as e:
            print(f"Error fetching BambooHR for {slug}: {e}")
            return slug, [], None

    return slug, [], None


def fetch_company_jobs_lever(slug):
    """https://api.lever.co/v0/postings/{slug}"""

    try:
        url = f"https://api.lever.co/v0/postings/{slug}"
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            jobs = response.json()

            if jobs:
                normalized = []
                for job in jobs:
                    categories = job.get("categories", {})
                    location = categories.get("location", "Not specified")[:50]
                    remote, coords = enrich_location(location)
                    normalized.append(
                        {
                            "company": slug,
                            "company_slug": slug,
                            "title": job.get("text"),
                            "location": location,
                            "remote": remote,
                            "coords": coords,
                            "url": job.get("hostedUrl"),
                            "is_recruiter": is_recruiter_company(slug),
                            "ats": "Lever",
                            "skill_level": job_tier_classification(job.get("text", "")),
                            **get_job_metadata(),
                        }
                    )
                return slug, normalized, response.status_code
        return slug, [], response.status_code  # got a response, just not 200
    except Exception as e:
        print(f"Error fetching Lever for {slug}: {e}")
    return slug, [], None


def _parse_workday_posted_on(text):
    """Convert Workday's relative string (e.g. 'Posted 2 Days Ago') to an ISO date."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().lower()
    today = datetime.now(timezone.utc).date()
    if "today" in t:
        return today.isoformat()
    m = re.search(r"(\d+)\s+day", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+week", t)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+month", t)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).isoformat()
    return None


def fetch_company_jobs_workday(slug):
    """
    slug format: "company|wd#|site_id" e.g. "kohls|wd1|kohlscareers"
    url: https://{company}.wd{num}.myworkdayjobs.com/wday/cxs/{company}/{site_id}/jobs
    """

    try:
        parts = slug.split("|")
        if len(parts) != 3:
            return slug, [], None

        company, wd, site_id = parts
        wd_num = wd.replace("wd", "")

        base_url = f"https://{company}.wd{wd_num}.myworkdayjobs.com"
        api_url = f"{base_url}/wday/cxs/{company}/{site_id}/jobs"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
            "Origin": base_url,
            "Referer": f"{base_url}/{site_id}",
        }

        normalized = []
        offset = 0
        limit = 20
        retries = 0
        max_retries = 2
        observed_total = None

        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }

            response = requests.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            if response.status_code != 200:
                if retries < max_retries:
                    retries += 1
                    time.sleep(random.uniform(2.0, 4.0))
                    continue
                break

            data = response.json()
            jobs = data.get("jobPostings", [])
            total = data.get("total", 0)

            # Detect silent blocking / truncation
            if observed_total is None:
                observed_total = total
            elif total != observed_total:
                # Workday sometimes lies mid-pagination when blocking
                break

            if not jobs:
                break

            for job in jobs:
                job_path = job.get("externalPath", "")
                location = (job.get("locationsText") or "Not specified")[:50]
                remote, coords = enrich_location(location)
                normalized.append(
                    {
                        "company": company,
                        "company_slug": slug,
                        "title": job.get("title"),
                        "location": location,
                        "remote": remote,
                        "coords": coords,
                        "url": f"{base_url}/{site_id}{job_path}",
                        "updated_at": _parse_workday_posted_on(job.get("postedOn")),
                        "is_recruiter": is_recruiter_company(company),
                        "ats": "Workday",
                        "skill_level": job_tier_classification(job.get("title", "")),
                        **get_job_metadata(),
                    }
                )

            offset += limit

            if offset >= total:
                break

            # Jitter between pages (critical)
            time.sleep(random.uniform(0.3, 1.0))

        return slug, normalized, response.status_code

    except Exception:
        return slug, [], None


def fetch_company_jobs_icims(slug):
    """
    https://careers-{slug}.icims.com/sitemap.xml

    Sitemap contains job URLs like:
        https://careers-{slug}.icims.com/jobs/9620/financial-service-representative/job

    Title extracted from URL path. Location not available via sitemap. Might look into fetching individual job pages for location,
    but that would be a lot more requests so skipping for now.
    """

    sitemap_url = f"https://careers-{slug}.icims.com/sitemap.xml"
    headers = {
        "Accept": "application/xml",
        "User-Agent": random.choice(USER_AGENTS),
    }

    try:
        resp = requests.get(sitemap_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return slug, [], resp.status_code

        root = ET.fromstring(resp.content)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        normalized = []
        for url_el in root.findall(".//s:url", ns):
            loc_el = url_el.find("s:loc", ns)
            if loc_el is None:
                continue
            job_url = loc_el.text.strip() if loc_el.text else ""
            if (
                not job_url
                or "/jobs/" not in job_url
                or job_url.endswith("/jobs/intro")
            ):
                continue

            path = job_url.split("/jobs/")[-1]
            parts = path.split("/")
            if len(parts) >= 2:
                title = unquote(parts[1]).replace("-", " ").strip().title()
            else:
                continue

            lastmod_el = url_el.find("s:lastmod", ns)
            updated_at = (
                lastmod_el.text.strip()
                if lastmod_el is not None and lastmod_el.text
                else None
            )

            remote, coords = False, None
            normalized.append(
                {
                    "company": slug,
                    "company_slug": slug,
                    "title": title,
                    "location": "Not specified",
                    "remote": remote,
                    "coords": coords,
                    "url": job_url,
                    "updated_at": updated_at,
                    "is_recruiter": is_recruiter_company(slug),
                    "ats": "iCIMS",
                    "skill_level": job_tier_classification(title),
                    **get_job_metadata(),
                }
            )

        return slug, normalized, resp.status_code

    except Exception as e:
        print(f"Error fetching iCIMS for {slug}: {e}")
        return slug, [], None


# ============================================================
# WORKABLE (public XML feed, no company list)
# ============================================================
#
# Workable publishes one XML feed with the postings of all its customers:
#   https://www.workable.com/boards/workable.xml
#   <source><job><title/><date/><referencenumber/><url/><company/><city/>
#     <state/><country/><remote/><description/>...<category/><website/></job>
#   ...</source>
# It is rebuilt every hour and documented for job boards:
#   https://help.workable.com/hc/en-us/articles/4420464031767
# So Workable needs no company list: the feed itself says which companies have
# postings today.
#
# The feed is large (about 870 MB of XML, served as gzip), so it is parsed as a
# stream with iterparse, one <job> at a time. Workable answers a second full
# download within one hour with HTTP 429, so it is fetched once per run.
#
# A posting with several locations has one <job> per location, all with the
# same <url>. These rows are folded into one job. Workable asks that the <url>
# values are not changed, because it uses them to attribute candidates.

WORKABLE_FEED_URL = "https://www.workable.com/boards/workable.xml"
# (connect, read) timeouts; the read timeout applies between received chunks.
WORKABLE_TIMEOUT = (30, 300)

# ISO 3166 code -> English name, from the full names in geolocation.py, so a
# country in the location text reads the same as for the other platforms.
# A code with no entry stays a bare code in the text.
WORKABLE_COUNTRY_NAMES = {}
for _alias, _code in COUNTRY_ALIASES.items():
    if _code not in WORKABLE_COUNTRY_NAMES and len(_alias) > 3 and "." not in _alias:
        WORKABLE_COUNTRY_NAMES[_code] = _alias.title()


def _workable_parse_date(text):
    """Feed date ('Wed, 28 Jul 2021 10:24:17 UTC') -> ISO 8601, or None."""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _workable_coords(city, state, country):
    """Coordinates from the feed's own city/state/country fields. The country is
    an ISO code, which locations.json uses too, so no free-text parsing is needed
    (and a code such as DE or CA cannot be read as a US state)."""
    if not city or not country:
        return None
    c = normalize(city)
    c = CITY_ALIASES.get(c, c)
    if state:
        coords = LOCATION_MAPS["city_admin_country"].get(f"{c}|{normalize(state)}|{country}")
        if coords:
            return coords
    return LOCATION_MAPS["city_country"].get(f"{c}|{country}")


def _workable_location(row):
    """Location text of one feed row: 'City, State, Country'."""
    country = WORKABLE_COUNTRY_NAMES.get(row["country"], row["country"])
    parts = []
    for p in (row["city"], row["state"], country):
        if p and p not in parts:
            parts.append(p)
    text = ", ".join(parts)
    if row["remote"] and not row["city"]:
        text = f"Remote, {text}" if text else "Remote"
    return text or "Not specified"


def parse_workable_feed(stream):
    """Parse the Workable XML feed from a binary stream.

    Returns (active_companies, jobs) like fetch_all_jobs: one job per posting
    URL, and the number of postings for each company. Raises
    xml.etree.ElementTree.ParseError when the document is cut short or not XML,
    so a partial feed is never returned.
    """
    rows_by_url = {}
    root = None
    for event, elem in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            if root is None:
                if elem.tag != "source":
                    raise ET.ParseError(f"unexpected root element <{elem.tag}>")
                root = elem
            continue
        if elem.tag != "job":
            continue
        f = {child.tag: (child.text or "").strip() for child in elem}
        # Free the parsed element: the document is too large to keep in memory.
        elem.clear()
        root.remove(elem)

        url = f.get("url", "")
        title = f.get("title")
        company = f.get("company")
        # The site renders url as a link: keep https links only.
        if not url.startswith("https://") or not title or not company:
            continue
        rows_by_url.setdefault(url, []).append(
            {
                "title": title,
                "company": company,
                "date": _workable_parse_date(f.get("date")),
                "id": f.get("referencenumber") or None,
                "city": f.get("city", ""),
                "state": f.get("state", ""),
                "country": f.get("country", "").upper(),
                "remote": f.get("remote", "").lower() == "true",
            }
        )

    active_companies = {}
    jobs = []
    for url, rows in rows_by_url.items():
        # Oldest row first: a posting's date is the date it was first published.
        rows.sort(key=lambda r: r["date"] or "9999")
        first = rows[0]
        location = _workable_location(first)
        if len(rows) > 1:
            # Cut the first location, not the count, to the 50-character limit.
            more = f" (+{len(rows) - 1} more)"
            location = location[: 50 - len(more)] + more
        remote = any(r["remote"] for r in rows)
        company = first["company"]
        job = {
            "company": company,
            "company_slug": company,
            "title": first["title"],
            "location": location[:50],
            "remote": remote,
            "coords": _workable_coords(first["city"], first["state"], first["country"]),
            "url": url,
            "absolute_url": url,
            "id": first["id"],
            "updated_at": first["date"],
            "is_recruiter": is_recruiter_company(company),
            "ats": "Workable",
            "skill_level": job_tier_classification(first["title"]),
            **get_job_metadata(),
        }
        if remote:
            job["workplaceType"] = "Remote"
        jobs.append(job)
        active_companies[company] = active_companies.get(company, 0) + 1

    return active_companies, jobs


def fetch_workable_jobs():
    """Download and parse the Workable feed. On any failure returns no jobs,
    never a partial feed; merge_data.py keeps the jobs of earlier runs."""
    print("=" * 80)
    print(f"FETCHING JOBS FROM PLATFORM: WORKABLE ({WORKABLE_FEED_URL})")
    print("=" * 80 + "\n")
    try:
        with requests.get(
            WORKABLE_FEED_URL,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            stream=True,
            timeout=WORKABLE_TIMEOUT,
            # The feed is served directly; a redirect is not the feed.
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                retry = response.headers.get("Retry-After")
                print(
                    f"  Workable feed: HTTP {response.status_code}"
                    + (f" (Retry-After: {retry}s)" if retry else "")
                )
                return {}, []
            # Let urllib3 undo the gzip content encoding while streaming.
            response.raw.decode_content = True
            active_companies, jobs = parse_workable_feed(response.raw)
    except Exception as e:
        # Reading response.raw raises urllib3 errors, not requests ones, when the
        # body is cut short; a ParseError means the XML ended early.
        print(f"  Error fetching Workable feed: {e}")
        return {}, []

    print("\nDETAILED STATS FOR WORKABLE:")
    print(f"  Companies with jobs: {len(active_companies)}")
    print(f"  Total jobs: {len(jobs)}")
    return active_companies, jobs


def _paylocity_location(j):
    """JobLocation carries the real city/state. LocationName is an internal
    label ('Main', 'AVI') and is only a last-resort fallback."""
    loc = j.get("JobLocation") or {}
    city, state = loc.get("City"), loc.get("State")
    if city and state:
        return html.unescape(f"{city}, {state}")
    if city:
        return html.unescape(city)
    return html.unescape(j.get("LocationName") or "Not specified")


def fetch_company_jobs_paylocity(slug):
    """slug is the Paylocity tenant GUID. Jobs come from window.pageData in the
    page HTML, not a JSON API. Name is resolved from PAYLOCITY_NAMES."""
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{slug}/"
    session = _paylocity_session()
    # jitter so concurrent workers don't fire in lockstep
    time.sleep(random.uniform(0.5, 2.0))
    max_retries = 3
    for attempt in range(max_retries + 1):
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            response = session.get(url, timeout=30, headers=headers)
        except requests.RequestException as e:
            # reset/abort = throttling. drop the poisoned connection, back off, retry.
            try:
                session.close()
            except Exception:
                pass
            _paylocity_local.session = None
            session = _paylocity_session()
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(1.0, 2.0))
                continue
            print(f"Error fetching Paylocity for {slug}: {e}")
            return slug, [], None
        if response.status_code in (429, 503, 502):
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(1.0, 2.0))
                continue
            return slug, [], response.status_code
        if response.status_code != 200:
            return slug, [], response.status_code
        m = PAGEDATA_RE.search(response.text)
        if not m:
            # page loaded but blob missing = soft block or layout drift.
            # return 200 so it retries next run, NOT cached as dead.
            return slug, [], 200
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return slug, [], 200
        company = PAYLOCITY_NAMES.get(slug, slug)
        normalized = []
        for job in data.get("Jobs") or []:
            job_id = job.get("JobId")
            title = html.unescape(job.get("JobTitle") or "")
            location = _paylocity_location(job)
            inferred_remote, coords = enrich_location(location)
            remote = bool(job.get("IsRemote")) or inferred_remote
            dept = job.get("HiringDepartment")  # almost always null on Paylocity
            if dept:
                dept = html.unescape(dept)
            detail = (
                f"https://recruiting.paylocity.com/recruiting/Jobs/Details/{job_id}"
                if job_id
                else url
            )
            normalized.append(
                {
                    "company": company,
                    "company_slug": slug,  # the GUID
                    "title": title,
                    "location": location,
                    "remote": remote,
                    "coords": coords,
                    "url": detail,
                    "absolute_url": detail,
                    "departments": [dept] if dept else [],
                    "id": job_id,
                    "updated_at": job.get("PublishedDate"),
                    "is_recruiter": is_recruiter_company(company),
                    "ats": "Paylocity",
                    "skill_level": job_tier_classification(title or ""),
                    **get_job_metadata(),
                }
            )
        return slug, normalized, response.status_code
    return slug, [], None


# ============================================================
# JOIN (company list from Join's own sitemap)
# ============================================================
#
# Join publishes its company directory:
#   https://join.com/companies/sitemap.xml  (listed in robots.txt)
# so the company list is read fresh on every run, not from a file.
#
# The jobs come from the public GraphQL endpoint that the careers page itself
# calls (query publicJobs). It needs the numeric company id, which only the
# careers page shows (__NEXT_DATA__). The id does not change, so it is kept in
# JOIN_FILE and read again only for companies that are new in the sitemap. A
# failed lookup is not stored and is tried again on the next run. Companies
# that left the sitemap are dropped from it. The company name comes with the
# jobs, so it is always current.
#
# JOIN_FILE is a JSON array with one {"slug", "id"} object per line, sorted by
# slug, so a daily diff shows only the companies that came or went.
#
# The GraphQL endpoint rate-limits all clients together: about 100 requests in
# a burst, then every request is refused for a few seconds; about ten requests
# a second hold. A refusal is an HTTP 200 with a GraphQL error whose
# extensions.code is TOO_MANY_REQUESTS. All join.com requests (sitemap,
# careers pages, GraphQL) go through one shared limiter, and a refusal pauses
# every worker, not only the one that got it.

JOIN_FILE = os.path.join(ROOT_DIR, "data", "join_company_ids.json")
JOIN_SITEMAP_URL = "https://join.com/companies/sitemap.xml"
JOIN_GRAPHQL_URL = "https://join.com/candidate-api/graphql"
JOIN_WORKERS = 4
JOIN_REQUESTS_PER_SECOND = 10
# Backoff after a refusal: 2, 4, 8, 16, 16 seconds. It outlasts the refusal
# window even when several workers wait on it at once.
JOIN_RETRIES = 5
JOIN_BACKOFF_BASE = 2.0
JOIN_BACKOFF_MAX = 16.0
# The server caps pageSize at 100.
JOIN_PAGE_SIZE = 100
# Safety cap on pagination, whatever pageInfo.pageCount says.
JOIN_MAX_PAGES = 50
JOIN_RATE_LIMIT_CODE = "TOO_MANY_REQUESTS"

JOIN_PUBLIC_JOBS_QUERY = """query PublicJobsList($input: PublicJobsQueryInput!) {
  publicJobs(input: $input) {
    items {
      id
      title
      createdAt
      workplaceType
      remoteType
      remoteRegion { name }
      city { cityName countryName }
      country { name }
      company { name }
    }
    pageInfo { pageCount }
  }
}"""

_JOIN_SLUG_RE = re.compile(r"^https://join\.com/companies/([^/?#]+)/?$")
_JOIN_NEXT_DATA_RE = re.compile(
    r"<script[^>]+__NEXT_DATA__[^>]*>([\s\S]*?)</script>"
)
_JOIN_URLSET_RE = re.compile(r"<urlset\b[^>]*/>|<urlset\b[^>]*>[\s\S]*</urlset\s*>")
_JOIN_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
_JOIN_WORKPLACE = {"ONSITE": "On-site", "HYBRID": "Hybrid", "REMOTE": "Remote"}


class JoinRateLimitError(Exception):
    """join.com still refused after every retry."""


class _RateLimiter:
    """Spaces requests from all threads to at most `per_second`, and lets one
    refusal pause every thread."""

    def __init__(self, per_second, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / per_second
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = self.clock()
            slot = max(now, self.next_at)
            self.next_at = slot + self.interval
        if slot > now:
            self.sleep(slot - now)

    def pause(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, self.clock() + seconds)


def _join_is_rate_limited(response):
    if response.status_code == 429:
        return True
    if response.status_code != 200 or "json" not in response.headers.get("Content-Type", ""):
        return False
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return False
    for e in errors:
        code = ((e or {}).get("extensions") or {}).get("code")
        if code == JOIN_RATE_LIMIT_CODE or "too many requests" in str((e or {}).get("message", "")).lower():
            return True
    return False


class JoinClient:
    """All join.com requests, through one shared limiter with backoff."""

    def __init__(self, session=None, limiter=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.limiter = limiter or _RateLimiter(JOIN_REQUESTS_PER_SECOND)
        self.sleep = sleep

    def request(self, method, url, **kwargs):
        """Returns the response; raises JoinRateLimitError when every retry was
        refused, and requests exceptions on network errors after the retries."""
        kwargs.setdefault("timeout", 30)
        # Every URL is a fixed https://join.com one; a redirect is not followed.
        kwargs.setdefault("allow_redirects", False)
        headers = {"User-Agent": random.choice(USER_AGENTS), **kwargs.pop("headers", {})}
        for attempt in range(JOIN_RETRIES + 1):
            self.limiter.wait()
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException:
                if attempt == JOIN_RETRIES:
                    raise
                self.sleep(min(JOIN_BACKOFF_BASE * 2**attempt, JOIN_BACKOFF_MAX))
                continue
            retryable = _join_is_rate_limited(response) or response.status_code in (502, 503, 504)
            if not retryable:
                return response
            if attempt == JOIN_RETRIES:
                if response.status_code in (502, 503, 504):
                    return response
                raise JoinRateLimitError(f"join.com: still refused after {JOIN_RETRIES} retries ({url})")
            self.limiter.pause(min(JOIN_BACKOFF_BASE * 2**attempt, JOIN_BACKOFF_MAX))
        raise AssertionError("unreachable")


def parse_join_sitemap(xml):
    """Company slugs from Join's company sitemap, in document order, each
    once. Only https://join.com/companies/<slug> counts; deeper paths and other
    hosts are skipped. An empty <urlset> is an empty list. Raises ValueError
    for a sitemap cut short (no complete <urlset>) and for one with entries of
    which none is a company page, so the caller keeps its last good list."""
    if not isinstance(xml, str) or not _JOIN_URLSET_RE.search(xml):
        raise ValueError("join: company sitemap has no complete <urlset> element")
    slugs = {}
    locs = 0
    for raw in _JOIN_LOC_RE.findall(xml):
        locs += 1
        m = _JOIN_SLUG_RE.match(html.unescape(raw))
        if m:
            slugs.setdefault(m.group(1), None)
    urls = len(re.findall(r"<url\b", xml))
    if (urls or locs) and not slugs:
        raise ValueError(
            f"join: company sitemap has {urls} <url> and {locs} <loc> entries but no company page"
        )
    return list(slugs)


def parse_join_company_id(page_html):
    """The company id from a careers page's __NEXT_DATA__, in either frontend
    shape (props.pageProps.pageData, or initialState on older cached pages).
    None when the page has no integer company id."""
    m = _JOIN_NEXT_DATA_RE.search(page_html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    page_props = (data.get("props") or {}).get("pageProps") or {}
    company = ((page_props.get("pageData") or page_props.get("initialState")) or {}).get("company") or {}
    cid = company.get("id")
    return cid if _is_join_id(cid) else None


def _is_join_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_join_cache(path=JOIN_FILE):
    """slug -> company id from JOIN_FILE; empty when it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    cache = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("slug"), str) and _is_join_id(r.get("id")):
            cache[r["slug"]] = r["id"]
    return cache


def save_join_cache(cache, path=JOIN_FILE):
    """Write JOIN_FILE: a JSON array, one object per line, sorted by slug."""
    lines = [
        json.dumps({"slug": slug, "id": cid}, ensure_ascii=False)
        for slug, cid in sorted(cache.items())
    ]
    text = "[\n" + ",\n".join(lines) + ("\n" if lines else "") + "]\n"
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def join_company_lookup(client, slug):
    """The company id from the careers page, or None when it cannot be read."""
    try:
        response = client.request("GET", f"https://join.com/companies/{slug}")
    except (requests.RequestException, JoinRateLimitError):
        return None
    if response.status_code != 200:
        return None
    return parse_join_company_id(response.text)


def format_join_location(job):
    """Location text as the careers page labels it. A remote posting shows only
    the area it is open to: its city is the company's default city."""
    city = ((job.get("city") or {}).get("cityName") or "").strip()
    country = (
        ((job.get("country") or {}).get("name") or "").strip()
        or ((job.get("city") or {}).get("countryName") or "").strip()
    )
    workplace = job.get("workplaceType")
    if workplace == "REMOTE":
        region = ((job.get("remoteRegion") or {}).get("name") or "").strip()
        remote_type = job.get("remoteType")
        area = "worldwide" if remote_type == "ANYWHERE" else region if remote_type == "REGION" else country
        return f"Remote ({area})" if area else "Remote"
    place = ", ".join(p for p in (city, country) if p)
    if workplace == "HYBRID":
        return f"{place} (hybrid)" if place else "Hybrid"
    return place or "Not specified"


def fetch_join_company_jobs(client, slug, company_id):
    """All jobs of one company. Raises when a page fails or the response has an
    unexpected shape: a partial list would read as postings that closed."""
    items = []
    page_count = 1
    page = 1
    while page <= min(page_count, JOIN_MAX_PAGES):
        response = client.request(
            "POST",
            JOIN_GRAPHQL_URL,
            json={
                "operationName": "PublicJobsList",
                "query": JOIN_PUBLIC_JOBS_QUERY,
                "variables": {
                    "input": {
                        "companyId": company_id,
                        "paginationPage": {"page": page, "pageSize": JOIN_PAGE_SIZE},
                        "filters": {},
                    }
                },
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise ValueError(f"join: publicJobs HTTP {response.status_code}")
        data = response.json()
        if data.get("errors"):
            raise ValueError(f"join: publicJobs failed: {data['errors'][0].get('message')}")
        public_jobs = (data.get("data") or {}).get("publicJobs") or {}
        page_items = public_jobs.get("items")
        if not isinstance(page_items, list):
            raise ValueError("join: publicJobs response has no items array")
        count = (public_jobs.get("pageInfo") or {}).get("pageCount")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"join: publicJobs response has no valid pageCount ({count!r})")
        if page_items and count < page:
            raise ValueError(f"join: page {page} has jobs but pageCount is {count}")
        items.extend(page_items)
        page_count = count
        page += 1

    names = [((j.get("company") or {}).get("name") or "").strip() for j in items]
    company = next((n for n in names if n), slug)
    jobs = []
    for j in items:
        jid = j.get("id")
        title = (j.get("title") or "").strip() if isinstance(j.get("title"), str) else ""
        if not isinstance(jid, int) or isinstance(jid, bool) or not title:
            continue
        workplace = j.get("workplaceType")
        location = format_join_location(j)
        if workplace == "REMOTE":
            remote, coords = True, None
        else:
            remote, coords = enrich_location(location.replace(" (hybrid)", ""))
        # /companies/<slug>/<id>: the job id stays the same for the posting's
        # whole life; Join redirects it to the current page of the posting.
        url = f"https://join.com/companies/{slug}/{jid}"
        job = {
            "company": company,
            "company_slug": slug,
            "title": title,
            "location": location[:50],
            "remote": remote,
            "coords": coords,
            "url": url,
            "absolute_url": url,
            "id": jid,
            "updated_at": j.get("createdAt"),
            "is_recruiter": is_recruiter_company(company),
            "ats": "Join",
            "skill_level": job_tier_classification(title),
            **get_job_metadata(),
        }
        if workplace in _JOIN_WORKPLACE:
            job["workplaceType"] = _JOIN_WORKPLACE[workplace]
        jobs.append(job)
    return jobs


def fetch_join_jobs(client=None, cache_path=JOIN_FILE, workers=JOIN_WORKERS):
    """Read the sitemap, update JOIN_FILE, then fetch every company's jobs.
    Returns (active_companies, jobs) like fetch_all_jobs."""
    print("=" * 80)
    print("FETCHING JOBS FROM PLATFORM: JOIN")
    print("=" * 80 + "\n")
    client = client or JoinClient()
    cache = load_join_cache(cache_path)

    try:
        response = client.request("GET", JOIN_SITEMAP_URL)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        slugs = parse_join_sitemap(response.text)
        sitemap_ok = True
    except Exception as e:
        # Keep the last good list: the companies already in JOIN_FILE.
        print(f"  join sitemap unavailable ({e}); using the {len(cache):,} cached companies")
        slugs = sorted(cache)
        sitemap_ok = False

    new = [s for s in slugs if s not in cache]
    failed_lookups = 0
    if new:
        print(f"  Looking up {len(new):,} new companies")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for slug, cid in zip(new, executor.map(lambda s: join_company_lookup(client, s), new)):
                if cid:
                    cache[slug] = cid
                else:
                    failed_lookups += 1

    if sitemap_ok:
        listed = set(slugs)
        cache = {s: v for s, v in cache.items() if s in listed}
    try:
        save_join_cache(cache, cache_path)
    except OSError as e:
        print(f"  Could not save {cache_path}: {e}")

    def run(slug):
        try:
            return slug, fetch_join_company_jobs(client, slug, cache[slug])
        except Exception as e:
            print(f"  Error fetching Join for {slug}: {e}")
            return slug, None

    targets = [s for s in slugs if s in cache]
    active_companies = {}
    all_jobs = []
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, (slug, jobs) in enumerate(executor.map(run, targets), 1):
            if jobs is None:
                failed += 1
            elif jobs:
                all_jobs.extend(jobs)
                active_companies[slug] = len(jobs)
            if i % 500 == 0:
                print(f"  [{i}/{len(targets)}] Checked...")

    print("\nDETAILED STATS FOR JOIN:")
    print(f"  Companies in sitemap: {len(slugs):,}" + ("" if sitemap_ok else " (cached list)"))
    print(f"  New lookups failed (retried next run): {failed_lookups:,}")
    print(f"  Companies checked: {len(targets):,}")
    print(f"  Companies with jobs: {len(active_companies):,}")
    print(f"  Failed: {failed:,}")
    print(f"  Total jobs: {len(all_jobs):,}")
    return active_companies, all_jobs


def fetch_all_jobs(companies, fetcher, platform="ATS"):
    """Fetch jobs from all companies in parallel."""
    print("=" * 80)
    print(f"FETCHING JOBS FROM {len(companies):,} COMPANIES FROM PLATFORM: {platform}")
    print("=" * 80 + "\n")

    platform_lower = platform.lower()

    # Skip known dead slugs
    dead_slugs = load_dead_slugs(platform_lower)
    live_companies = [s for s in companies if s not in dead_slugs]
    if dead_slugs:
        print(f"  Skipping {len(dead_slugs):,} known dead slugs")
        print(f"  Checking {len(live_companies):,} potentially active companies\n")

    all_jobs = []
    active_companies = {}
    failed = 0
    new_dead = set()

    MAX_WORKERS = {
        "bamboohr": 10,
        "greenhouse": 30,
        "ashby": 5,
        "lever": 30,
        "workday": 50,
        "icims": 30,
        "paylocity": 5,
    }

    max_workers = MAX_WORKERS.get(platform_lower, 30)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetcher, slug): slug for slug in live_companies}

        for i, future in enumerate(as_completed(futures), 1):

            # fetcher returns slug, jobs, status_code (if implemented)
            slug, jobs, status_code = future.result()

            if jobs:
                all_jobs.extend(jobs)
                active_companies[slug] = len(jobs)
                print(f"  [{i}/{len(live_companies)}] {slug}: {len(jobs)} jobs")
            else:
                failed += 1
                # Only cache permanent failures
                if status_code in (404, 410):
                    new_dead.add(slug)
                if i % 50 == 0:
                    print(
                        f"  [{i}/{len(live_companies)}] Checked... ({failed} inactive)"
                    )

    # Update dead slug cache
    if new_dead:
        all_dead = dead_slugs | new_dead
        save_dead_slugs(platform_lower, all_dead)

    print(f"\nDETAILED STATS FOR {platform}:")
    print(f"  Companies checked: {len(live_companies)}")
    print(f"  Companies with jobs: {len(active_companies)}")
    print(f"  Failed/empty: {failed}")
    print(f"  Newly dead: {len(new_dead)}")
    print(f"  Total jobs: {len(all_jobs)}")

    return active_companies, all_jobs


# ============================================================
# Helper Functions
# ============================================================
def is_recruiter_company(slug):
    slug = slug.lower()

    # Keyword-based detection
    if any(term in slug for term in RECRUITER_TERMS):
        return True

    return False


def clean_job_data(jobs):
    """Remove invalid/useless job entries."""
    cleaned = []
    skipped_reasons = {"no_title": 0, "no_url": 0, "no_company": 0}

    for job in jobs:
        title = (job.get("title") or "").strip().lower()
        url = job.get("url") or job.get("absolute_url")
        company = job.get("company") or job.get("company_slug")

        # Skip jobs with invalid titles
        if not title or title in ["not specified", "n/a", "unknown", ""]:
            skipped_reasons["no_title"] += 1
            continue

        # Skip jobs without URLs
        if not url:
            skipped_reasons["no_url"] += 1
            continue

        # Skip jobs without company info
        if not company:
            skipped_reasons["no_company"] += 1
            continue

        cleaned.append(job)

    # Print summary
    total_skipped = sum(skipped_reasons.values())
    if total_skipped > 0:
        print(f"\n  Skipped {total_skipped:,} invalid jobs:")
        for reason, count in skipped_reasons.items():
            if count > 0:
                print(f"    - {reason.replace('_', ' ').title()}: {count:,}")

    return cleaned


# module level, compiled once
_TIER_PATTERNS = [
    (re.compile(r"\b(?:chief|cto|ceo|cfo|vp|vice president|director)\b"), 50),
    (re.compile(r"\b(?:principal|distinguished|fellow)\b"), 40),
    (re.compile(r"\b(?:staff|lead|head of)\b"), 30),
    (re.compile(r"\b(?:senior|sr\.?)\b"), 20),
    (re.compile(r"\b(?:architect|manager)\b"), 15),
    (re.compile(r"\b(?:iii|iv|v|vi)\b"), 15),
    (re.compile(r"\blevel\s*[4-9]\b"), 15),
    (re.compile(r"\bengr?\s*[4-6]\b"), 15),
    (re.compile(r"\b(?:counsel|of\s*counsel)\b"), 20),
    (re.compile(r"\b(?:attending|charge)\b"), 20),
    (re.compile(r"\b(?:ii|2)\b"), 5),
    (re.compile(r"\blevel\s*3\b"), 5),
    (re.compile(r"\b(?:associate)\b"), -10),
    (re.compile(r"\b(?:junior|jr\.?)\b"), -20),
    (re.compile(r"\bentry[\s-]?level\b"), -25),
    (re.compile(r"\b(?:i|1)\b(?!\s*-|\d)"), -15),
    (re.compile(r"\b(?:trainee|graduate|new\s*grad)\b"), -25),
    (re.compile(r"\b(?:paralegal|clerk)\b"), -15),
    (re.compile(r"\b(?:resident|clinical\s*fellow)\b"), -15),
    (re.compile(r"\b(?:aide|assistant|tech)\b"), -10),
    (re.compile(r"\bintern(?:ship)?\b"), -100),
]


def job_tier_classification(title):
    title_lower = title.lower()
    score = 0
    for pattern, weight in _TIER_PATTERNS:
        if pattern.search(title_lower):
            score += weight
    if score <= -50:
        return "intern"
    elif score <= -5:
        return "entry"
    elif score >= 15:
        return "senior"
    else:
        return "mid"


# ============================================================
# DEAD SLUG CACHE
# ============================================================

DEAD_SLUG_DIR = os.path.join(ROOT_DIR, "data", "dead_slugs")
os.makedirs(DEAD_SLUG_DIR, exist_ok=True)


def load_dead_slugs(platform):
    """Load cached dead slugs for a platform."""
    filepath = os.path.join(DEAD_SLUG_DIR, f"{platform}.json")
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError):
        return set()


def save_dead_slugs(platform, slugs):
    """Save dead slugs for a platform."""
    filepath = os.path.join(DEAD_SLUG_DIR, f"{platform}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(sorted(slugs), f, ensure_ascii=False, indent=2)
    print(f"  Cached {len(slugs):,} dead slugs for {platform}")


# ============================================================
# SAVE RESULTS
# ============================================================
def save_results(all_companies, active_companies, all_jobs):
    """Save all data to JSON files."""
    print("=" * 80)
    print("SAVING RESULTS")
    print("=" * 80 + "\n")

    original_count = len(all_jobs)
    all_jobs = clean_job_data(all_jobs)
    cleaned_count = original_count - len(all_jobs)
    print(f"Removed {cleaned_count:,} invalid jobs (blank/not specified titles)")

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Save all companies list
    companies_file = os.path.join(OUTPUT_DIR, "all_companies.json")
    with open(companies_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(all_companies)), f, ensure_ascii=False, indent=2)
    print(f"All companies: {companies_file}")

    # Save active companies with job counts
    active_file = os.path.join(OUTPUT_DIR, "active_companies.json")
    with open(active_file, "w", encoding="utf-8") as f:
        json.dump(active_companies, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"Active companies: {active_file}")

    # Load salary lookup once
    salary_lookup_path = os.path.join(ROOT_DIR, "data", "salary", "salary_lookup.json")
    salary_lookup = {}
    salary_fallback = {}
    if os.path.exists(salary_lookup_path):
        with open(salary_lookup_path, encoding="utf-8") as f:
            data = json.load(f)
            salary_lookup = data.get("primary", {})
            salary_fallback = data.get("fallback", {})
        print(f"Loaded {len(salary_lookup):,} salary entries")

    # Enrich jobs with salary data
    for job in all_jobs:
        company = (job.get("company") or "").lower().strip()
        title = (job.get("title") or "").lower().strip()
        level = job.get("skill_level", "mid")

        primary_key = f"{company}|{title}|{level}"
        fallback_key = f"{title}|{level}"

        job["salary"] = salary_lookup.get(primary_key) or salary_fallback.get(
            fallback_key
        )

    # Save all jobs
    all_jobs_file = os.path.join(OUTPUT_DIR, "all_jobs.json")
    with open(all_jobs_file, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, ensure_ascii=False, indent=2)
    print(f"All jobs: {all_jobs_file} ({len(all_jobs):,} jobs)")

    # Build slim version for frontend
    FRONTEND_FIELDS = {
        "title",
        "company",
        "location",
        "url",
        "ats",
        "skill_level",
        "is_recruiter",
        "workplaceType",
        "scraped_at",
        "remote",
        "coords",
        "salary",
        "updated_at",
        "first_seen",
    }

    slim_jobs = [
        {k: job.get(k) for k in FRONTEND_FIELDS if k in job} for job in all_jobs
    ]

    # Pre-sort by company name for better frontend caching
    slim_jobs.sort(
        key=lambda x: (x.get("company", "").lower(), x.get("title", "").lower())
    )

    # Chunks go in a subdirectory to keep the output folder organized
    chunks_dir = os.path.join(OUTPUT_DIR, "chunks")
    os.makedirs(chunks_dir, exist_ok=True)

    # Remove old chunk files to prevent confusion and save space
    for old_chunk in os.listdir(chunks_dir):
        if old_chunk.startswith("jobs_chunk_") and old_chunk.endswith(".json.gz"):
            os.remove(os.path.join(chunks_dir, old_chunk))

    # Split into chunks of ~25k for frontend loading (with gzip compression)
    CHUNK_SIZE = 25_000

    chunks = [
        slim_jobs[i : i + CHUNK_SIZE] for i in range(0, len(slim_jobs), CHUNK_SIZE)
    ]

    chunk_filenames = []
    for idx, chunk in enumerate(chunks):
        chunk_file = os.path.join(chunks_dir, f"jobs_chunk_{idx}.json.gz")
        with gzip.open(chunk_file, "wt", encoding="utf-8") as f:
            json.dump(chunk, f, ensure_ascii=False, indent=0)
        chunk_filenames.append(f"jobs_chunk_{idx}.json.gz")
        size_mb = os.path.getsize(chunk_file) / (1024 * 1024)
        print(f"  Chunk {idx}: {len(chunk):,} jobs ({size_mb:.1f}MB)")

    # Manifest so the frontend knows what to load
    manifest = {
        "chunks": chunk_filenames,
        "totalJobs": len(slim_jobs),
        "last_updated": timestamp,
    }
    manifest_file = os.path.join(chunks_dir, "jobs_manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    recruiter_jobs = sum(1 for job in all_jobs if job.get("is_recruiter"))

    # Save metadata summary
    metadata = {
        "last_updated": timestamp,
        "total_companies": len(all_companies),
        "active_companies": len(active_companies),
        "total_jobs": len(all_jobs),
        "recruiter_jobs": recruiter_jobs,
        "source_type": SOURCE_TYPE,
        "platforms": "greenhouse_api, ashby_api, bamboohr_api, lever_api, workday_api, icims_sitemap, paylocity_scrape, workable_feed, join_graphql",
    }

    metadata_file = os.path.join(OUTPUT_DIR, "metadata.json")
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"Metadata: {metadata_file}")

    print()


def main():
    print("\n" + "=" * 80)
    print("JOB BOARD AGGREGATOR")
    print("Scraping all jobs from ATS companies")
    print("=" * 80)

    # Load existing companies
    greenhouse_companies = load_companies(GREENHOUSE_FILE)
    ashby_companies = load_companies(ASHBY_FILE)
    bamboohr_companies = load_companies(BAMBOOHR_FILE)
    lever_companies = load_companies(LEVER_FILE)
    workday_companies = load_companies(WORKDAY_FILE)
    icims_companies = load_companies(ICIMS_FILE)
    paylocity_companies = load_paylocity(PAYLOCITY_FILE)

    if (
        not greenhouse_companies
        and not ashby_companies
        and not bamboohr_companies
        and not lever_companies
        and not workday_companies
        and not icims_companies
        and not paylocity_companies
    ):
        print("Exiting - no companies loaded!")
        return

    # Define all platform jobs
    platforms = [
        (greenhouse_companies, fetch_company_jobs_greenhouse, "GREENHOUSE"),
        (ashby_companies, fetch_company_jobs_ashby, "ASHBY"),
        (bamboohr_companies, fetch_company_jobs_bamboohr, "BAMBOOHR"),
        (lever_companies, fetch_company_jobs_lever, "LEVER"),
        (workday_companies, fetch_company_jobs_workday, "WORKDAY"),
        (icims_companies, fetch_company_jobs_icims, "iCIMS"),
        (paylocity_companies, fetch_company_jobs_paylocity, "PAYLOCITY"),
    ]

    # Run all platforms concurrently
    all_active_companies = {}
    all_jobs = []
    workable_companies = set()
    join_companies = set()

    # Workable has no company list: one feed carries every company's postings.
    # Join reads its company list from Join's sitemap, not from a file.
    with ThreadPoolExecutor(max_workers=len(platforms) + 2) as platform_executor:
        futures = {
            platform_executor.submit(fetch_all_jobs, companies, fetcher, name): name
            for companies, fetcher, name in platforms
        }
        futures[platform_executor.submit(fetch_workable_jobs)] = "WORKABLE"
        futures[platform_executor.submit(fetch_join_jobs)] = "JOIN"

        for future in as_completed(futures):
            name = futures[future]
            active, jobs = future.result()
            if name == "WORKABLE":
                workable_companies = set(active)
            if name == "JOIN":
                join_companies = set(active)
            all_active_companies.update(active)
            all_jobs.extend(jobs)
            print(
                f"\n  >>> {name} COMPLETE: {len(active):,} active, {len(jobs):,} jobs <<<\n"
            )

    # Combine all company sets for total count
    all_companies = (
        greenhouse_companies
        | ashby_companies
        | bamboohr_companies
        | lever_companies
        | workday_companies
        | icims_companies
        | paylocity_companies
        | workable_companies
        | join_companies
    )

    save_results(all_companies, all_active_companies, all_jobs)

    # Final summary
    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(f"Total companies:   {len(all_companies):,}")
    print(f"Active companies:  {len(all_active_companies):,}")
    print(f"Total jobs:        {len(all_jobs):,}")
    print(f"\nAll data saved to '{OUTPUT_DIR}/' directory")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Board Aggregator Scraper")
    parser.add_argument(
        "--source",
        choices=["automated", "manual"],
        default="automated",
        help="Source type: automated (GitHub Actions) or manual (local run)",
    )

    args = parser.parse_args()
    SOURCE_TYPE = args.source

    print(f"\nRunning in {SOURCE_TYPE.upper()} mode\n")

    main()
