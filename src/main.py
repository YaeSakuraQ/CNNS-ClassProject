#!/usr/bin/env python3
"""
NCFlow reproduction runner + Internet Topology Zoo evaluation.

This script loads `.graphml` topologies from `Internet_Topology_Zoo/`, generates
synthetic traffic matrices following NSDI'21 NCFlow (§5.1) methodology, and
evaluates:
  - PF4: k=4 path-based LP baseline (k-shortest-ish, edge-disjoint greedy) using Gurobi
  - NCFlow: current reproduction implementation in `src/ncflow/step1~5.py` using Gurobi

Outputs per-case CSV rows plus a small aggregate summary.

Notes / intentional deviations from the paper:
  - We do not replicate the full offline grid-search over #clusters (η) and path choices.
    `src/ncflow/step1.py` decides clusters via greedy modularity communities.
  - For PF4 paths, we implement a greedy edge-disjoint shortest-path procedure, not Yen k-shortest.
  - For the “10% max utilization” normalization (§5.1), we use a reproducible proxy:
    route all demands on a single shortest path (by invcap if available, else hop)
    and scale the whole TM such that max(util)≈0.1.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import math
import pickle
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import networkx as nx

try:
    import gurobipy as gp
    from gurobipy import GRB
except Exception as e:  # pragma: no cover
    print(f"[FATAL] gurobipy not available: {e}", file=sys.stderr)
    sys.exit(2)

from ncflow import (
    step1_offline_clustering,
    step2_online_flow_allocation,
    step5_generate_forwarding_entries,
)
from ncflow.utils import (
    get_physical_crossing_edges,
    load_topology_zoo,
    resolve_demand_path,
)


# -------------------------
# Data structures
# -------------------------


@dataclasses.dataclass(frozen=True)
class TMParams:
    model: str
    sample_idx: int
    alpha: int
    model_params: Tuple[Tuple[str, float], ...] = ()


@dataclasses.dataclass
class CaseResult:
    topology: str
    tm_model: str
    alpha: int
    sample: int
    pf4_flow: float
    pf4_time_s: float
    ncflow_flow: float
    ncflow_time_s: float
    rel_flow: float
    speedup: float
    proxy_edges_used: int
    proxy_total_hops: int


# -------------------------
# Progress bar (no deps)
# -------------------------


def _format_seconds(secs: float) -> str:
    secs = max(0.0, float(secs))
    if secs < 60:
        return f"{secs:.0f}s"
    mins = secs / 60.0
    if mins < 60:
        return f"{mins:.1f}m"
    hrs = mins / 60.0
    return f"{hrs:.1f}h"


def _render_progress_line(done: int, total: int, start_t: float, *, width: int = 24) -> str:
    done = int(done)
    total = max(1, int(total))
    frac = min(1.0, max(0.0, done / total))
    filled = int(round(frac * width))
    bar = "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
    elapsed = time.perf_counter() - start_t
    eta = (elapsed / done) * (total - done) if done > 0 else float("inf")
    pct = frac * 100.0
    return f"{bar} {done}/{total} ({pct:5.1f}%) ETA {_format_seconds(eta)}"


def _progress_supported(enabled: bool) -> bool:
    return bool(enabled and sys.stdout.isatty())


# -------------------------
# Helpers: topology loading
# -------------------------


def _set_invcap_weights(G: nx.Graph) -> None:
    for u, v, data in G.edges(data=True):
        cap = float(data.get("capacity", 0.0) or 0.0)
        if cap <= 0:
            cap = 1.0
        data["invcap"] = 1.0 / cap


def load_topology_graphml(zoo_dir: Path, name: str) -> nx.Graph:
    path = zoo_dir / f"{name}.graphml"
    if not path.exists():
        raise FileNotFoundError(str(path))
    G = load_topology_zoo(str(path))
    if G.number_of_nodes() == 0 or G.number_of_edges() == 0:
        raise ValueError(f"Empty/invalid topology: {path}")
    _set_invcap_weights(G)
    return G


# -------------------------
# Traffic models (§5.1)
# -------------------------


def _poisson_sample(rng: random.Random, lam: float) -> int:
    """Knuth poisson sampler (ok for small lam; deterministic per rng)."""
    if lam <= 0:
        return 0
    # For larger lam, Knuth becomes slow; approximate with normal.
    if lam > 20:
        x = rng.gauss(lam, math.sqrt(lam))
        return max(0, int(round(x)))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while p > L:
        k += 1
        p *= rng.random()
    return k - 1


def _all_ordered_pairs(nodes: Sequence[int]) -> Iterable[Tuple[int, int]]:
    for s in nodes:
        for t in nodes:
            if s != t:
                yield s, t


def _shortest_hop_distances(G: nx.Graph) -> Dict[Tuple[int, int], int]:
    # Precompute all-pairs shortest hop distances.
    dists: Dict[Tuple[int, int], int] = {}
    for s, lengths in nx.all_pairs_shortest_path_length(G):
        for t, d in lengths.items():
            if s != t:
                dists[(s, t)] = int(d)
    return dists


def generate_tm_base(
    G: nx.Graph,
    model: str,
    rng: random.Random,
    *,
    poisson_delta: float = 0.9,
    gravity_v: float = 1.0,
    bimodal_p: float = 0.2,
    bimodal_a: float = 0.1,
    bimodal_b: float = 0.5,
    bimodal_c: float = 1.0,
) -> Dict[Tuple[int, int], float]:
    """
    Generate an *unscaled* TM base with values roughly in [0,1] (not guaranteed),
    for all ordered pairs (s,t), s!=t.
    """
    nodes = list(G.nodes())
    tm: Dict[Tuple[int, int], float] = {}

    if model == "uniform":
        for s, t in _all_ordered_pairs(nodes):
            tm[(s, t)] = rng.random()
        return tm

    if model == "bimodal":
        for s, t in _all_ordered_pairs(nodes):
            if rng.random() < bimodal_p:
                tm[(s, t)] = rng.uniform(bimodal_b, bimodal_c)
            else:
                tm[(s, t)] = rng.uniform(0.0, bimodal_a)
        return tm

    if model == "poisson":
        dists = _shortest_hop_distances(G)
        # λ is absorbed by later normalization; set λ=1.
        lam0 = 1.0
        for s, t in _all_ordered_pairs(nodes):
            d = dists.get((s, t))
            if d is None:
                tm[(s, t)] = 0.0
                continue
            lam = lam0 * (poisson_delta ** float(d))
            tm[(s, t)] = float(_poisson_sample(rng, lam))
        # Normalize roughly into [0,1]
        mx = max(tm.values()) if tm else 1.0
        if mx > 0:
            for k in list(tm.keys()):
                tm[k] = tm[k] / mx
        return tm

    if model == "gravity":
        # Total out/in proportional to sum capacities.
        out_cap = {}
        in_cap = {}
        for n in nodes:
            # undirected: treat incident capacities as both out/in
            tot = 0.0
            for nbr in G.neighbors(n):
                tot += float(G[n][nbr].get("capacity", 0.0) or 0.0)
            out_cap[n] = tot
            in_cap[n] = tot
        out_w = {n: (out_cap[n] ** gravity_v) for n in nodes}
        in_w = {n: (in_cap[n] ** gravity_v) for n in nodes}
        sum_in = sum(in_w.values()) or 1.0
        for s, t in _all_ordered_pairs(nodes):
            tm[(s, t)] = (out_w[s] * in_w[t]) / sum_in
        mx = max(tm.values()) if tm else 1.0
        if mx > 0:
            for k in list(tm.keys()):
                tm[k] = tm[k] / mx
        return tm

    raise ValueError(f"Unknown traffic model: {model}")


def _estimate_max_utilization_singlepath(
    G: nx.Graph, tm: Dict[Tuple[int, int], float], *, weight: Optional[str] = "invcap"
) -> float:
    """
    Proxy: route every (s,t) demand on ONE shortest path and compute max edge utilization.
    Utilization uses undirected shared capacity; we add directional loads to the same edge key.
    """
    load = defaultdict(float)  # key: frozenset({u,v})
    for (s, t), vol in tm.items():
        if vol <= 0:
            continue
        try:
            path = nx.shortest_path(G, s, t, weight=weight)
        except nx.NetworkXNoPath:
            continue
        for u, v in zip(path[:-1], path[1:]):
            load[frozenset((u, v))] += vol

    max_util = 0.0
    for u, v, data in G.edges(data=True):
        cap = float(data.get("capacity", 0.0) or 0.0)
        if cap <= 0:
            continue
        util = load.get(frozenset((u, v)), 0.0) / cap
        if util > max_util:
            max_util = util
    return max_util


def normalize_tm_to_target_util(
    G: nx.Graph,
    tm: Dict[Tuple[int, int], float],
    *,
    target_max_util: float = 0.10,
    weight: Optional[str] = "invcap",
) -> Dict[Tuple[int, int], float]:
    max_util = _estimate_max_utilization_singlepath(G, tm, weight=weight)
    if max_util <= 0:
        return dict(tm)
    scale = target_max_util / max_util
    return {k: float(v) * scale for k, v in tm.items()}


def tm_to_demands(
    tm: Dict[Tuple[int, int], float], *, epsilon: float = 1e-12
) -> List[Dict[str, object]]:
    demands = []
    did = 0
    for (s, t), vol in tm.items():
        if vol <= epsilon:
            continue
        demands.append({"id": f"d{did}", "source": s, "target": t, "volume": float(vol)})
        did += 1
    return demands


# -------------------------
# PF4 baseline (path-based LP, k=4)
# -------------------------


def edge_disjoint_shortest_paths(
    G: nx.Graph, s: int, t: int, k: int, *, weight: Optional[str] = "invcap"
) -> List[List[int]]:
    """
    Greedy edge-disjoint shortest paths:
      repeat: shortest path on remaining graph, then remove its edges.
    """
    if s == t:
        return []
    paths: List[List[int]] = []
    H = G.copy()
    for _ in range(k):
        try:
            p = nx.shortest_path(H, s, t, weight=weight)
        except nx.NetworkXNoPath:
            break
        if len(p) < 2:
            break
        paths.append(p)
        H.remove_edges_from(list(zip(p[:-1], p[1:])))
    return paths


def build_pf4_pathsets(
    G: nx.Graph,
    demands: List[Dict[str, object]],
    k_paths: int,
    cache_path: Optional[Path],
    *,
    weight: Optional[str] = "invcap",
) -> Dict[str, List[List[int]]]:
    """
    Returns demand_id -> list of node-paths.
    Caches per-topology+params on disk when cache_path is provided.
    """
    if cache_path and cache_path.exists():
        with cache_path.open("rb") as f:
            return pickle.load(f)

    pathsets: Dict[str, List[List[int]]] = {}
    for d in demands:
        did = str(d["id"])
        s = int(d["source"])
        t = int(d["target"])
        pathsets[did] = edge_disjoint_shortest_paths(G, s, t, k_paths, weight=weight)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as f:
            pickle.dump(pathsets, f)
    return pathsets


def solve_pf4_gurobi(
    G: nx.Graph,
    demands: List[Dict[str, object]],
    pathsets: Dict[str, List[List[int]]],
    *,
    time_limit_s: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Path-based LP (PF4-like): maximize total allocated flow over candidate paths.
    Variables: f_{d,p} >= 0
    Constraints:
      - per demand sum_p f_{d,p} <= demand volume
      - per edge sum_{d,p uses e} f_{d,p} <= capacity(e)
    """
    t0 = time.perf_counter()
    m = gp.Model("PF4")
    m.setParam("OutputFlag", 0)
    if time_limit_s is not None:
        m.setParam("TimeLimit", float(time_limit_s))

    # Vars
    f_vars: Dict[Tuple[str, int], gp.Var] = {}
    for d in demands:
        did = str(d["id"])
        vol = float(d["volume"])
        ps = pathsets.get(did, [])
        for pi, _ in enumerate(ps):
            f_vars[(did, pi)] = m.addVar(lb=0.0, ub=vol, name=f"f_{did}_{pi}")

    # Demand constraints
    for d in demands:
        did = str(d["id"])
        vol = float(d["volume"])
        ps = pathsets.get(did, [])
        if not ps:
            continue
        m.addConstr(gp.quicksum(f_vars[(did, pi)] for pi in range(len(ps))) <= vol, name=f"dem_{did}")

    # Edge capacity constraints
    # Build edge -> list of (did,pi) that use it.
    uses = defaultdict(list)  # key: frozenset({u,v}) -> list[(did,pi)]
    for d in demands:
        did = str(d["id"])
        ps = pathsets.get(did, [])
        for pi, path in enumerate(ps):
            for u, v in zip(path[:-1], path[1:]):
                uses[frozenset((u, v))].append((did, pi))

    for u, v, data in G.edges(data=True):
        cap = float(data.get("capacity", 0.0) or 0.0)
        if cap <= 0:
            continue
        key = frozenset((u, v))
        lst = uses.get(key)
        if not lst:
            continue
        m.addConstr(gp.quicksum(f_vars[(did, pi)] for (did, pi) in lst) <= cap, name=f"cap_{u}_{v}")

    m.setObjective(gp.quicksum(f_vars.values()), GRB.MAXIMIZE)
    m.optimize()

    obj = float(m.objVal) if m.SolCount > 0 else 0.0
    t1 = time.perf_counter()
    return obj, (t1 - t0)


