// ============================================================
// COLUMN CONFIGURATION
// ============================================================

import { escape } from './ui_utils.js';
import { loadApplicationStatus } from './storage.js';

/** Build and return the column definitions for the job table */
export function createColumns() {
    return [
        { key: 'company', label: 'Company', sortable: false },
        { key: 'title', label: 'Title', sortable: false },
        { key: 'location', label: 'Location', sortable: false },
        {
            key: 'salary',
            label: 'Salary (est.)',
            sortable: true,
            render: job => {
                const s = job.salary;
                if (!s?.median) return '<span class="text-muted">—</span>';
                const fmt = n => '$' + (n / 1000).toFixed(0) + 'k';
                return `<span title="p25: ${fmt(s.p25)} / p75: ${fmt(s.p75)} (n=${s.n})">${fmt(s.median)}</span>`;
            }
        },
        {
            key: 'ats',
            label: 'ATS',
            sortable: false,
            render: job => {
                const ats = job.ats || 'unknown';
                const classes = {
                    'greenhouse': 'ats-greenhouse',
                    'lever': 'ats-lever',
                    'workday': 'ats-workday',
                    'ashby': 'ats-ashby',
                    'icims': 'ats-icims',
                    'bamboohr': 'ats-bamboohr',
                    'workable': 'ats-workable',
                    'paylocity': 'ats-paylocity',
                    'join': 'ats-join',
                };
                const cls = classes[ats.toLowerCase()] || 'ats-unknown';
                return `<span class="badge ${cls}">${escape(ats)}</span>`;
            }
        },
        {
            key: 'url',
            label: 'Apply',
            sortable: false,
            render: job => {
                const url = job.absolute_url || job.url;
                return url
                    ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-primary">Apply</a>`
                    : 'N/A';
            }
        },
        {
            key: 'posted',
            label: 'Posted',
            sortable: true,
            render: job => {
                const raw = job.updated_at || job.first_seen;
                if (!raw) return '<span class="text-muted">—</span>';
                const d = new Date(raw);
                if (isNaN(d.getTime())) return '<span class="text-muted">—</span>';
                const days = Math.floor((Date.now() - d) / 86400000);
                if (days === 0) return 'Today';
                if (days === 1) return 'Yesterday';
                if (days < 30) return `${days}d ago`;
                return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            }
        },
        {
            key: 'actions',
            label: 'Actions',
            sortable: false,
            render: job => {
                const url = job.absolute_url || job.url;
                return `
                    <div class="btn-group" role="group">
                        <input type="checkbox" class="btn-check save-checkbox"
                               id="save-${escape(url)}"
                               data-job-url="${escape(url)}">
                        <label class="btn btn-sm btn-outline-primary" for="save-${escape(url)}">Saved</label>

                        <input type="checkbox" class="btn-check apply-checkbox"
                               id="apply-${escape(url)}"
                               data-job-url="${escape(url)}">
                        <label class="btn btn-sm btn-outline-success" for="apply-${escape(url)}">Applied</label>

                        <input type="checkbox" class="btn-check ignored-checkbox"
                               id="ignore-${escape(url)}"
                               data-job-url="${escape(url)}">
                        <label class="btn btn-sm btn-outline-secondary" for="ignore-${escape(url)}">Ignored</label>
                    </div>`;
            }
        }
    ];
}