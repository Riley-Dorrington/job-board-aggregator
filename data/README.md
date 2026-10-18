# job-board-data

Generated data store for [job-board-aggregator](https://github.com/Riley-Dorrington/job-board-aggregator).

This repo holds the chunked, gzipped job data (`data/chunks/*.json.gz` plus
`jobs_manifest.json`) served to the live site over GitHub Pages.

**This repo is machine-generated.** Every run of the aggregator's daily workflow
force-pushes a fresh orphan commit, replacing the entire contents. There is no
meaningful history, and pull requests or manual edits will be overwritten on the
next run. File issues on the main repo instead.
