#!/usr/bin/env node
/**
 * Snapshot collector for skegdb dashboard.
 *
 * Pulls current state from:
 *   - GitHub API: workflow runs, releases, release assets, sha256 sidecars
 *   - crates.io API: latest version, downloads
 *   - pypi.org JSON API: latest version
 *   - homebrew-tap formula file: parsed version
 *
 * Appends one snapshot record per package to src/data/snapshots.jsonl.
 * Records share a top-level "ts" so a single cron run forms a column.
 *
 * Auth: requires GITHUB_TOKEN env (works fine with default in GitHub Actions).
 */

import { readFile, appendFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

const GH_TOKEN = process.env.GITHUB_TOKEN || '';
const UA = 'skegdb-dashboard/1.0 (+https://skegdb.github.io)';

const ghHeaders = {
  'User-Agent': UA,
  'Accept': 'application/vnd.github+json',
  ...(GH_TOKEN ? { 'Authorization': `Bearer ${GH_TOKEN}` } : {}),
};

async function ghJson(path) {
  const url = `https://api.github.com${path}`;
  const res = await fetch(url, { headers: ghHeaders });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`GH ${res.status} ${path}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

async function ghText(url) {
  const res = await fetch(url, { headers: { 'User-Agent': UA } });
  if (!res.ok) return null;
  return res.text();
}

async function cratesJson(name) {
  const res = await fetch(`https://crates.io/api/v1/crates/${name}`, {
    headers: { 'User-Agent': UA, 'Accept': 'application/json' },
  });
  if (!res.ok) return null;
  return res.json();
}

async function pypiJson(name) {
  const res = await fetch(`https://pypi.org/pypi/${name}/json`, {
    headers: { 'User-Agent': UA, 'Accept': 'application/json' },
  });
  if (!res.ok) return null;
  return res.json();
}

async function fetchRepo(repo) {
  // Repo metadata + default branch
  const meta = await ghJson(`/repos/${repo}`);

  // Last 20 CI runs across all workflows on the default branch
  const runs = await ghJson(
    `/repos/${repo}/actions/runs?per_page=20&branch=${encodeURIComponent(meta.default_branch)}`,
  );
  const runList = (runs.workflow_runs || []).map((r) => ({
    id: r.id,
    name: r.name,
    workflow: r.path?.split('/').pop() || null,
    event: r.event,
    status: r.status,
    conclusion: r.conclusion,
    head_sha: r.head_sha,
    head_commit_message: r.head_commit?.message?.split('\n')[0] || null,
    run_number: r.run_number,
    html_url: r.html_url,
    created_at: r.created_at,
    updated_at: r.updated_at,
  }));

  // For the most-recent failed run, expand jobs+steps to surface where it broke
  let failedDetail = null;
  const lastFail = runList.find((r) => r.conclusion === 'failure');
  if (lastFail) {
    try {
      const jobs = await ghJson(`/repos/${repo}/actions/runs/${lastFail.id}/jobs`);
      const failedJobs = (jobs.jobs || [])
        .filter((j) => j.conclusion === 'failure')
        .map((j) => ({
          name: j.name,
          html_url: j.html_url,
          first_failed_step:
            j.steps?.find((s) => s.conclusion === 'failure')?.name || null,
        }));
      failedDetail = { run_id: lastFail.id, jobs: failedJobs };
    } catch (e) {
      failedDetail = { run_id: lastFail.id, error: String(e.message || e) };
    }
  }

  // Latest release + assets + sha256 sidecars
  let latestRelease = null;
  try {
    const rel = await ghJson(`/repos/${repo}/releases/latest`);
    const assets = await Promise.all(
      (rel.assets || []).map(async (a) => {
        let sha256 = null;
        if (a.name.endsWith('.sha256')) return null; // skip sidecars themselves
        const sidecar = (rel.assets || []).find((x) => x.name === `${a.name}.sha256`);
        if (sidecar) {
          const text = await ghText(sidecar.browser_download_url);
          // sha256 file format: "<hex>  <filename>\n" or just "<hex>\n"
          sha256 = text ? text.trim().split(/\s+/)[0] : null;
        }
        return {
          name: a.name,
          size: a.size,
          download_url: a.browser_download_url,
          sha256,
        };
      }),
    );
    latestRelease = {
      tag: rel.tag_name,
      name: rel.name,
      published_at: rel.published_at,
      html_url: rel.html_url,
      body: rel.body?.slice(0, 2000) || null,
      assets: assets.filter(Boolean),
    };
  } catch (e) {
    // 404 = no release yet
  }

  return {
    default_branch: meta.default_branch,
    stargazers: meta.stargazers_count,
    open_issues: meta.open_issues_count,
    updated_at: meta.updated_at,
    runs: runList,
    last_failed_detail: failedDetail,
    latest_release: latestRelease,
  };
}