# -------------------------
# NCFlow runner
# -------------------------


def build_ncflow_constraints(
    G: nx.Graph,
    clusters: Dict[int, int],
    aggregated_graph: nx.Graph,
    demands: List[Dict[str, object]],
    seed: int,
    *,
    weight: Optional[str] = "invcap",
) -> Dict[Tuple[int, int], Dict[str, object]]:
    """
    Builds constraints[(c_src,c_dst)] = {'allowed_path': [cluster ids], 'allowed_edges': [(u,v),...]}
    required by `src/ncflow/step2.py`.
    """
    physical_crossing_edges = get_physical_crossing_edges(G, clusters)

    # Precompute invcap on agg graph for path selection
    for u, v, data in aggregated_graph.edges(data=True):
        cap = float(data.get("capacity", 0.0) or 0.0)
        if cap <= 0:
            cap = 1.0
        data["invcap"] = 1.0 / cap

    bundles = set()
    for d in demands:
        s = int(d["source"])
        t = int(d["target"])
        c_s = int(clusters[s])
        c_t = int(clusters[t])
        if c_s != c_t:
            bundles.add((c_s, c_t))

    constraints: Dict[Tuple[int, int], Dict[str, object]] = {}
    for (c_s, c_t) in bundles:
        try:
            c_path = nx.shortest_path(aggregated_graph, c_s, c_t, weight=weight)
        except nx.NetworkXNoPath:
            continue

        chosen_edges: List[Tuple[int, int]] = []
        ok = True
        for i in range(len(c_path) - 1):
            cu = int(c_path[i])
            cv = int(c_path[i + 1])
            candidates = physical_crossing_edges.get((cu, cv), [])
            if not candidates:
                ok = False
                break
            # deterministic choice per (bundle, hop)
            hop_seed = hash((seed, c_s, c_t, cu, cv)) & 0xFFFFFFFF
            rr = random.Random(hop_seed)
            chosen_edges.append(rr.choice(candidates))
        if not ok:
            continue
        constraints[(c_s, c_t)] = {"allowed_path": c_path, "allowed_edges": chosen_edges}

    return constraints


