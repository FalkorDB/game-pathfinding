# Game-Grid Pathfinding · FalkorDB (CCH vs Dijkstra vs A\*)

An abstract character on a 3D grid finds shortest paths through FalkorDB. Left-click
to walk, right-click / drag to build walls. You pick which algorithm finds the path —
**CCH**, **Dijkstra** (`algo.SPpaths`), or **A\*** (`algo.AStar`) — and the panel
shows what each one actually costs when the map changes.

A companion to the [road-network CCH demo](https://github.com/FalkorDB/CCH-demo):
that one shows CCH *winning* on a real, million-node road network; this one shows
where CCH is the *wrong* tool.

## The story this demo tells

> **Road network** (stable, near-planar graph) → **CCH dominates.**
> **Game grid** (dynamic, high-treewidth graph) → **A\* dominates; CCH is the wrong tool.**

Grids are a *hard* case for contraction hierarchies. They have high treewidth, so
contraction explodes the hierarchy (hundreds of thousands of arcs even on a modest
grid). The CCH *query* is quick — quicker than a plain Dijkstra here — but building
the hierarchy is expensive, and on a dynamic grid you pay that build again on every
change. A\*, guided by a coordinate heuristic, walks almost straight to the goal
with **zero** preprocessing — so it wins outright.

A wall change is a *topology* change, so keeping the CCH correct means **rebuilding
the whole hierarchy**. FalkorDB's CCH is a self-maintaining path index — it will
rebuild automatically at write-commit — but on a high-treewidth grid that auto-
maintenance *is* a full rebuild on every edit. This demo makes the cost visible: it
drops the CCH index while you edit walls (so edits stay cheap) and rebuilds it on
demand (`CREATE CCH INDEX`), timing that rebuild. In a game where obstacles change
constantly, that preprocessing is wasted work — A\* and Dijkstra need none.

Suggested live flow: open on **CCH** → route a few times, note the rebuild cost each
time you drop a wall → hit **Compare all 3** → switch to **A\*** and drop walls
freely (re-routes in well under 1 ms, no rebuild).

## Measured results (illustrative — FalkorDB CCH on a grid)

**Three algorithms, same query, empty grid** (DB-internal `run_time_ms`, median).
The `CCH arcs` column is from an earlier build that surfaced an arc count; the
current `CREATE CCH INDEX` DDL exposes no arc count, so the app shows a built ✓ and
the rebuild time instead. The scaling story is unchanged:

| grid | Dijkstra | A\* | CCH query | CCH build | CCH arcs |
|------|----------|-----|-----------|-----------|----------|
| 30×30 | 0.17 ms | **0.04 ms** | 0.08 ms | 8 ms | 16.7K |
| 50×50 | 0.42 ms | **0.05 ms** | 0.18 ms | 21 ms | 61K |
| 100×100 | 1.65 ms | **0.08 ms** | 0.63 ms | 106 ms | 301K |
| 150×150 | 4.44 ms | **0.10 ms** | 1.33 ms | 299 ms | 778K |

All three return the **identical optimal weight** — it's algorithm *fit*, not
accuracy. A\* is ~4×–40× faster than Dijkstra; CCH *queries* now beat Dijkstra too,
but CCH must rebuild its whole index on every change (`CREATE CCH INDEX`: ~7 ms at
30×30 → hundreds of ms on larger grids) — preprocessing A\* and Dijkstra never pay.

Contrast with the [road-network demo](https://github.com/FalkorDB/CCH-demo):
California roads, 1.25M nodes on a stable, near-planar graph — few shortcut arcs per
node, and no rebuilds because the map doesn't change. There, CCH is the clear winner.

## Prerequisites

- **Docker** (Docker Desktop running) — `run.sh` starts the official
  **`falkordb/falkordb:v4.22.0`** image, which ships the CCH path index
  (`CREATE CCH INDEX` / `db.idx.cch.query`).
- **Python 3.12** — a virtualenv with `flask` + `falkordb` is created automatically.

## Run

```bash
./run.sh                 # FalkorDB (Docker, v4.22.0) + app on http://localhost:8090
./run.sh --n 50          # bigger grid = more dramatic CCH struggle
./run.sh stop            # stop the app + remove the FalkorDB container
./run.sh --help          # options + environment overrides
```

`run.sh` starts **FalkorDB in Docker** (`falkordb/falkordb:v4.22.0`, `--cpus 8
--memory 4g`; override with `FALKORDB_IMAGE`/`FALKORDB_CPUS`/`FALKORDB_MEMORY`) on
:6379, creates a `./venv` with `flask`+`falkordb` on first run, then serves the app
(:8090), building the grid + CCH on boot. If a FalkorDB is already listening on the
DB port it's reused. Then open **http://localhost:8090**.

## Files

| file | purpose |
|------|---------|
| `app.py` | Flask backend: builds the grid + CCH, serves routing for all 3 algorithms |
| `static/index.html` | 3D UI (Three.js): grid, glowing character, algorithm selector, live stats |
| `common/` | shared visual system (`design.css` + `grid3d.js`), served at `/common/*` |
| `bench.py` | CCH build + query scaling sweep across grid sizes |
| `compare.py` | rigorous CCH vs Dijkstra vs A\* comparison (the table above) |
| `run.sh` | one-command launcher (FalkorDB container + venv + app) |

```bash
SIZES=30,50,100,150 REPS=40 ./venv/bin/python compare.py   # reproduce the numbers
```

## Data model

- `(:Cell {id, x, y, lat, lon})` — one node per grid cell. `lat`/`lon` are **fake
  coords scaled to ~1 metre per cell**, so A\*'s great-circle heuristic is both
  admissible and well-scaled against the `STEP` weights.
- `[:STEP {w}]` — 8-connected edges, both directions. `w = 1` orthogonal,
  `sqrt(2)` diagonal. Diagonals never cut a wall corner (an edge exists only if the
  two shared orthogonal cells are also free).
- **The CCH adds nothing to the graph** — it's a CCH path index (built with
  `CREATE CCH INDEX`, queried with `db.idx.cch.query`) whose shortcut arcs and node
  ranks live inside the index, not as `SHORTCUT` edges / `rank` properties.

**A wall** removes a cell's incident `STEP` edges (and the corner-diagonals it
gates) via a small local edit; in CCH mode the index is then rebuilt
(`DROP CCH INDEX` + `CREATE CCH INDEX`) and the rebuild is timed.

