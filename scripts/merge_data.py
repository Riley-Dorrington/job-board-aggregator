import json
import gzip
import os
import re
from pathlib import Path
from datetime import datetime, timezone

CHUNK_SIZE = 25_000


def get_dedup_key(job):
    url = job.get("url", "")
    ats = job.get("ats")
    if ats == "Workday":
        match = re.search(r'/jobs/(\d+)', url)
        if match:
            company = job.get("company", "")
            return f"workday:{company}:{match.group(1)}"
    if ats == "Paylocity":
        jid = job.get("id")
        if jid:
            return f"paylocity:{jid}"
        # no job_id: key on company+title so they don't collapse to one listing URL
        return f"paylocity:{job.get('company','')}:{job.get('title','')}"
    if ats == "Join":
        # https://join.com/companies/<slug>/<job id>: the chunks carry no "id"
        # field, so the id is read from the URL. It stays the same for the
        # posting's whole life, also when the company slug changes.
        match = re.search(r'/companies/[^/]+/(\d+)$', url)
        if match:
            return f"join:{match.group(1)}"
    return url


def load_chunks(directory):
    """Load all jobs from chunked gzip files via manifest."""
    chunks_dir = Path(directory) / "chunks"
    manifest_path = chunks_dir / "jobs_manifest.json"
    if not manifest_path.exists():
        return []
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    jobs = []
    for chunk_file in manifest["chunks"]:
        chunk_path = chunks_dir / chunk_file
        if chunk_path.exists():
            with gzip.open(chunk_path, "rt", encoding="utf-8") as f:
                jobs.extend(json.load(f))
    return jobs


def save_chunks(jobs, directory, timestamp):
    """Write chunked gzip files + manifest."""
    chunks_dir = Path(directory) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Clean old chunks
    for f in os.listdir(chunks_dir):
        if f.startswith("jobs_chunk_") and f.endswith(".json.gz"):
            os.remove(chunks_dir / f)

    # Sort consistently
    jobs.sort(key=lambda x: (x.get('company', '').lower(), x.get('title', '').lower()))
    chunks = [jobs[i:i + CHUNK_SIZE] for i in range(0, len(jobs), CHUNK_SIZE)]

    chunk_filenames = []
    for idx, chunk in enumerate(chunks):
        chunk_file = f"jobs_chunk_{idx}.json.gz"
        with gzip.open(chunks_dir / chunk_file, "wt", encoding="utf-8") as f:
            json.dump(chunk, f, ensure_ascii=False, indent=0)
        chunk_filenames.append(chunk_file)
        print(f"  Chunk {idx}: {len(chunk):,} jobs")

    manifest = {
        "chunks": chunk_filenames,
        "totalJobs": len(jobs),
        "last_updated": timestamp,
    }
    with open(chunks_dir / "jobs_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def merge_job_data():
    """Merge new scrape with existing data, removing stale jobs."""
    new_jobs = load_chunks("scripts/output")
    print(f"New scrape: {len(new_jobs):,} jobs")

    existing_jobs = load_chunks("data")
    print(f"Existing data: {len(existing_jobs):,} jobs")

    # Merge by dedup key
    merged = {}
    stale_count = 0
    for job in existing_jobs:
        key = get_dedup_key(job)
        if not key:
            continue
        scraped = job.get("scraped_at")
        if scraped:
            try:
                scraped_date = datetime.fromisoformat(scraped.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - scraped_date).days
                if age_days <= 30:
                    merged[key] = job
                else:
                    stale_count += 1
            except Exception:
                merged[key] = job
        else:
            merged[key] = job

    if stale_count > 0:
        print(f"Dropped {stale_count:,} stale jobs (>30 days old)")
    
    # New scrape always wins on duplicates; preserve first_seen across runs
    for job in new_jobs:
        key = get_dedup_key(job)
        if key:
            existing = merged.get(key)
            if existing:
                job["first_seen"] = existing.get("first_seen") or existing.get("scraped_at")
            else:
                job["first_seen"] = job.get("scraped_at")
            merged[key] = job

    final_jobs = list(merged.values())
    print(f"Merged result: {len(final_jobs):,} jobs")

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    save_chunks(final_jobs, "data", timestamp)

    # Update metadata
    with open("scripts/output/metadata.json", encoding="utf-8") as f:
        metadata = json.load(f)
    metadata["total_jobs"] = len(final_jobs)
    with open("data/metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print("Merge complete")
    return len(final_jobs)


if __name__ == "__main__":
    merge_job_data()