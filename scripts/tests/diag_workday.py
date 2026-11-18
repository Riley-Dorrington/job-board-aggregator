"""Temporary diagnostic: what makes Workday answer 429? Run once, then delete."""
import re, time, requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
TENANTS = ["abbott|wd5|abbottcareers", "accenture|wd1|accenturecareers",
           "lowes|wd5|LWS_External_CS", "pwc|wd3|Global_Experienced_Careers",
           "nvidia|wd5|NVIDIAExternalCareerSite"]
PAYLOAD = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

def show(label, r):
    keep = {k: v for k, v in r.headers.items()
            if k.lower() in ("retry-after", "server", "via", "x-cache", "content-type", "x-wd-request-id", "akamai-grn", "x-envoy-upstream-service-time")
            or k.lower().startswith(("cf-", "x-amz", "x-ratelimit", "ratelimit"))}
    text = " ".join(re.sub(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>|<[^>]+>", " ", r.text).split())[:300]
    jobs = ""
    if "json" in r.headers.get("Content-Type", ""):
        try: jobs = f" total={r.json().get('total')}"
        except ValueError: pass
    print(f"{label}: HTTP {r.status_code}{jobs} {keep} | {text}", flush=True)

print("runner IP:", requests.get("https://api.ipify.org", timeout=10).text)
for t in TENANTS:
    company, wd, site = t.split("|")
    base = f"https://{company}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{company}/{site}/jobs"
    print(f"\n=== {t}")
    try:
        show("A plain POST (scraper headers)", requests.post(api, json=PAYLOAD, timeout=30, headers={
            "Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA,
            "Origin": base, "Referer": f"{base}/{site}"}))
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        r = s.get(f"{base}/{site}", timeout=30)
        show("B GET careers page", r)
        print("   cookies:", sorted(s.cookies.keys()))
        show("C POST with session cookies", s.post(api, json=PAYLOAD, timeout=30, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": base, "Referer": f"{base}/{site}"}))
        show("D GET robots.txt", requests.get(f"{base}/robots.txt", timeout=30, headers={"User-Agent": UA}))
    except requests.RequestException as e:
        print("   error:", e)
    time.sleep(2)
