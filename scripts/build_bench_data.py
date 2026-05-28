#!/usr/bin/env python3
"""Build static JSON aggregates from bench-compare parquet outputs.

Outputs go to ``src/data/bench/*.json`` and are imported by Astro pages
at build time. The browser never touches the parquet directly — these
JSONs are small enough to embed in the page.

Slice D (co-residence):
  - aggregates per (backend, corpus_size): median tps, retrieval p95,
    embed p95, gen_ms median, plus system pressure summaries derived
    from samples.parquet windows aligned to each run.

Run:

    python scripts/build_bench_data.py \\
        --bench-dir /path/to/bench-compare \\
        --out src/data/bench
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]. Returns NaN on empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return float('nan')
    if len(vals) == 1:
        return float(vals[0])
    pos = q * (len(vals) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(vals) - 1)
    frac = pos - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def median(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else float('nan')


def aggregate_slice_d(bench_dir: Path) -> dict:
    """Build slice-D JSON.

    Reads:
        slice-d/turns.parquet          (chroma + baseline canonical)
        slice-d/turns-rerun.parquet    (skeg + qdrant, if present)
        slice-d/samples.parquet        (system rows window-aligned to turns)
        slice-d/samples-rerun.parquet  (system rows for rerun, if present)

    The rerun parquet, if present, *overrides* skeg + qdrant rows in the
    canonical turns.parquet — newer measurements win, while chroma and
    baseline keep their canonical numbers.
    """
    out: dict = {
        'slice': 'D',
        'name': 'co-residence',
        'description': (
            'LLM throughput and system memory pressure when the vector store '
            'and the LLM share the same machine. Single-machine M1 16 GB.'
        ),
        'methodology_path': 'bench-compare/SLICE-D-METHODOLOGY.md',
        'results_path': 'bench-compare/SLICE-D-RESULTS.md',
    }

    sd = bench_dir / 'slice-d'
    if not sd.exists():
        out['available'] = False
        out['note'] = 'slice-d/ not found in bench-compare yet'
        return out

    turns_canonical = sd / 'turns.parquet'
    turns_rerun = sd / 'turns-rerun.parquet'
    samples_canonical = sd / 'samples.parquet'
    samples_rerun = sd / 'samples-rerun.parquet'

    if not turns_canonical.exists() and not turns_rerun.exists():
        out['available'] = False
        out['note'] = 'no turns parquet present'
        return out

    # Load turns. Apply rerun-override semantics for skeg/qdrant.
    # Be tolerant of in-progress writes: a rerun parquet that is being
    # written has no footer yet and pyarrow will raise ArrowInvalid.
    def _safe_read(p: Path):
        try:
            return pq.read_table(p)
        except Exception as e:
            print(f'[bench-data] skipping unreadable {p.name}: {e!s:.120}')
            return None

    turns_tables = []
    rerun_table = _safe_read(turns_rerun) if turns_rerun.exists() else None
    if turns_canonical.exists():
        canonical = _safe_read(turns_canonical)
        if canonical is not None:
            if rerun_table is not None:
                # drop skeg + qdrant rows from canonical, keep chroma + baseline
                mask = pc.is_in(canonical.column('backend'),
                                pa.array(['chroma', 'baseline']))
                canonical = canonical.filter(mask)
            turns_tables.append(canonical)
    if rerun_table is not None:
        turns_tables.append(rerun_table)

    if not turns_tables:
        out['available'] = False
        out['note'] = 'no readable turns parquet present yet'
        return out

    turns = pa.concat_tables(turns_tables, promote_options='default')
    turns_data = turns.to_pylist()

    out['n_turns_total'] = len(turns_data)

    # Aggregate per (backend, corpus_size). Skip turn_id=0 to drop warmup.
    by_cell: dict[tuple[str, int], list[dict]] = {}
    for r in turns_data:
        if r.get('turn_id') == 0:
            continue
        key = (r['backend'], r['corpus_size'])
        by_cell.setdefault(key, []).append(r)

    backends = sorted({k[0] for k in by_cell})
    corpus_sizes = sorted({k[1] for k in by_cell})
    out['backends'] = backends
    out['corpus_sizes'] = corpus_sizes

    # System samples lookup (system rows only) — combined from rerun + canonical.
    # Same tolerance for in-progress writes.
    sys_rows = []
    proc_rows = []  # for backend RSS aggregation (rerun only; canonical is contaminated)
    thermal_rows = []
    for samp_name, samp in [('rerun', samples_rerun), ('canonical', samples_canonical)]:
        if not samp.exists():
            continue
        try:
            t = pq.read_table(samp, columns=['ts', 'proc', 'rss_mb', 'mem_compressed_mb',
                                             'mem_pressure_pct', 'swap_used_mb',
                                             'page_outs_per_sec', 'mem_free_mb',
                                             'mem_active_mb', 'mem_inactive_mb', 'mem_wired_mb',
                                             'cpu_user_pct', 'cpu_sys_pct', 'cpu_idle_pct',
                                             'load_avg_1m'])
            sys_rows.append(t.filter(pc.equal(t.column('proc'), '__system__')))
            if samp_name == 'rerun':
                # Only trust per-proc rows from the rerun (PID injection clean).
                proc_rows.append(t.filter(
                    pc.is_in(t.column('proc'), pa.array(['skeg', 'qdrant', 'ollama', 'orchestrator']))
                ))
            # Thermal rows present only if the sampler had sudo powermetrics.
            try:
                tt = pq.read_table(samp, columns=['ts', 'proc',
                                                  'cpu_die_temp_c', 'gpu_die_temp_c',
                                                  'fan_rpm', 'thermal_pressure_level'])
                thermal_rows.append(tt.filter(pc.equal(tt.column('proc'), '__thermals__')))
            except Exception:
                pass
        except Exception as e:
            print(f'[bench-data] skipping unreadable {samp.name}: {e!s:.120}')

    if sys_rows:
        sys_combined = pa.concat_tables(sys_rows, promote_options='default').sort_by('ts')
    else:
        sys_combined = None
    if proc_rows:
        proc_combined = pa.concat_tables(proc_rows, promote_options='default').sort_by('ts')
    else:
        proc_combined = None
    if thermal_rows:
        thermal_combined = pa.concat_tables(thermal_rows, promote_options='default').sort_by('ts')
    else:
        thermal_combined = None

    cells: list[dict] = []
    for backend in backends:
        for size in corpus_sizes:
            rows = by_cell.get((backend, size), [])
            if not rows:
                continue
            tps = [r['model_tps'] for r in rows if r.get('error') is None and r.get('model_tps')]
            retr = [r['retrieval_ms'] for r in rows if r.get('error') is None]
            embed = [r['embed_ms'] for r in rows if r.get('error') is None]
            gen = [r['gen_ms'] for r in rows if r.get('error') is None]
            tokens = [r['tokens_generated'] for r in rows if r.get('error') is None]

            cell = {
                'backend': backend,
                'corpus_size': size,
                'n_turns': len(rows),
                'tps_median': median(tps),
                'tps_p10': percentile(tps, 0.10),
                'tps_p90': percentile(tps, 0.90),
                'retrieval_ms_p50': median(retr),
                'retrieval_ms_p95': percentile(retr, 0.95),
                'retrieval_ms_p99': percentile(retr, 0.99),
                'embed_ms_p50': median(embed),
                'embed_ms_p95': percentile(embed, 0.95),
                'gen_ms_median': median(gen),
                'tokens_median': median([float(x) for x in tokens]),
            }

            # Memory pressure window: aligned to the cell's turn range.
            if sys_combined is not None and rows:
                tmin = min(r['t_query_start_ns'] for r in rows)
                tmax = max(r['t_gen_end_ns'] for r in rows)
                win = sys_combined.filter(pc.and_(
                    pc.greater_equal(sys_combined.column('ts'), tmin),
                    pc.less_equal(sys_combined.column('ts'), tmax),
                ))
                if win.num_rows:
                    comp = win.column('mem_compressed_mb').to_pylist()
                    press = win.column('mem_pressure_pct').to_pylist()
                    pgo = win.column('page_outs_per_sec').to_pylist()
                    free = win.column('mem_free_mb').to_pylist()
                    cell['compressed_mb_p50'] = median(comp)
                    cell['compressed_mb_p95'] = percentile(comp, 0.95)
                    cell['pressure_pct_p50'] = median(press)
                    cell['pressure_pct_p95'] = percentile(press, 0.95)
                    cell['page_outs_per_sec_p95'] = percentile(pgo, 0.95)
                    cell['mem_free_mb_p50'] = median(free)
                    # Full memory composition (medians during the cell window).
                    try:
                        active = [v for v in win.column('mem_active_mb').to_pylist() if v is not None]
                        inactive = [v for v in win.column('mem_inactive_mb').to_pylist() if v is not None]
                        wired = [v for v in win.column('mem_wired_mb').to_pylist() if v is not None]
                        if active:    cell['mem_active_mb_p50']   = median(active)
                        if inactive:  cell['mem_inactive_mb_p50'] = median(inactive)
                        if wired:     cell['mem_wired_mb_p50']    = median(wired)
                    except KeyError:
                        pass
                    # CPU load during this cell window
                    try:
                        cu = win.column('cpu_user_pct').to_pylist()
                        cs = win.column('cpu_sys_pct').to_pylist()
                        ci = win.column('cpu_idle_pct').to_pylist()
                        la = win.column('load_avg_1m').to_pylist()
                        cell['cpu_user_pct_p50'] = median(cu)
                        cell['cpu_sys_pct_p50'] = median(cs)
                        cell['cpu_idle_pct_p50'] = median(ci)
                        cell['cpu_idle_pct_p05'] = percentile(ci, 0.05)
                        cell['load_avg_1m_p50'] = median(la)
                        cell['load_avg_1m_p95'] = percentile(la, 0.95)
                    except KeyError:
                        pass

                # Thermals (best effort; only when sampler had sudo).
                if thermal_combined is not None and rows:
                    tmin0 = min(r['t_query_start_ns'] for r in rows)
                    tmax0 = max(r['t_gen_end_ns'] for r in rows)
                    twin = thermal_combined.filter(pc.and_(
                        pc.greater_equal(thermal_combined.column('ts'), tmin0),
                        pc.less_equal(thermal_combined.column('ts'), tmax0),
                    ))
                    if twin.num_rows:
                        try:
                            cpt = [v for v in twin.column('cpu_die_temp_c').to_pylist() if v is not None]
                            gpt = [v for v in twin.column('gpu_die_temp_c').to_pylist() if v is not None]
                            fan = [v for v in twin.column('fan_rpm').to_pylist() if v is not None]
                            thp = [v for v in twin.column('thermal_pressure_level').to_pylist() if v is not None]
                            if cpt:
                                cell['cpu_die_temp_c_p50'] = median(cpt)
                                cell['cpu_die_temp_c_p95'] = percentile(cpt, 0.95)
                                cell['cpu_die_temp_c_max'] = max(cpt)
                            if gpt:
                                cell['gpu_die_temp_c_p50'] = median(gpt)
                                cell['gpu_die_temp_c_max'] = max(gpt)
                            if fan:
                                cell['fan_rpm_p50'] = median(fan)
                                cell['fan_rpm_max'] = max(fan)
                            if thp:
                                cell['thermal_pressure_max'] = max(thp)
                        except KeyError:
                            pass

                # Backend process RSS (from rerun samples only; clean PID labels).
                if proc_combined is not None and backend in ('skeg', 'qdrant'):
                    proc_win = proc_combined.filter(pc.and_(
                        pc.and_(
                            pc.greater_equal(proc_combined.column('ts'), tmin),
                            pc.less_equal(proc_combined.column('ts'), tmax),
                        ),
                        pc.equal(proc_combined.column('proc'), backend),
                    ))
                    if proc_win.num_rows:
                        rss = proc_win.column('rss_mb').to_pylist()
                        cell['backend_rss_mb_p50'] = median(rss)
                        cell['backend_rss_mb_p95'] = percentile(rss, 0.95)
                        cell['backend_rss_mb_max'] = max(rss)

            cells.append(cell)

    out['cells'] = cells
    out['available'] = True
    return out


# Engines explicitly excluded from the public charts. LanceDB ran with a
# misconfigured tier in this CSV (see SLICE-A-RESULTS for the open question);
# rather than mislead readers with bad numbers we drop it. Add a note in the
# slice JSON so this is auditable.
EXCLUDED_ENGINES = {'lancedb'}


def aggregate_results_csv(bench_dir: Path) -> dict[str, dict]:
    """Load the historical bench-compare results.csv (long format) and pivot.

    Returns three dicts keyed by slice: 'A', 'B', 'C'. Each contains a list of
    row records with (engine, scale, n, sweep_param, sweep_value, concurrency,
    run, metric, value) re-shaped into pivot form per (engine, scale,
    sweep_value).
    """
    csv_path = bench_dir / 'results.csv'
    if not csv_path.exists():
        return {}

    import csv as _csv
    pivots: dict[str, dict] = {}  # slice → key tuple → record
    with csv_path.open() as f:
        reader = _csv.DictReader(f)
        for row in reader:
            sl = row['slice']
            if row.get('engine') in EXCLUDED_ENGINES:
                continue
            try:
                key = (
                    row['engine'], row['scale'], int(row['n']),
                    row['sweep_param'], float(row['sweep_value']) if row['sweep_value'] else None,
                    int(row['concurrency']), int(row['run']),
                )
            except (ValueError, KeyError):
                continue
            slot = pivots.setdefault(sl, {}).setdefault(key, {
                'engine': row['engine'],
                'scale': row['scale'],
                'n': int(row['n']),
                'sweep_param': row['sweep_param'],
                'sweep_value': float(row['sweep_value']) if row['sweep_value'] else None,
                'concurrency': int(row['concurrency']),
                'run': int(row['run']),
            })
            metric = row['metric']
            try:
                slot[metric] = float(row['value'])
            except ValueError:
                slot[metric] = row['value']

    out: dict[str, dict] = {}
    for sl, items in pivots.items():
        records = list(items.values())
        # Median across runs per (engine, scale, sweep_value)
        from collections import defaultdict
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for r in records:
            k = (r['engine'], r['scale'], r['sweep_value'], r['concurrency'])
            grouped[k].append(r)

        agg: list[dict] = []
        for k, rs in grouped.items():
            engine, scale, sweep_val, conc = k
            metric_keys = set()
            for r in rs:
                metric_keys.update(r.keys())
            metric_keys -= {'engine', 'scale', 'n', 'sweep_param', 'sweep_value',
                            'concurrency', 'run'}
            cell = {
                'engine': engine,
                'scale': scale,
                'n': rs[0]['n'],
                'sweep_param': rs[0]['sweep_param'],
                'sweep_value': sweep_val,
                'concurrency': conc,
                'n_runs': len(rs),
            }
            for m in metric_keys:
                vals = [r[m] for r in rs if isinstance(r.get(m), (int, float))]
                if vals:
                    cell[m] = median(vals)
            agg.append(cell)
        out[sl] = {'cells': agg}
    return out


def aggregate_slice_a(bench_dir: Path) -> dict:
    """Slice A — competitive matrix across engines × scales.

    Picks each engine's median row per scale (at the engine's most aggressive
    operating point if a sweep_value is present — typically the best knob).
    """
    pivots = aggregate_results_csv(bench_dir)
    if 'A' not in pivots:
        return {'slice': 'A', 'name': 'competitive', 'available': False,
                'note': 'no slice A rows in results.csv'}

    cells = pivots['A']['cells']
    # Order engines by family
    engines_seen = sorted({c['engine'] for c in cells})
    scales = sorted({c['scale'] for c in cells},
                    key=lambda s: int(''.join(d for d in s if d.isdigit()) or '0') * (1000 if 'k' in s else 1000000 if 'm' in s else 1))

    return {
        'slice': 'A',
        'name': 'competitive',
        'description': (
            'One row per engine per scale: recall@10, build time, RSS at steady '
            'state, query latency p99. Single corpus (mxbai-wiki-chunked), six '
            'engines in their default production-like configuration.'
        ),
        'methodology_path': 'bench-compare/BENCHMARK-METHODOLOGY.md',
        'results_path': 'bench-compare/SLICE-A-RESULTS.md',
        'excluded_engines': sorted(EXCLUDED_ENGINES),
        'available': True,
        'engines': engines_seen,
        'scales': scales,
        'cells': cells,
    }


def aggregate_slice_b(bench_dir: Path) -> dict:
    """Slice B — efficiency frontier (recall vs latency)."""
    pivots = aggregate_results_csv(bench_dir)
    if 'B' not in pivots:
        return {'slice': 'B', 'name': 'efficiency', 'available': False,
                'note': 'no slice B rows in results.csv'}
    cells = pivots['B']['cells']
    return {
        'slice': 'B',
        'name': 'efficiency',
        'description': (
            'Per-engine sweep of the query-time effort knob (l_search for skeg, '
            'ef for HNSW backends, nprobes for PQ variants). Traces the '
            'recall/latency frontier at a fixed scale.'
        ),
        'methodology_path': 'bench-compare/BENCHMARK-METHODOLOGY.md',
        'results_path': 'bench-compare/SLICE-B-RESULTS.md',
        'available': True,
        'engines': sorted({c['engine'] for c in cells}),
        'cells': cells,
    }


def aggregate_slice_e(bench_dir: Path) -> dict:
    """Slice E — skeg internals matrix.

    Loads results.matrix.csv (wide-format alternative is results.matrix.wide.csv
    but we re-pivot to keep the schema consistent with other slices).

    Surfaces:
        - tier comparison (int8, pq:128:256, turboquant-1/2/4)
        - default-ram vs low-ram mode
        - distribution (mxbai vs minilm)
        - extra metrics: first_query_p99_us (cold start), disk breakdown
    """
    csv_path = bench_dir / 'results.matrix.csv'
    if not csv_path.exists():
        return {'slice': 'E', 'name': 'internals', 'available': False,
                'note': 'results.matrix.csv not present'}

    import csv as _csv
    from collections import defaultdict
    # Pivot long-format → one record per (slice, dataset, tier, mode, layout, concurrency)
    pivots: dict[str, dict] = {}
    with csv_path.open() as f:
        reader = _csv.DictReader(f)
        for row in reader:
            try:
                key = (
                    row['slice'], row['dataset'], row['distribution'],
                    row['tier'], row['mode'], row['layout'],
                    int(row['concurrency']),
                )
            except (ValueError, KeyError):
                continue
            slot = pivots.setdefault(key, {
                'slice': row['slice'],
                'dataset': row['dataset'],
                'distribution': row['distribution'],
                'tier': row['tier'],
                'mode': row['mode'],
                'layout': row['layout'],
                'concurrency': int(row['concurrency']),
            })
            try:
                slot[row['metric']] = float(row['value'])
            except ValueError:
                slot[row['metric']] = row['value']

    cells = list(pivots.values())
    tiers = sorted({c['tier'] for c in cells})
    datasets = sorted({c['dataset'] for c in cells})
    modes = sorted({c['mode'] for c in cells})
    sub_slices = sorted({c['slice'] for c in cells})

    return {
        'slice': 'E',
        'name': 'internals',
        'description': (
            'skeg internal sweep across compression tiers (int8 / pq:128:256 / '
            'turboquant-1/2/4), with low-ram mode and cold-start RSS. One engine, '
            'many configurations — the choice menu inside the system.'
        ),
        'available': True,
        'tiers': tiers,
        'datasets': datasets,
        'modes': modes,
        'sub_slices': sub_slices,
        'cells': cells,
    }


def aggregate_slice_c(bench_dir: Path) -> dict:
    """Slice C — multi-tenant isolation."""
    pivots = aggregate_results_csv(bench_dir)
    if 'C' not in pivots:
        return {'slice': 'C', 'name': 'multi-tenant', 'available': False,
                'note': 'no slice C rows in results.csv'}
    cells = pivots['C']['cells']
    return {
        'slice': 'C',
        'name': 'multi-tenant',
        'description': (
            'Disjoint per-tenant datasets in a single skeg process. Measures '
            'isolation cost and recall stability under concurrent load.'
        ),
        'methodology_path': 'bench-compare/BENCHMARK-METHODOLOGY.md',
        'results_path': 'bench-compare/SLICE-C-RESULTS.md',
        'available': True,
        'engines': sorted({c['engine'] for c in cells}),
        'cells': cells,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bench-dir', type=Path, required=True,
                    help='Path to bench-compare/ checkout (with slice-d/ etc).')
    ap.add_argument('--out', type=Path, required=True,
                    help='Output directory for *.json files.')
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)

    slice_a = aggregate_slice_a(args.bench_dir)
    slice_b = aggregate_slice_b(args.bench_dir)
    slice_c = aggregate_slice_c(args.bench_dir)
    slice_d = aggregate_slice_d(args.bench_dir)
    slice_e = aggregate_slice_e(args.bench_dir)

    for s, name in [(slice_a, 'a'), (slice_b, 'b'), (slice_c, 'c'), (slice_d, 'd'), (slice_e, 'e')]:
        path = args.out / f'slice-{name}.json'
        path.write_text(json.dumps(s, indent=2, default=str))
        n = len(s.get('cells', []))
        print(f'wrote {path}  ({n} cells)')

    # Capture machine context from machine_info.*.jsonl (per-slice snapshots).
    # Includes the slice-d/ subdir (newer reruns may write their own).
    machine_index: dict[str, dict] = {}
    candidates = list(args.bench_dir.glob('machine_info*.jsonl'))
    for sub in ('slice-d',):
        sub_path = args.bench_dir / sub / 'machine_info.jsonl'
        if sub_path.exists():
            candidates.append(sub_path)
    for jf in candidates:
        # Map filename hint to slice label
        if jf.parent != args.bench_dir:
            slice_key = jf.parent.name  # e.g. "slice-d"
        else:
            slice_key = jf.stem.replace('machine_info', '').lstrip('.').lstrip('-') or 'default'
        # Accept both JSONL (one object per line) and a single
        # pretty-printed JSON object (newer run-slice-d.sh output).
        # Anything before the first '{' is ignored (stderr noise).
        text = jf.read_text()
        idx = text.find('{')
        if idx < 0:
            continue
        body = text[idx:]
        lines: list[dict] = []
        try:
            # Try JSONL first
            for ln in body.splitlines():
                ln = ln.strip()
                if not ln or not ln.startswith('{') or not ln.endswith('}'):
                    raise ValueError('not single-line JSONL')
                lines.append(json.loads(ln))
        except (ValueError, json.JSONDecodeError):
            lines = []
            try:
                obj = json.loads(body)
                lines = obj if isinstance(obj, list) else [obj]
            except json.JSONDecodeError:
                continue
        if not lines:
            continue
        start = lines[0]
        # Dev branches whose binaries are functionally identical to a
        # published release. The display label is the published version.
        # Update by hand when a new release is cut.
        DEV_TO_PUBLISHED = {
            'adapters+other': 'v0.1.1',
        }
        release_label = (
            DEV_TO_PUBLISHED.get(start.get('skeg_git_branch') or '')
            or start.get('skeg_git_branch')
        )
        machine_index[slice_key] = {
            'model': start.get('model'),
            'cpu_brand': start.get('cpu_brand'),
            'perf_cores': start.get('perf_cores'),
            'eff_cores': start.get('eff_cores'),
            'ram_gib': start.get('ram_gib'),
            'os_product': start.get('os_product'),
            'os_version': start.get('os_version'),
            'skeg_release': release_label,
            'skeg_git_branch': start.get('skeg_git_branch'),
            'skeg_git_commit': (start.get('skeg_git_commit') or '')[:7],
            'rustc': (start.get('rustc') or '').split(' ')[1] if start.get('rustc') else None,
            'numpy': start.get('numpy'),
            'chromadb': start.get('chromadb'),
            'pyarrow': start.get('pyarrow'),
            'label': start.get('label'),
        }
    (args.out / 'machine.json').write_text(json.dumps(machine_index, indent=2))
    print(f'wrote {args.out / "machine.json"}  ({len(machine_index)} snapshots)')

    index = {
        'slices': [
            {'id': 'a', 'name': 'competitive',  'available': slice_a.get('available', False)},
            {'id': 'b', 'name': 'efficiency',   'available': slice_b.get('available', False)},
            {'id': 'c', 'name': 'concurrency',  'available': slice_c.get('available', False)},
            {'id': 'd', 'name': 'co-residence', 'available': slice_d.get('available', False)},
            {'id': 'e', 'name': 'internals',    'available': slice_e.get('available', False)},
        ]
    }
    (args.out / 'index.json').write_text(json.dumps(index, indent=2))
    print(f'wrote {args.out / "index.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
