#!/usr/bin/env python3
"""Grid pathfinding — CCH build + query benchmark.

Builds an N x N 8-connected grid in FalkorDB (STEP edges, weight 1 / sqrt(2)),
builds the CCH index (CREATE CCH INDEX DDL), then a corner-to-corner db.idx.cch.query,
timing each stage. Sweeps several grid sizes so we can see how the cost scales.

Usage:
    FALKOR_PORT=6379 ./venv/bin/python bench.py            # default sweep
    SIZES=30,60,100 FALKOR_PORT=6379 python bench.py       # custom sizes
"""
import os, time, math
from falkordb import FalkorDB

PORT  = int(os.environ.get("FALKOR_PORT", "6379"))
SIZES = [int(s) for s in os.environ.get("SIZES", "20,30,50,100").split(",")]
SQRT2 = math.sqrt(2.0)

db = FalkorDB(host="localhost", port=PORT)
try:
    db.config_set("RESULTSET_SIZE", -1)
except Exception:
    pass


def build_grid(g, n):
    """Create an N x N 8-connected grid. Returns (node_count, edge_count, load_ms)."""
    g.query("MATCH (m) DETACH DELETE m")
    t0 = time.time()
    cells = [{"id": y * n + x, "x": x, "y": y} for y in range(n) for x in range(n)]
    g.query("UNWIND $cells AS c CREATE (:Cell {id:c.id, x:c.x, y:c.y})", {"cells": cells})
    g.query("CREATE INDEX FOR (c:Cell) ON (c.id)")
    # 8-connected edges, both directions, no walls at build time
    edges = []
    for y in range(n):
        for x in range(n):
            a = y * n + x
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < n and 0 <= ny < n:
                    w = SQRT2 if (dx and dy) else 1.0
                    edges.append({"s": a, "t": ny * n + nx, "w": w})
    # batch edge creation
    B = 20000
    for i in range(0, len(edges), B):
        g.query("UNWIND $e AS e MATCH (a:Cell {id:e.s}),(b:Cell {id:e.t}) "
                "CREATE (a)-[:STEP {w:e.w}]->(b)", {"e": edges[i:i + B]})
    load_ms = (time.time() - t0) * 1000
    nc = g.query("MATCH (c:Cell) RETURN count(c)").result_set[0][0]
    ec = g.query("MATCH ()-[r:STEP]->() RETURN count(r)").result_set[0][0]
    return nc, ec, load_ms


def drop_cch(g):
    """Drop the CCH index if present (swallow 'no such index')."""
    try:
        g.query("DROP CCH INDEX FOR ()-[e:STEP]->() ON (e.w)")
    except Exception:
        pass


def run_cch(g):
    """Build the CCH index (CREATE CCH INDEX DDL). Returns (arcs, db_ms, wall_ms);
    arcs is None — the DDL exposes no arc/shortcut count."""
    drop_cch(g)                                      # idempotent: create from a clean slate
    t0 = time.time()
    qr = g.query("CREATE CCH INDEX FOR ()-[e:STEP]->() ON (e.w)")
    wall = (time.time() - t0) * 1000
    db_ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    return None, db_ms, wall


def query(g, s, t):
    """Corner-to-corner CCH query. Returns (weight, hops, db_ms, wall_ms)."""
    t0 = time.time()
    qr = g.query("""MATCH (a:Cell {id:$s}), (b:Cell {id:$t})
                    CALL db.idx.cch.query({sourceNode:a, targetNode:b,
                        relTypes:['STEP'], weightProp:'w'})
                    YIELD pathWeight, path
                    RETURN pathWeight, [n IN nodes(path) | [n.x, n.y]]""",
                 {"s": s, "t": t})
    wall = (time.time() - t0) * 1000
    db_ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    row = qr.result_set[0]
    if row[0] is None:
        return None, 0, db_ms, wall
    return float(row[0]), len(row[1]), db_ms, wall


def rebuild(g):
    """Simulate an obstacle change: drop the CCH index and rebuild it fully.
    Returns (clear_ms, cch_db_ms, cch_wall_ms)."""
    t0 = time.time()
    drop_cch(g)
    clear_ms = (time.time() - t0) * 1000
    _, db_ms, wall = run_cch(g)     # run_cch's own drop is now a no-op
    return clear_ms, db_ms, wall


print(f"[bench] FalkorDB @ localhost:{PORT}  sizes={SIZES}\n")
hdr = f"{'grid':>9} {'nodes':>7} {'edges':>8} {'load ms':>9} {'CCH ms':>9} {'arcs':>10} {'query ms':>9} {'hops':>5} {'rebuild ms':>11}"
print(hdr); print("-" * len(hdr))
for n in SIZES:
    g = db.select_graph(f"game_grid_{n}")
    nc, ec, load_ms = build_grid(g, n)
    sc, cch_db, cch_wall = run_cch(g)
    w, hops, q_db, q_wall = query(g, 0, n * n - 1)       # top-left -> bottom-right corner
    _, reb_db, reb_wall = rebuild(g)
    e2e = cch_wall + q_wall                               # build CCH + one query, wall clock
    sc_str = f"{sc:,}" if sc is not None else "n/a"       # DDL exposes no arc count
    print(f"{n:>4}x{n:<4} {nc:>7} {ec:>8} {load_ms:>9.0f} "
          f"{cch_db:>8.2f} ({cch_wall:>5.0f}) {sc_str:>10} "
          f"{q_db:>7.2f} ({q_wall:>4.0f}) {hops:>5} {reb_wall:>10.0f}")
    print(f"          -> end-to-end (CCH build + query, wall): {e2e:.1f} ms  "
          f"[DB-internal: {cch_db + q_db:.2f} ms]")
print("\n[bench] done. () = wall-clock incl. python round-trip; bare = DB-internal run_time_ms.")
