import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import reposJson from '../data/repos.json';

// Resolved from project root: stable across dev + SSG build.
const SNAPSHOT_PATH = join(process.cwd(), 'src/data/snapshots.jsonl');

export type RegistryRef =
  | { kind: 'crates'; name: string; entryKind?: 'library' | 'binary' }
  | { kind: 'pypi'; name: string }
  | { kind: 'brew'; tap: string; formula: string };

export type Package = {
  id: string;
  name: string;
  repo: string;
  tagline: string;
  category: 'engine' | 'client' | 'tool' | 'adapter' | 'infra';
  install: { label: string; cmd: string }[];
  primaryRegistry: 'crates' | 'pypi' | 'brew' | null;
  registries: {
    crates?: { name: string; kind: string }[];
    pypi?: { name: string }[];
    brew?: { tap: string; formula: string };
  };
};

export type Snapshot = {
  ts: string;
  id: string;
  repo: string;
  github?: {
    default_branch: string;
    stargazers: number;
    open_issues: number;
    updated_at: string;
    runs: Run[];
    last_failed_detail?: {
      run_id: number;
      jobs?: { name: string; html_url: string; first_failed_step: string | null }[];
      error?: string;
    } | null;
    latest_release?: Release | null;
  };
  github_error?: string;
  registries: {
    crates?: CrateInfo[];
    pypi?: PypiInfo[];
    brew?: BrewInfo;
    crates_errors?: { name: string; error: string }[];
    pypi_errors?: { name: string; error: string }[];
    brew_error?: string;
  };
};

export type Run = {
  id: number;
  name: string;
  workflow: string | null;
  event: string;
  status: string;
  conclusion: string | null;
  head_sha: string;
  head_commit_message: string | null;
  run_number: number;
  html_url: string;
  created_at: string;
  updated_at: string;
};

export type Release = {
  tag: string;
  name: string;
  published_at: string;
  html_url: string;
  body: string | null;
  assets: { name: string; size: number; download_url: string; sha256: string | null }[];
};

export type CrateInfo = {
  name: string;
  max_version: string;
  downloads_total: number;
  recent_downloads: number;
  updated_at: string;
  homepage: string | null;
  documentation: string | null;
};

export type PypiInfo = {
  name: string;
  version: string;
  summary: string | null;
  home_page: string | null;
  requires_python: string | null;
  upload_time: string | null;
};

export type BrewInfo = {
  tap: string;
  formula: string;
  version: string | null;
  raw_url: string;
};

export function getPackages(): Package[] {
  return reposJson.packages as Package[];
}

export function getLatestSnapshots(): Map<string, Snapshot> {
  const byId = new Map<string, Snapshot>();
  if (!existsSync(SNAPSHOT_PATH)) return byId;
  const text = readFileSync(SNAPSHOT_PATH, 'utf8');
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line) as Snapshot;
      byId.set(rec.id, rec);
    } catch {
      // skip malformed
    }
  }
  return byId;
}

export function getHistory(id: string, n = 60): Snapshot[] {
  if (!existsSync(SNAPSHOT_PATH)) return [];
  const text = readFileSync(SNAPSHOT_PATH, 'utf8');
  const out: Snapshot[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line) as Snapshot;
      if (rec.id === id) out.push(rec);
    } catch {
      // skip
    }
  }
  return out.slice(-n);
}

export function healthStatus(snap?: Snapshot): 'op' | 'deg' | 'down' | 'unknown' {
  if (!snap?.github) return 'unknown';
  const runs = snap.github.runs;
  if (!runs.length) return 'unknown';
  const completed = runs.filter((r) => r.status === 'completed');
  if (!completed.length) return 'unknown';
  const last = completed[0];
  if (last.conclusion === 'success') return 'op';
  // Check the last 10 — if mostly success, degraded; else down
  const window = completed.slice(0, 10);
  const greens = window.filter((r) => r.conclusion === 'success').length;
  if (greens >= window.length * 0.7) return 'deg';
  return 'down';
}

export function shortSha(sha: string, n = 7): string {
  return sha?.slice(0, n) || '';
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatRelTime(iso: string): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const m = Math.round(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.round(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.round(mo / 12)}y ago`;
}
