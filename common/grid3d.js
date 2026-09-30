/* FalkorDB webinar demos — shared Three.js grid renderer.
 *
 * Grid3D.create(host, {nx, ny, zoom, panX, elevation, aimZ}) -> a `world` with:
 *   world.scene / camera / renderer / nx / ny
 *   world.wx(x), world.wz(y)          cell -> world coords (plane at y=0)
 *   world.resize(), world.render()
 *   world.buildGrid(isWall)           (re)build the instanced floor/rack grid
 *   world.setCell(x, y, isWall)       raise/lower + recolor one cell
 *   world.tintCells(cells, hexOrNull) path highlight (null resets to floor)
 *   world.raycastCell(clientX, clientY) -> {x, y}
 *   world.addMarkers(cells, hex)      static glowing pads (stations)
 *   world.makeAgents(max)             fleet layer -> {set(i,x,y,hex), setCount(n), flush()}
 *   world.makeTrails(max, len)        trails      -> {set(i,cells,hex), hideFrom(n), flush()}
 * Requires THREE (loaded before this file).
 */
(function (global) {
  const PALETTE = {
    bg: 0x060608, floor: 0xe9e7df, wall: 0x2a2c33,
    accent: 0x7fe3ff, path: 0xaee7f5, pick: 0x46e0a0, pack: 0xffb347,
  };

  function create(host, opts) {
    opts = opts || {};
    const nx = opts.nx, ny = opts.ny;
    const ZOOM = opts.zoom || 1.14;
    const PAN_X = (opts.panX != null ? opts.panX : -0.15);   // fraction of nx
    const EL = (opts.elevation || 56) * Math.PI / 180;
    const AIMZ = (opts.aimZ != null ? opts.aimZ : 2.0);
    const CX = nx / 2, CY = ny / 2;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(PALETTE.bg);
    scene.fog = new THREE.Fog(PALETTE.bg, Math.max(nx, ny) * 1.45, Math.max(nx, ny) * 3.4);
    const cam = new THREE.PerspectiveCamera(42, 1, 0.1, 2000);
    scene.add(new THREE.AmbientLight(0x6a737f, 0.95));
    const dir = new THREE.DirectionalLight(0xffffff, 0.5);
    dir.position.set(nx * 0.35, nx * 1.1, ny * 0.35); scene.add(dir);

    const wx = (x) => x - CX + 0.5, wz = (y) => y - CY + 0.5;
    const _m = new THREE.Matrix4(), _q = new THREE.Quaternion(),
          _p = new THREE.Vector3(), _s = new THREE.Vector3();
    const FLOOR = new THREE.Color(PALETTE.floor), WALL = new THREE.Color(PALETTE.wall),
          PATH = new THREE.Color(PALETTE.path);

    // ---- instanced floor/wall grid ----
    const cellGeo = new THREE.BoxGeometry(1, 1, 1);
    const gridMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.88, metalness: 0.02 });
    let grid = null, isWallFn = () => false, tinted = [];
    const idx = (x, y) => y * nx + x;
    function placeCell(x, y) {
      const wall = isWallFn(x, y);
      _p.set(wx(x), wall ? 0.42 : 0, wz(y));
      _s.set(0.92, wall ? 1.0 : 0.16, 0.92);
      grid.setMatrixAt(idx(x, y), _m.compose(_p, _q, _s));
      grid.setColorAt(idx(x, y), wall ? WALL : FLOOR);
    }
    function buildGrid(isWall) {
      isWallFn = isWall || (() => false);
      if (grid) { scene.remove(grid); grid.dispose(); }
      tinted = [];
      grid = new THREE.InstancedMesh(cellGeo, gridMat, nx * ny);
      grid.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) placeCell(x, y);
      grid.instanceMatrix.needsUpdate = true; grid.instanceColor.needsUpdate = true;
      scene.add(grid);
    }
    function setCell(x, y, isWall) {
      isWallFn = (function (prev) { return (px, py) => (px === x && py === y) ? isWall : prev(px, py); })(isWallFn);
      placeCell(x, y);
      grid.instanceMatrix.needsUpdate = true; grid.instanceColor.needsUpdate = true;
    }
    function tintCells(cells, hex) {
      for (const [x, y] of tinted) if (!isWallFn(x, y)) grid.setColorAt(idx(x, y), FLOOR);
      tinted = [];
      if (cells && hex !== null) {
        const c = hex == null ? PATH : new THREE.Color(hex);
        for (const p of cells) if (!isWallFn(p[0], p[1])) { grid.setColorAt(idx(p[0], p[1]), c); tinted.push([p[0], p[1]]); }
      }
      grid.instanceColor.needsUpdate = true;
    }

    // ---- picking ----
    const ray = new THREE.Raycaster(), ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    function raycastCell(px, py) {
      const rect = renderer.domElement.getBoundingClientRect();
      const ndc = new THREE.Vector2(((px - rect.left) / rect.width) * 2 - 1, -((py - rect.top) / rect.height) * 2 + 1);
      ray.setFromCamera(ndc, cam); const hit = new THREE.Vector3();
      if (!ray.ray.intersectPlane(ground, hit)) return { x: -1, y: -1 };
      return { x: Math.floor(hit.x + CX), y: Math.floor(hit.z + CY) };
    }

    // ---- static glowing markers (stations) ----
    function addMarkers(cells, hex) {
      const g = new THREE.BoxGeometry(0.7, 0.06, 0.7);
      const m = new THREE.InstancedMesh(g, new THREE.MeshBasicMaterial({ color: hex }), cells.length);
      cells.forEach((c, i) => { _p.set(wx(c[0]), 0.12, wz(c[1])); _s.set(1, 1, 1); m.setMatrixAt(i, _m.compose(_p, _q, _s)); });
      scene.add(m); return m;
    }

    // ---- fleet agents (unlit glowing cubes, per-instance colour) ----
    function makeAgents(max, size, y) {
      const s0 = size || 0.6, yy = (y != null ? y : 0.4);
      const g = new THREE.BoxGeometry(s0, s0 * 0.82, s0);
      const mesh = new THREE.InstancedMesh(g, new THREE.MeshBasicMaterial({ color: 0xffffff }), max);
      mesh.count = 0; scene.add(mesh);
      const _c = new THREE.Color();
      return {
        mesh,
        setCount(n) { mesh.count = n; },
        set(i, x, yc, hex) {
          _p.set(wx(x), yy, wz(yc)); _s.set(1, 1, 1);
          mesh.setMatrixAt(i, _m.compose(_p, _q, _s));
          mesh.setColorAt(i, _c.set(hex));
        },
        flush() { mesh.instanceMatrix.needsUpdate = true; if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true; },
      };
    }

    // ---- comet trail: additive glowing dots that fade out (clearly visible) ----
    function makeTrailDots(max, life, size) {
      const LIFE = life || 1.3;
      const g = new THREE.PlaneGeometry(size || 0.55, size || 0.55); g.rotateX(-Math.PI / 2);   // flat on the floor
      const mat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
      const mesh = new THREE.InstancedMesh(g, mat, max); mesh.count = max;
      mesh.frustumCulled = false; scene.add(mesh);
      const dots = Array.from({ length: max }, () => ({ x: 0, z: 0, age: 1e9, r: 0, g: 0, b: 0 }));
      let cur = 0; const _c = new THREE.Color();
      return {
        drop(x, y, hex) { _c.set(hex); const d = dots[cur]; d.x = wx(x); d.z = wz(y); d.age = 0; d.r = _c.r; d.g = _c.g; d.b = _c.b; cur = (cur + 1) % max; },
        update(dt) {
          for (let i = 0; i < max; i++) {
            const d = dots[i]; d.age += dt;
            const a = d.age < LIFE ? 1 - d.age / LIFE : 0;
            _p.set(d.x, 0.2, d.z); _s.set(a * 0.95 + 1e-3, 1, a * 0.95 + 1e-3);
            mesh.setMatrixAt(i, _m.compose(_p, _q, _s));
            mesh.setColorAt(i, _c.setRGB(d.r * a, d.g * a, d.b * a));
          }
          mesh.instanceMatrix.needsUpdate = true; if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
        },
      };
    }

    // ---- trails (one vertex-coloured line per agent, bright head -> dark tail) ----
    function makeTrails(max, len) {
      const mat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.85 });
      const lines = [];
      const _c = new THREE.Color();
      function line(i) {
        if (!lines[i]) {
          const geo = new THREE.BufferGeometry();
          geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(len * 3), 3));
          geo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(len * 3), 3));
          const l = new THREE.Line(geo, mat); l.frustumCulled = false; scene.add(l); lines[i] = l;
        }
        return lines[i];
      }
      return {
        set(i, cells, hex, headX, headY) {
          const l = line(i), pos = l.geometry.attributes.position.array, col = l.geometry.attributes.color.array;
          _c.set(hex);
          for (let t = 0; t < len; t++) {
            const src = cells[Math.max(0, cells.length - len + t)] || cells[0];
            pos[t * 3] = wx(src[0]); pos[t * 3 + 1] = 0.28; pos[t * 3 + 2] = wz(src[1]);
            const f = t / (len - 1);
            col[t * 3] = _c.r * f; col[t * 3 + 1] = _c.g * f; col[t * 3 + 2] = _c.b * f;
          }
          if (headX != null) { pos[(len - 1) * 3] = wx(headX); pos[(len - 1) * 3 + 2] = wz(headY); }
          l.geometry.attributes.position.needsUpdate = true; l.geometry.attributes.color.needsUpdate = true;
          l.visible = true;
        },
        hideFrom(n) { for (let i = n; i < lines.length; i++) if (lines[i]) lines[i].visible = false; },
      };
    }

    function resize() {
      const w = host.clientWidth, h = host.clientHeight;
      renderer.setSize(w, h); cam.aspect = w / h;
      const vfov = cam.fov * Math.PI / 180, R = 0.5 * Math.hypot(nx, ny) * 1.05;
      const dv = R / Math.tan(vfov / 2), hfov = 2 * Math.atan(Math.tan(vfov / 2) * cam.aspect), dh = R / Math.tan(hfov / 2);
      const dist = Math.max(dv, dh) * ZOOM, aimX = PAN_X * nx;
      cam.position.set(aimX, Math.sin(EL) * dist, AIMZ + Math.cos(EL) * dist);
      cam.lookAt(aimX, 0, AIMZ); cam.updateProjectionMatrix();
    }
    function render() { renderer.render(scene, cam); }

    return {
      scene, camera: cam, renderer, nx, ny, wx, wz, THREE_PALETTE: PALETTE,
      resize, render, buildGrid, setCell, tintCells, raycastCell, addMarkers, makeAgents, makeTrails, makeTrailDots,
    };
  }

  global.Grid3D = { create, PALETTE };
})(window);