def run_ncflow_once(
    G: nx.Graph,
    demands: List[Dict[str, object]],
    seed: int,
) -> Tuple[float, float, int, int]:
    """
    Returns: (total_flow, runtime_s, proxy_edges_used, proxy_total_hops)
    """
    t0 = time.perf_counter()

    clusters, aggG = step1_offline_clustering(G)
    constraints = build_ncflow_constraints(G, clusters, aggG, demands, seed=seed)
    alloc = step2_online_flow_allocation(G, clusters, aggG, demands, constraints)

    # total allocated flow
    total_flow = float(sum(float(v) for v in alloc.values()))

    # Build a proxy edge-flow dict by mapping each demand to a single concrete node path.
    edge_flow: Dict[Tuple[int, int], float] = defaultdict(float)
    proxy_total_hops = 0
    for d in demands:
        did = str(d["id"])
        vol = float(alloc.get(did, 0.0) or 0.0)
        if vol <= 1e-12:
            continue
        c_s = int(clusters[int(d["source"])])
        c_t = int(clusters[int(d["target"])])
        cons = constraints.get((c_s, c_t))
        node_path = resolve_demand_path(G, clusters, d, cons)
        if not node_path or len(node_path) < 2:
            continue
        proxy_total_hops += (len(node_path) - 1)
        for u, v in zip(node_path[:-1], node_path[1:]):
            edge_flow[(int(u), int(v))] += vol

    proxy_edges_used = sum(1 for _, f in edge_flow.items() if f > 1e-12)

    # Optional: run step5 decomposition for sanity (not used in metrics directly)
    _ = step5_generate_forwarding_entries(G, dict(edge_flow))

    t1 = time.perf_counter()
    return total_flow, (t1 - t0), proxy_edges_used, proxy_total_hops