async function fetchCrate(name) {
  const data = await cratesJson(name);
  if (!data?.crate) return null;
  return {
    name,
    max_version: data.crate.max_version,
    downloads_total: data.crate.downloads,
    recent_downloads: data.crate.recent_downloads,
    updated_at: data.crate.updated_at,
    homepage: data.crate.homepage,
    documentation: data.crate.documentation,
  };
}

async function fetchPypi(name) {
  const data = await pypiJson(name);
  if (!data?.info) return null;
  return {
    name,
    version: data.info.version,
    summary: data.info.summary,
    home_page: data.info.project_urls?.Homepage || data.info.home_page || null,
    requires_python: data.info.requires_python,
    upload_time: data.urls?.[0]?.upload_time_iso_8601 || null,
  };
}

async function fetchBrew(tap, formula) {
  // homebrew-tap has Formula/<formula>.rb. Parse the `version` line.
  const [owner, name] = tap.split('/');
  const url = `https://raw.githubusercontent.com/${owner}/homebrew-${name}/main/Formula/${formula}.rb`;
  const text = await ghText(url);
  if (!text) return null;
  const m = text.match(/version\s+"([^"]+)"/);
  return { tap, formula, version: m?.[1] || null, raw_url: url };
}

async function collect() {
  const repos = JSON.parse(await readFile(join(ROOT, 'src/data/repos.json'), 'utf8'));
  const ts = new Date().toISOString();
  const out = [];

  for (const pkg of repos.packages) {
    const record = { ts, id: pkg.id, repo: pkg.repo };
    try {
      record.github = await fetchRepo(pkg.repo);
    } catch (e) {
      record.github_error = String(e.message || e);
    }

    record.registries = {};

    for (const c of pkg.registries.crates || []) {
      try {
        const r = await fetchCrate(c.name);
        if (r) (record.registries.crates ||= []).push(r);
      } catch (e) {
        (record.registries.crates_errors ||= []).push({ name: c.name, error: String(e) });
      }
    }

    for (const p of pkg.registries.pypi || []) {
      try {
        const r = await fetchPypi(p.name);
        if (r) (record.registries.pypi ||= []).push(r);
      } catch (e) {
        (record.registries.pypi_errors ||= []).push({ name: p.name, error: String(e) });
      }
    }

    if (pkg.registries.brew) {
      try {
        const b = await fetchBrew(pkg.registries.brew.tap, pkg.registries.brew.formula);
        if (b) record.registries.brew = b;
      } catch (e) {
        record.registries.brew_error = String(e);
      }
    }

    out.push(record);
    console.log(`[snapshot] ${pkg.id}: ${record.github?.runs?.length || 0} runs, ` +
      `${record.github?.latest_release?.tag || 'no release'}`);
  }

  const path = join(ROOT, 'src/data/snapshots.jsonl');
  const lines = out.map((r) => JSON.stringify(r)).join('\n') + '\n';
  await appendFile(path, lines, 'utf8');
  console.log(`[snapshot] wrote ${out.length} records to ${path}`);
}

collect().catch((e) => {
  console.error(e);
  process.exit(1);
});
