#!/usr/bin/env python3
"""
Game-grid pathfinding backend (FalkorDB + CCH).

An N x N 8-connected grid lives in FalkorDB as (:Cell) nodes joined by
[:STEP {w}] edges (w = 1 orthogonal, sqrt(2) diagonal). The CCH is a graph-level
path *index*: `CREATE CCH INDEX FOR ()-[e:STEP]->() ON (e.w)` builds the hierarchy
inside the index (no SHORTCUT edges / rank properties on the graph),
db.idx.cch.query answers point-to-point shortest paths, `DROP CCH INDEX ...` frees it.

A wall is a blocked cell: its incident STEP edges (and the diagonal "corner" edges
that would cut past it) are removed. FalkorDB *can* auto-maintain a live CCH index
across edits (it rebuilds/recustomizes at write-commit), but on a high-treewidth
grid every tiny edit would then pay a full rebuild -- so this demo instead DROPS
the index while walls are edited (keeping edits cheap) and rebuilds it on demand
(CREATE CCH INDEX), timing that rebuild: the CCH preprocessing cost a dynamic map
keeps re-paying. On a small grid that's tens of milliseconds, so re-routing stays
live.

Endpoints:
  GET  /                 -> the 3D UI (static/index.html)
  GET  /meta             -> grid size, start cell, walls, graph + CCH stats
  GET  /route            -> shortest path via db.idx.cch.query  (?sx&sy&tx&ty)
  POST /obstacle         -> toggle a wall, rebuild CCH, optionally re-route
  POST /reset            -> clear all walls, rebuild grid + CCH
"""
import os, time, math, threading
from flask import Flask, request, jsonify, send_from_directory
from falkordb import FalkorDB

