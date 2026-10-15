# Issue tracker: Local markdown

Issues live as files under `.scratch/<feature-slug>/` in this repo.

- New feature work: `.scratch/<feature-slug>/issues/<NN>-<slug>.md` (numbered from `01` in dependency order, blockers first).
- Specs / notes: `.scratch/<feature-slug>/` siblings as needed.
- Status field in file (e.g. `Status: ready-for-agent`) carries the triage state; see `triage-labels.md`.
- No `gh`/`glab` calls; all reads/writes are local files.
- `to-tickets` publishes one file per ticket using its local-ticket template. `to-spec` writes its spec here.