## Endpoints

- `GET /` — the UI
- `GET /meta` — grid size, start cell, walls, node count, CCH index status, initial CCH ms
- `GET /route?algo=cch|dijkstra|astar&sx&sy&tx&ty` — shortest path as `[x,y]` cells
  (CCH mode rebuilds the hierarchy first if walls changed since the last build)
- `POST /obstacle {x, y, algo, [sx,sy,tx,ty]}` — toggle a wall; rebuild CCH only in
  CCH mode; optionally re-route in one round-trip
- `GET /compare?sx&sy&tx&ty` — race all three on the same query
- `POST /reset` — clear walls, rebuild grid + CCH

## CCH / algorithm signatures (FalkorDB CCH build)

```cypher
CREATE CCH INDEX FOR ()-[e:STEP]->() ON (e.w)
DROP   CCH INDEX FOR ()-[e:STEP]->() ON (e.w)
CALL db.idx.cch.query({sourceNode, targetNode, relTypes:['STEP'], weightProp:'w'})
               YIELD pathWeight, path
CALL algo.SPpaths({sourceNode, targetNode, relTypes:['STEP'], weightProp:'w'}) YIELD pathWeight, path
CALL algo.AStar({sourceNode, targetNode, relTypes:['STEP'], weightProp:'w',
               latitudeProperty:'lat', longitudeProperty:'lon'}) YIELD pathWeight, path
```

The CCH index is created/dropped with the `CREATE/DROP CCH INDEX` DDL (not the older
`db.idx.cch.create`/`.drop` procedures); routing is answered by `db.idx.cch.query`.

Note: `db.idx.cch.query`'s `path` is already the fully-unpacked cell-by-cell path —
no manual shortcut unpacking needed to animate the character.