HERE       = os.path.dirname(os.path.abspath(__file__))
N          = int(os.environ.get("GRID_N", "30"))
FALKOR_PORT= int(os.environ.get("FALKOR_PORT", "6379"))
GRAPH      = os.environ.get("GRAPH", "game_grid")
PORT       = int(os.environ.get("PORT", "8090"))
SQRT2      = math.sqrt(2.0)
START      = {"x": 2, "y": N // 2}

# fake geo coords so algo.AStar has a heuristic: scaled to ~1 metre per cell, which
# keeps A*'s great-circle heuristic admissible AND well-scaled vs the STEP weights.
LAT0, LON0 = 32.0, 34.8
DLAT = 1.0 / 111320.0
DLON = 1.0 / (111320.0 * math.cos(math.radians(LAT0)))

# the CCH index spans STEP edges over the 'w' metric; this config (plus source/
# target) parameterizes the db.idx.cch.query call.
CCH_CFG = "relTypes:['STEP'], weightProp:'w'"
# Build/drop use the CCH-INDEX DDL (the current FalkorDB CCH interface), not the old
# db.idx.cch.create/.drop procedures. db.idx.cch.query still answers the routing.
CCH_CREATE = "CREATE CCH INDEX FOR ()-[e:STEP]->() ON (e.w)"
CCH_DROP   = "DROP CCH INDEX FOR ()-[e:STEP]->() ON (e.w)"

_db = FalkorDB(host="localhost", port=FALKOR_PORT)
try:
    _db.config_set("RESULTSET_SIZE", -1)
except Exception as e:
    print("[boot] warn: RESULTSET_SIZE:", e, flush=True)
g = _db.select_graph(GRAPH)

DB_TIMEOUT = int(os.environ.get("DB_TIMEOUT_MS", "120000"))
def cy(q, **p):
    return g.query(q, p, timeout=DB_TIMEOUT) if p else g.query(q, timeout=DB_TIMEOUT)

# in-memory mirror of the wall set + latest CCH stats (for /meta without re-counting).
# "arcs" stays None: CREATE CCH INDEX is DDL and exposes no arc/shortcut count, so the
# build's headline number is its run time (cch_ms), not an arc count.
walls = set()                       # {(x, y), ...}
stats = {"nodes": 0, "edges": 0, "arcs": None, "cch_ms": 0.0, "cch_dirty": False}
_index_present = False               # is a CCH index currently registered on the graph?

# --------------------------------------------------------------------------
# grid helpers
# --------------------------------------------------------------------------
def cid(x, y): return y * N + x
def in_bounds(x, y): return 0 <= x < N and 0 <= y < N
def walkable(x, y): return in_bounds(x, y) and (x, y) not in walls

NB8 = [(1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)]

def edge_ok(ax, ay, bx, by):
    """Should a STEP edge exist between two adjacent cells? Both walkable, and a
    diagonal must not cut the corner past a wall (both shared orthogonal cells free)."""
    if not (walkable(ax, ay) and walkable(bx, by)):
        return False
    dx, dy = bx - ax, by - ay
    if dx and dy:                                   # diagonal
        return walkable(ax + dx, ay) and walkable(ax, ay + dy)
    return True

def all_edges():
    """Every directed STEP edge for the current wall set."""
    out = []
    for y in range(N):
        for x in range(N):
            if not walkable(x, y):
                continue
            for dx, dy in NB8:
                nx, ny = x + dx, y + dy
                if edge_ok(x, y, nx, ny):
                    out.append({"s": cid(x, y), "t": cid(nx, ny),
                                "w": SQRT2 if (dx and dy) else 1.0})
    return out

# --------------------------------------------------------------------------
# CCH build / rebuild
# --------------------------------------------------------------------------
def drop_cch():
    """Drop the CCH index if one is registered (cheap no-op otherwise). Keeping the
    index absent while STEP edges are edited stops FalkorDB's write-commit auto-
    maintenance from rebuilding it on every tiny edit."""
    global _index_present
    if not _index_present:
        return
    try:
        cy(CCH_DROP)
    except Exception:
        pass                                        # already gone; keep the flag honest
    _index_present = False

def build_cch():
    """(Re)build the CCH index from the current graph. Returns DB-internal run_time_ms.
    Drops first so create never hits the 'already exists' error; the create call's
    own run_time_ms is the CCH preprocessing cost we surface."""
    global _index_present
    drop_cch()
    qr = cy(CCH_CREATE)
    ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    _index_present = True
    stats["arcs"] = None            # CREATE CCH INDEX is DDL — no arc/shortcut count exposed
    stats["cch_ms"] = ms
    stats["cch_dirty"] = False
    return ms

def refresh_counts():
    stats["nodes"] = cy("MATCH (c:Cell) RETURN count(c)").result_set[0][0]
    stats["edges"] = cy("MATCH ()-[r:STEP]->() RETURN count(r)").result_set[0][0]

def build_grid_full():
    """Wipe + create the whole grid from scratch (nodes, edges), then CCH."""
    drop_cch()                                       # avoid rebuilding on every wipe/create batch
    cy("MATCH (m) DETACH DELETE m")
    cells = [{"id": cid(x, y), "x": x, "y": y,
              "lat": LAT0 + y * DLAT, "lon": LON0 + x * DLON}
             for y in range(N) for x in range(N)]
    cy("UNWIND $cells AS c CREATE (:Cell {id:c.id, x:c.x, y:c.y, lat:c.lat, lon:c.lon})",
       cells=cells)
    try:
        cy("CREATE INDEX FOR (c:Cell) ON (c.id)")
    except Exception:
        pass
    edges = all_edges()
    B = 20000
    for i in range(0, len(edges), B):
        cy("UNWIND $e AS e MATCH (a:Cell {id:e.s}),(b:Cell {id:e.t}) "
           "CREATE (a)-[:STEP {w:e.w}]->(b)", e=edges[i:i+B])
    refresh_counts()
    build_cch()

def edit_wall(x, y):
    """Add/remove a wall at (x,y) with a *local* edge edit (no CCH rebuild).
    Only edges incident to the cell and the four corner-diagonals it gates change.
    Marks the CCH stale. Returns (now_wall, edit_ms)."""
    now_wall = (x, y) not in walls
    if now_wall: walls.add((x, y))
    else:        walls.discard((x, y))

    # drop the index first so the STEP-edge edits below don't each trigger a
    # write-commit CCH rebuild; it's recreated on demand (build_cch) when needed.
    drop_cch()

    # candidate undirected edges affected by this cell:
    #  - the cell to each of its 8 neighbours
    #  - the 4 diagonals among its orthogonal neighbours (the corners it gates)
    cand = [((x, y), (x+dx, y+dy)) for dx, dy in NB8]
    cand += [((x-1, y), (x, y+1)), ((x-1, y), (x, y-1)),
             ((x+1, y), (x, y+1)), ((x+1, y), (x, y-1))]

    t0 = time.time()
    pairs, add = [], []
    for (ax, ay), (bx, by) in cand:
        if not (in_bounds(ax, ay) and in_bounds(bx, by)):
            continue
        a, b = cid(ax, ay), cid(bx, by)
        pairs.append({"a": a, "b": b})              # drop both directions
        if edge_ok(ax, ay, bx, by):                 # re-add if the edge should exist
            w = SQRT2 if (bx-ax and by-ay) else 1.0
            add += [{"s": a, "t": b, "w": w}, {"s": b, "t": a, "w": w}]
    # one batched delete + one batched create (keeps the graph update a couple of ms)
    cy("UNWIND $p AS p MATCH (x:Cell {id:p.a})-[r:STEP]-(y:Cell {id:p.b}) DELETE r", p=pairs)
    if add:
        cy("UNWIND $e AS e MATCH (a:Cell {id:e.s}),(b:Cell {id:e.t}) "
           "CREATE (a)-[:STEP {w:e.w}]->(b)", e=add)
    edit_ms = (time.time() - t0) * 1000
    refresh_counts()
    stats["cch_dirty"] = True
    return now_wall, edit_ms

# --------------------------------------------------------------------------
# routing — CCH vs Dijkstra (algo.SPpaths) vs A* (algo.AStar)
# --------------------------------------------------------------------------
_ALGO_Q = {
  "cch": (f"MATCH (a:Cell {{id:$s}}),(b:Cell {{id:$t}}) "
          f"CALL db.idx.cch.query({{sourceNode:a,targetNode:b,{CCH_CFG}}}) "
          f"YIELD pathWeight,path RETURN pathWeight,[n IN nodes(path)|[n.x,n.y]]"),
  "dijkstra": ("MATCH (a:Cell {id:$s}),(b:Cell {id:$t}) "
          "CALL algo.SPpaths({sourceNode:a,targetNode:b,relTypes:['STEP'],weightProp:'w'}) "
          "YIELD pathWeight,path RETURN pathWeight,[n IN nodes(path)|[n.x,n.y]]"),
  "astar": ("MATCH (a:Cell {id:$s}),(b:Cell {id:$t}) "
          "CALL algo.AStar({sourceNode:a,targetNode:b,relTypes:['STEP'],weightProp:'w',"
          "latitudeProperty:'lat',longitudeProperty:'lon'}) "
          "YIELD pathWeight,path RETURN pathWeight,[n IN nodes(path)|[n.x,n.y]]"),
}
ALGOS = ("cch", "dijkstra", "astar")

def ensure_cch():
    """Rebuild the CCH iff walls changed since the last build. Returns rebuild ms or None.
    (FalkorDB CCH has no incremental customize -> any change is a full rebuild.)"""
    return build_cch() if stats.get("cch_dirty") else None

def run_algo(algo, sx, sy, tx, ty):
    """Shortest path via the chosen algorithm. Returns (cells, weight, db_ms)."""
    if algo not in _ALGO_Q or not walkable(sx, sy) or not walkable(tx, ty):
        return [], None, 0.0
    qr = cy(_ALGO_Q[algo], s=cid(sx, sy), t=cid(tx, ty))
    db_ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    row = qr.result_set[0] if qr.result_set else None
    if not row or row[0] is None:
        return [], None, db_ms
    return [[int(c[0]), int(c[1])] for c in row[1]], float(row[0]), db_ms

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
srv = Flask(__name__)
# serialize graph-mutating work: each endpoint's multi-step DB sequence + stats
# update runs atomically, so concurrent requests can't interleave on the graph.
_LOCK = threading.Lock()

@srv.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")

@srv.get("/common/<path:f>")
def common(f):
    return send_from_directory(os.path.join(HERE, "common"), f)

@srv.get("/meta")
def meta():
    return jsonify(n=N, start=START, walls=[[x, y] for (x, y) in walls], **stats)

@srv.get("/route")
def route_ep():
    a = request.args
    algo = a.get("algo", "astar")
    sx, sy, tx, ty = int(a["sx"]), int(a["sy"]), int(a["tx"]), int(a["ty"])
    with _LOCK:
        # CCH can only answer once its (stale) hierarchy is rebuilt -- part of the story
        rebuild_ms = ensure_cch() if algo == "cch" else None
        cells, w, ms = run_algo(algo, sx, sy, tx, ty)
        return jsonify(algo=algo, cells=cells, weight=w, query_ms=ms,
                       cch_rebuild_ms=rebuild_ms, arcs=stats["arcs"],
                       cch_dirty=stats["cch_dirty"])

@srv.post("/obstacle")
def obstacle_ep():
    d = request.get_json(force=True)
    x, y = int(d["x"]), int(d["y"])
    algo = d.get("algo", "astar")
    if not in_bounds(x, y):
        return jsonify(error="out of bounds"), 400
    if (x, y) == (START["x"], START["y"]):
        return jsonify(error="cannot wall the start cell"), 400
    with _LOCK:
        is_wall, edit_ms = edit_wall(x, y)
        # only CCH mode pays the full rebuild now; A*/Dijkstra need no preprocessing
        cch_ms = build_cch() if algo == "cch" else None
        resp = {"wall": is_wall, "walls_count": len(walls), "algo": algo,
                "edit_ms": round(edit_ms, 3),
                "cch_ms": round(cch_ms, 3) if cch_ms is not None else None,
                "arcs": stats["arcs"], "edges": stats["edges"],
                "cch_dirty": stats["cch_dirty"]}
        if all(k in d for k in ("sx", "sy", "tx", "ty")):
            cells, w, ms = run_algo(algo, int(d["sx"]), int(d["sy"]), int(d["tx"]), int(d["ty"]))
            resp["route"] = {"cells": cells, "weight": w, "query_ms": ms}
        return jsonify(resp)

@srv.get("/compare")
def compare_ep():
    """Race all three algorithms on the same query (CCH rebuilt first if stale)."""
    a = request.args
    sx, sy, tx, ty = int(a["sx"]), int(a["sy"]), int(a["tx"]), int(a["ty"])
    with _LOCK:
        rebuild_ms = ensure_cch()             # CCH must be current to time it fairly
        out = {"cch_rebuild_ms": rebuild_ms, "arcs": stats["arcs"], "results": {}}
        for algo in ALGOS:
            cells, w, ms = run_algo(algo, sx, sy, tx, ty)
            out["results"][algo] = {"weight": w, "hops": max(0, len(cells) - 1),
                                    "query_ms": ms}
        return jsonify(out)

@srv.post("/reset")
def reset_ep():
    with _LOCK:
        walls.clear()
        build_grid_full()
        return jsonify(n=N, start=START, walls=[], **stats)

def boot():
    print(f"[boot] graph={GRAPH!r} port={FALKOR_PORT} grid={N}x{N}", flush=True)
    # always start from a clean, correct grid (walls cleared) so every launch is predictable
    print(f"[boot] building {N}x{N} grid ...", flush=True)
    build_grid_full()
    print(f"[boot] nodes={stats['nodes']} edges={stats['edges']} "
          f"arcs={stats['arcs']} cch={stats['cch_ms']:.1f}ms", flush=True)

boot()

if __name__ == "__main__":
    srv.run(host="127.0.0.1", port=PORT, threaded=True)
