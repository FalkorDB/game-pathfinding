#!/usr/bin/env python3
"""Compare FalkorDB shortest-path algorithms on the game grid: CCH vs Dijkstra
(algo.SPpaths) vs A* (algo.AStar).

Builds an N x N 8-connected grid with fake lat/lon (~1 m per cell, so A*'s
great-circle heuristic is admissible and well-scaled), builds the CCH once, then
times each algorithm over many repeats on several query pairs. Verifies all three
agree on the optimal path weight.

Usage:
    FALKOR_PORT=6379 ./venv/bin/python compare.py
    SIZES=30,60,100 REPS=40 python compare.py
"""
import os, time, math, statistics as st
from falkordb import FalkorDB

PORT  = int(os.environ.get("FALKOR_PORT", "6379"))
SIZES = [int(s) for s in os.environ.get("SIZES", "30,50,100").split(",")]
REPS  = int(os.environ.get("REPS", "40"))
SQRT2 = math.sqrt(2.0)
LAT0, LON0 = 32.0, 34.8
DLAT = 1.0 / 111320.0
DLON = 1.0 / (111320.0 * math.cos(math.radians(LAT0)))

db = FalkorDB(host="localhost", port=PORT)
try: db.config_set("RESULTSET_SIZE", -1)
except Exception: pass


def build(g, n):
    g.query("MATCH (m) DETACH DELETE m")
    cells = [{"id": y*n+x, "x": x, "y": y, "lat": LAT0+y*DLAT, "lon": LON0+x*DLON}
             for y in range(n) for x in range(n)]
    g.query("UNWIND $c AS c CREATE (:Cell {id:c.id,x:c.x,y:c.y,lat:c.lat,lon:c.lon})", {"c": cells})
    g.query("CREATE INDEX FOR (c:Cell) ON (c.id)")
    edges = []
    for y in range(n):
        for x in range(n):
            for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                nx, ny = x+dx, y+dy
                if 0 <= nx < n and 0 <= ny < n:
                    edges.append({"s": y*n+x, "t": ny*n+nx, "w": SQRT2 if (dx and dy) else 1.0})
    for i in range(0, len(edges), 20000):
        g.query("UNWIND $e AS e MATCH (a:Cell{id:e.s}),(b:Cell{id:e.t}) CREATE (a)-[:STEP{w:e.w}]->(b)",
                {"e": edges[i:i+20000]})


def drop_cch(g):
    try: g.query("DROP CCH INDEX FOR ()-[e:STEP]->() ON (e.w)")
    except Exception: pass

def build_cch(g):
    """Build the CCH index (CREATE CCH INDEX DDL). Returns (arcs, db_ms, wall_ms);
    arcs is None — the DDL exposes no arc/shortcut count."""
    drop_cch(g)                                       # idempotent: build from a clean slate
    t0 = time.time()
    qr = g.query("CREATE CCH INDEX FOR ()-[e:STEP]->() ON (e.w)")
    return None, float(getattr(qr, "run_time_ms", 0.0) or 0.0), (time.time()-t0)*1000

# one query for each algorithm -> (pathWeight, hops, db_ms)
Q = {
  "CCH": """MATCH (a:Cell{id:$s}),(b:Cell{id:$t})
            CALL db.idx.cch.query({sourceNode:a,targetNode:b,relTypes:['STEP'],weightProp:'w'})
            YIELD pathWeight,path RETURN pathWeight,length(path)""",
  "Dijkstra": """MATCH (a:Cell{id:$s}),(b:Cell{id:$t})
            CALL algo.SPpaths({sourceNode:a,targetNode:b,relTypes:['STEP'],weightProp:'w'})
            YIELD pathWeight,path RETURN pathWeight,length(path)""",
  "A*": """MATCH (a:Cell{id:$s}),(b:Cell{id:$t})
            CALL algo.AStar({sourceNode:a,targetNode:b,relTypes:['STEP'],weightProp:'w',
                latitudeProperty:'lat',longitudeProperty:'lon'})
            YIELD pathWeight,path RETURN pathWeight,length(path)""",
}

def run(g, algo, s, t):
    qr = g.query(Q[algo], {"s": s, "t": t})
    ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    row = qr.result_set[0]
    return (None, 0, ms) if row[0] is None else (float(row[0]), row[1], ms)


print(f"[compare] FalkorDB @ :{PORT}  sizes={SIZES}  reps={REPS}\n")
for n in SIZES:
    g = db.select_graph(f"cmp_grid_{n}")
    build(g, n)
    sc, cch_db, cch_wall = build_cch(g)
    pairs = [(0, n*n-1), (n//2, n*n-1-n//2), (n*(n-1), n-1)]   # corner/edge diagonals
    sc_str = f"{sc:,} arcs" if sc is not None else "index built (DDL: no arc count)"
    print(f"### {n}x{n}  ({n*n} nodes)   CCH build: {cch_db:.1f} ms DB / {cch_wall:.0f} ms wall, {sc_str}")
    print(f"    {'algo':<9} {'weight':>9} {'hops':>5} {'median ms':>10} {'mean ms':>9} {'min ms':>8}  {'vs Dijkstra':>11}")
    dijkstra_med = None
    results = {}
    for algo in ("Dijkstra", "A*", "CCH"):
        w = hops = None; times = []
        for s, t in pairs:
            run(g, algo, s, t)                       # warm-up
            for _ in range(REPS):
                w, hops, ms = run(g, algo, s, t)
                times.append(ms)
        med = st.median(times)
        results[algo] = (w, hops, med, st.mean(times), min(times))
        if algo == "Dijkstra": dijkstra_med = med
    for algo in ("Dijkstra", "A*", "CCH"):
        w, hops, med, mean, mn = results[algo]
        rel = f"{dijkstra_med/med:.2f}x" if med else "-"
        print(f"    {algo:<9} {w:>9.2f} {hops:>5} {med:>10.3f} {mean:>9.3f} {mn:>8.3f}  {rel:>11}")
    # sanity: all three agree on optimal weight
    ws = {round(results[a][0], 3) for a in results}
    print(f"    -> agree on optimal weight: {'YES' if len(ws)==1 else 'NO ' + str(ws)}\n")
print("[compare] done.  ms = DB-internal run_time_ms.  'vs Dijkstra' >1 means faster than Dijkstra.")