# -------------------------
# Orchestration
# -------------------------


DEFAULT_TOPOLOGIES = [
    "Kdl",
    "Cogentco",
    "UsCarrier",
    "Colt",
    "GtsCe",
    "TataNld",
    "DialtelecomCz",
    "Ion",
    "Deltacom",
    "Interoute",
    "Uninett2010",
]


def iter_tm_param_grid(
    models: Sequence[str], alphas: Sequence[int], samples_per: int, rng: random.Random
) -> Iterable[TMParams]:
    for model in models:
        # For Poisson, match §5.1 intent (δ close to 0 and close to 1)
        if model == "poisson":
            deltas = [0.2, 0.9]
        else:
            deltas = [None]
        for alpha in alphas:
            for sample_idx in range(samples_per):
                if model == "poisson":
                    delta = deltas[sample_idx % len(deltas)]
                    mp = (("delta", float(delta)),)
                else:
                    mp = ()
                yield TMParams(model=model, sample_idx=sample_idx, alpha=int(alpha), model_params=mp)


def cache_key_for_paths(topology_name: str, k_paths: int) -> str:
    return f"{topology_name}__k{k_paths}__weight_invcap__edgedisjoint_greedy.pkl"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="NCFlow reproduction evaluator (Topology Zoo, §5.1-like).")
    ap.add_argument("--zoo-dir", default="Internet_Topology_Zoo", help="Directory containing Topology Zoo files.")
    ap.add_argument("--topologies", nargs="*", default=DEFAULT_TOPOLOGIES, help="Topology names without extension.")
    ap.add_argument(
        "--traffic-models",
        nargs="*",
        default=["poisson", "gravity", "uniform", "bimodal"],
        choices=["poisson", "gravity", "uniform", "bimodal"],
    )
    ap.add_argument("--alphas", nargs="*", type=int, default=[1, 2, 4, 8, 16, 32, 64, 128])
    ap.add_argument("--samples-per-setting", type=int, default=5)
    ap.add_argument("--k-paths", type=int, default=4)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--pf4-time-limit-s", type=float, default=None, help="Optional Gurobi time limit for PF4.")
    ap.add_argument("--out", default="results/results.csv", help="CSV output path.")
    ap.add_argument("--cache-dir", default="cache/paths", help="Disk cache directory for PF4 pathsets.")
    ap.add_argument("--smoke", action="store_true", help="Run a minimal quick smoke test configuration.")
    ap.add_argument("--no-progress", action="store_true", help="Disable progress bar.")
    ap.add_argument("--verbose", action="store_true", help="Print per-case lines (otherwise use progress bar only).")
    args = ap.parse_args(list(argv) if argv is not None else None)

    zoo_dir = Path(args.zoo_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)

    print(f"[env] gurobipy={gp.gurobi.version()}")
    print(f"[cfg] topologies={args.topologies}")
    print(f"[cfg] traffic_models={args.traffic_models}")
    print(f"[cfg] alphas={args.alphas} samples={args.samples_per_setting} seed={args.seed}")

    if args.smoke:
        # shrink the run for sanity
        args.topologies = ["Uninett2010"]
        args.traffic_models = ["poisson"]
        args.alphas = [1]
        args.samples_per_setting = 1
        print("[cfg] smoke enabled: using Uninett2010 / poisson / alpha=1 / samples=1")

    rng_master = random.Random(args.seed)
    results: List[CaseResult] = []

    # Pre-compute a best-effort total for progress bar (counts only existing graphml files).
    existing_topos = [t for t in args.topologies if (zoo_dir / f"{t}.graphml").exists()]
    total_cases_est = len(existing_topos) * (len(args.traffic_models) * len(args.alphas) * args.samples_per_setting)
    progress_on = _progress_supported(not args.no_progress)
    progress_start = time.perf_counter()
    done_cases = 0

    with out_path.open("w", newline="") as fcsv:
        writer = csv.DictWriter(
            fcsv,
            fieldnames=[
                "topology",
                "model",
                "alpha",
                "sample",
                "pf4_flow",
                "pf4_time_s",
                "ncflow_flow",
                "ncflow_time_s",
                "rel_flow",
                "speedup",
                "proxy_edges_used",
                "proxy_total_hops",
            ],
        )
        writer.writeheader()

        for topo_name in args.topologies:
            try:
                G = load_topology_graphml(zoo_dir, topo_name)
            except Exception as e:
                print(f"[WARN] skip topology {topo_name}: {e}")
                continue

            for tm_params in iter_tm_param_grid(
                args.traffic_models, args.alphas, args.samples_per_setting, rng_master
            ):
                # Deterministic per-case RNG
                case_seed = hash((args.seed, topo_name, tm_params.model, tm_params.alpha, tm_params.sample_idx, tm_params.model_params)) & 0xFFFFFFFF
                rng = random.Random(case_seed)

                # Model-specific params
                model_kwargs = {}
                if tm_params.model == "poisson":
                    md = dict(tm_params.model_params)
                    model_kwargs["poisson_delta"] = float(md["delta"])

                tm_base = generate_tm_base(G, tm_params.model, rng, **model_kwargs)
                tm_norm = normalize_tm_to_target_util(G, tm_base, target_max_util=0.10, weight="invcap")
                tm_scaled = {k: v * float(tm_params.alpha) for k, v in tm_norm.items()}
                demands = tm_to_demands(tm_scaled)

                # PF4
                cache_path = cache_dir / cache_key_for_paths(topo_name, args.k_paths)
                pathsets = build_pf4_pathsets(G, demands, args.k_paths, cache_path)
                pf4_flow, pf4_time = solve_pf4_gurobi(
                    G, demands, pathsets, time_limit_s=args.pf4_time_limit_s
                )

                # NCFlow
                nc_flow, nc_time, proxy_edges, proxy_hops = run_ncflow_once(G, demands, seed=case_seed)

                rel_flow = (nc_flow / pf4_flow) if pf4_flow > 1e-12 else 0.0
                speedup = (pf4_time / nc_time) if nc_time > 1e-12 else float("inf")

                row = CaseResult(
                    topology=topo_name,
                    tm_model=tm_params.model,
                    alpha=tm_params.alpha,
                    sample=tm_params.sample_idx,
                    pf4_flow=pf4_flow,
                    pf4_time_s=pf4_time,
                    ncflow_flow=nc_flow,
                    ncflow_time_s=nc_time,
                    rel_flow=rel_flow,
                    speedup=speedup,
                    proxy_edges_used=proxy_edges,
                    proxy_total_hops=proxy_hops,
                )
                results.append(row)
                writer.writerow(
                    {
                        "topology": row.topology,
                        "model": row.tm_model,
                        "alpha": row.alpha,
                        "sample": row.sample,
                        "pf4_flow": f"{row.pf4_flow:.6g}",
                        "pf4_time_s": f"{row.pf4_time_s:.6g}",
                        "ncflow_flow": f"{row.ncflow_flow:.6g}",
                        "ncflow_time_s": f"{row.ncflow_time_s:.6g}",
                        "rel_flow": f"{row.rel_flow:.6g}",
                        "speedup": f"{row.speedup:.6g}",
                        "proxy_edges_used": row.proxy_edges_used,
                        "proxy_total_hops": row.proxy_total_hops,
                    }
                )
                fcsv.flush()

                done_cases += 1
                if progress_on and not args.verbose:
                    line = _render_progress_line(done_cases, total_cases_est, progress_start)
                    # Clear line tail with padding
                    pad = " " * max(0, 4)
                    print("\r" + line + pad, end="", flush=True)
                else:
                    print(
                        f"[case] {done_cases}/{total_cases_est} topo={topo_name} model={tm_params.model} alpha={tm_params.alpha} sample={tm_params.sample_idx} "
                        f"rel_flow={rel_flow:.3f} speedup={speedup:.2f}x"
                    )

    if progress_on and not args.verbose:
        # Finish the progress line with a newline.
        print()

    # Summary
    if results:
        rels = sorted(r.rel_flow for r in results)
        spds = sorted(r.speedup for r in results if math.isfinite(r.speedup))
        def pct(xs: List[float], p: float) -> float:
            if not xs:
                return float("nan")
            i = int(round((len(xs) - 1) * p))
            return xs[max(0, min(len(xs) - 1, i))]

        print("[summary] cases =", len(results))
        print(f"[summary] rel_flow median={pct(rels, 0.5):.3f} p10={pct(rels, 0.1):.3f} p90={pct(rels, 0.9):.3f}")
        print(f"[summary] speedup median={pct(spds, 0.5):.2f}x p10={pct(spds, 0.1):.2f}x p90={pct(spds, 0.9):.2f}x")
        print(f"[summary] csv={out_path}")
    else:
        print("[summary] no results produced (all topologies skipped?)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

