# skegdb.github.io

Static status dashboard for the skegdb ecosystem.

Live at https://skegdb.github.io.

## What it shows

For each public package:
- live CI sparkline (last 20 runs on default branch)
- latest release with SHA256 sidecars surfaced inline
- registry versions (crates.io, PyPI, brew tap)
- install commands ready to copy
- last failure detail (which job, which step)

## Architecture

Static site built with Astro. No client framework, no tracking.

```
.github/workflows/snapshot.yml   hourly cron → scripts/snapshot.mjs
                                 → src/data/snapshots.jsonl (committed)

.github/workflows/deploy.yml     on push to main → astro build → Pages
```

`scripts/snapshot.mjs` pulls from:
- GitHub REST (workflow runs, releases, release assets, sha256 sidecars)
- crates.io
- pypi.org JSON
- raw Formula/skeg.rb on homebrew-tap

Each snapshot appends one record per package to `src/data/snapshots.jsonl`.
The build reads the file and renders cards + drawers at SSG time.

## Add a package

Edit `src/data/repos.json`. Re-deploy. Snapshot will pick it up on next cron.

## Local dev

```sh
npm install
npm run snapshot   # fetches current state, appends to snapshots.jsonl
npm run dev        # http://localhost:4321
```

`GITHUB_TOKEN` is optional locally (raises rate limit). Generate one with `repo` read scope if you hit 403s.

## License

Apache-2.0.
