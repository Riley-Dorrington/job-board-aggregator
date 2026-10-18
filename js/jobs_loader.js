// ============================================================
// JOBS LOADER
// ============================================================

import { enableMap } from "./map_view.js";

/**
 * Fetch and decompress a single gzipped JSON file.
 * @param {string} url - Path to the .json.gz file
 * @returns {Promise<Array>} Parsed JSON array
 */
export async function fetchAndDecompress(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Failed to load ${url}`);

    const blob = await response.blob();
    const ds = new DecompressionStream('gzip');

    const text = await new Response(blob.stream().pipeThrough(ds))
        .blob()
        .then(b => b.text());

    return JSON.parse(text);
}

/**
 * Fetch and decompress a single gzipped JSON file.
 * @param {string} url - Path to the .json.gz file
 * @returns {Promise<Array>} Parsed JSON array
 */
export async function loadJobsProgressive(app, basePath = 'https://riley-dorrington.github.io/job-board-data/data/chunks') {
    document.querySelector('.job-table thead')?.classList.add('sorting-locked');

    const base_url = basePath;

    const manifest = await fetch(`${base_url}/jobs_manifest.json?t=${Date.now()}`).then(res => {
        if (!res.ok) throw new Error('Failed to load jobs manifest');
        return res.json();
    });

    const v = encodeURIComponent(manifest.last_updated);

    const firstChunk = await fetchAndDecompress(`${base_url}/${manifest.chunks[0]}?v=${v}`);
    app.allJobs = firstChunk;
    app.filteredJobs = firstChunk;
    updateStats(app.allJobs, manifest.last_updated);
    app.render();

    if (manifest.chunks.length <= 1) {
        app.isFullyLoaded = true;
        document.querySelector('.job-table thead')?.classList.remove('sorting-locked');
        enableMap();
        return;
    }

    const worker = new Worker('./js/chunk_worker.js', { type: 'module' });
    app.sortWorker = worker;
    let pending = manifest.chunks.length - 1;

    worker.onmessage = ({ data }) => {
        if (data.type === 'CHUNK_LOADED') {
            app.allJobs.push(...data.jobsChunk);
            app.refilter();
            updateStats(app.allJobs, manifest.last_updated);
            if (--pending === 0) {
                app.isFullyLoaded = true;
                document.querySelector('.job-table thead')?.classList.remove('sorting-locked');
                enableMap();
                console.log("All chunks successfully loaded.");
            }
        }
        if (data.type === 'SORTED') {
            app.sortedJobs = data.sortedJobs;
            app.virtualFilteredCount = data.sortedJobs.length;
            if (app.sortLoader?.hide) app.sortLoader.hide();
            app.isSorting = false;
            app.render();
        }
        if (data.type === 'SORT_ERROR') {
            console.error('Worker sort failed:', data.message);
            if (app.sortLoader?.hide) app.sortLoader.hide();
            app.isSorting = false;
        }
    };

    manifest.chunks.slice(1).forEach(chunk => {
        worker.postMessage({ type: 'FETCH_CHUNK', url: `${base_url}/${chunk}?v=${v}` });
    });
}

/**
 * Update the stats bar in the DOM.
 * @param {Array} jobs - The full jobs array
 * @param {string} [lastUpdated] - ISO timestamp from manifest
 */
export function updateStats(jobs, lastUpdated) {
    const companies = new Set(jobs.map(j => j.company_slug || j.company)).size;
    document.getElementById('total-jobs').textContent = jobs.length.toLocaleString();
    document.getElementById('total-companies').textContent = companies.toLocaleString();
    document.getElementById('last-updated').textContent = lastUpdated
        ? new Date(lastUpdated).toLocaleDateString()
        : new Date().toLocaleDateString();
}