/*
 * 3D view of the selected vehicle on the Battery RUL page (three.js, bundled locally in /static/vendor).
 * Generic models built in code (no downloaded model files, no real brand's design): a compact electric SUV (4W),
 * an electric auto-rickshaw (3W) and an electric scooter (2W). Smooth lofted body panels, clear-coat paint, alloy
 * wheels, studio reflections and soft shadows.
 * Drag to turn it, scroll to zoom, "Show battery" makes the body see-through so the battery pack shows.
 * After a prediction the battery pack is coloured by the estimated health (green / amber / red).
 */
(function () {
  "use strict";
  if (typeof THREE === "undefined") return;
  if (THREE.ColorManagement) THREE.ColorManagement.legacyMode = false;

  const LABEL = { "4W": "Car (4-wheeler)", "3W": "Auto-rickshaw (3-wheeler)", "2W": "Scooter (2-wheeler)" };
  const PAINT = { "4W": 0xaeb3b9, "3W": 0x1f9a55, "2W": 0xc72c3b };

  // ------------------------------------------------------------------ materials
  function materials(type) {
    const D = THREE.DoubleSide;
    const m = {
      paint: new THREE.MeshPhysicalMaterial({ color: PAINT[type], metalness: 0.55, roughness: 0.3, clearcoat: 1, clearcoatRoughness: 0.05, side: D }),
      roof: new THREE.MeshPhysicalMaterial({ color: 0x0c0d0f, metalness: 0.4, roughness: 0.25, clearcoat: 1, clearcoatRoughness: 0.05, side: D }),
      glass: new THREE.MeshPhysicalMaterial({ color: 0x0d141b, metalness: 0.1, roughness: 0.04, clearcoat: 1, transparent: true, opacity: 0.93, side: D }),
      trim: new THREE.MeshStandardMaterial({ color: 0x17181a, metalness: 0.05, roughness: 0.7, side: D }),
      fabric: new THREE.MeshStandardMaterial({ color: 0x1c1d1f, roughness: 0.85, side: D }),
      chrome: new THREE.MeshStandardMaterial({ color: 0xdfe2e6, metalness: 1, roughness: 0.18 }),
      alloy: new THREE.MeshStandardMaterial({ color: 0xc8ccd2, metalness: 0.9, roughness: 0.28 }),
      gun: new THREE.MeshStandardMaterial({ color: 0x2b2e33, metalness: 0.7, roughness: 0.4 }),
      tyre: new THREE.MeshStandardMaterial({ color: 0x151515, roughness: 0.88 }),
      seat: new THREE.MeshStandardMaterial({ color: 0x232325, roughness: 0.55 }),
      head: new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xf2f6ff, emissiveIntensity: 2.2 }),
      tail: new THREE.MeshStandardMaterial({ color: 0xb3131b, emissive: 0xe0141e, emissiveIntensity: 1.6 }),
      amber: new THREE.MeshStandardMaterial({ color: 0xffa31a, emissive: 0xff8c00, emissiveIntensity: 1.0 }),
      battery: new THREE.MeshStandardMaterial({ color: 0x3987e5, emissive: 0x3987e5, emissiveIntensity: 0.5, roughness: 0.4 }),
    };
    m.shell = [m.paint, m.roof, m.glass, m.trim, m.fabric, m.seat];
    return m;
  }

  // ------------------------------------------------------------------ helpers
  const smooth = (t) => t * t * (3 - 2 * t);
  function table(pts) {                          // smooth interpolation through [x, y] points (sorted by x)
    return (x) => {
      if (x <= pts[0][0]) return pts[0][1];
      for (let i = 1; i < pts.length; i++) {
        if (x <= pts[i][0]) {
          const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
          return y0 + (y1 - y0) * smooth((x - x0) / (x1 - x0));
        }
      }
      return pts[pts.length - 1][1];
    };
  }
  const pinch = (x0, x1, p = 8) => (x) => {        // rounds the ends of a loft in plan view
    const u = Math.min(1, Math.abs((x - (x0 + x1) / 2) / ((x1 - x0) / 2)));
    return Math.pow(Math.max(0, 1 - Math.pow(u, p)), 1 / p);
  };
  const arch = (base, wheels) => (x) => {          // wheel arches cut into the bottom line
    let y = base(x);
    wheels.forEach(([xw, yw, r]) => { const d = x - xw; if (Math.abs(d) < r) y = Math.max(y, yw + Math.sqrt(r * r - d * d)); });
    return y;
  };
  function stations(x0, x1, n) {                  // denser at both ends
    return Array.from({ length: n + 1 }, (_, k) => x0 + (x1 - x0) * (1 - Math.cos(Math.PI * k / n)) / 2);
  }
  // rounded-box cross-section (superellipse) with tumblehome
  function section(W, yb, yt, n = 44, e = 6, tumble = 0.07) {
    const out = [];
    for (let k = 0; k < n; k++) {
      const t = (2 * Math.PI * k) / n, c = Math.cos(t), s = Math.sin(t);
      const y = (yb + yt) / 2 + ((yt - yb) / 2) * Math.sign(s) * Math.pow(Math.abs(s), 2 / e);
      const f = (y - yb) / Math.max(1e-6, yt - yb);
      out.push([W * Math.sign(c) * Math.pow(Math.abs(c), 2 / e) * (1 - tumble * f * f) * (1 - 0.05 * Math.pow(1 - f, 4)), y]);
    }
    return out;
  }
  // loft along x: ring(x) -> [[z, y], ...]; matOf(x, j) -> material index; closed ring or open strip
  function loft(xs, ring, mats, matOf = () => 0, closed = true) {
    const rings = xs.map(ring), n = rings[0].length, pos = [];
    rings.forEach((r, i) => r.forEach(([z, y]) => pos.push(xs[i], y, z)));
    const idx = mats.map(() => []);
    for (let i = 0; i < xs.length - 1; i++) {
      for (let j = 0; j < (closed ? n : n - 1); j++) {
        const a = i * n + j, b = i * n + ((j + 1) % n), c = (i + 1) * n + j, d = (i + 1) * n + ((j + 1) % n);
        idx[matOf((xs[i] + xs[i + 1]) / 2, j)].push(a, c, b, b, c, d);
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    const all = []; let start = 0;
    idx.forEach((list, k) => { g.addGroup(start, list.length, k); start += list.length; all.push(...list); });
    g.setIndex(all);
    g.computeVertexNormals();
    return new THREE.Mesh(g, mats.length === 1 ? mats[0] : mats);
  }
  function rbox(w, h, d, r, mat, x, y, z) {       // rounded box
    const s = new THREE.Shape(), hw = w / 2 - r, hh = h / 2 - r;
    s.moveTo(-hw, -h / 2); s.lineTo(hw, -h / 2); s.quadraticCurveTo(w / 2, -h / 2, w / 2, -hh); s.lineTo(w / 2, hh);
    s.quadraticCurveTo(w / 2, h / 2, hw, h / 2); s.lineTo(-hw, h / 2); s.quadraticCurveTo(-w / 2, h / 2, -w / 2, hh);
    s.lineTo(-w / 2, -hh); s.quadraticCurveTo(-w / 2, -h / 2, -hw, -h / 2);
    const depth = Math.max(0.001, d - 2 * r);
    const g = new THREE.ExtrudeGeometry(s, { depth, bevelEnabled: true, bevelThickness: r, bevelSize: r * 0.6, bevelSegments: 4, curveSegments: 6 });
    g.translate(0, 0, -depth / 2);
    const m = new THREE.Mesh(g, mat); m.position.set(x, y, z); return m;
  }
  function box(w, h, d, mat, x, y, z) { const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat); m.position.set(x, y, z); return m; }
  function rod(mat, a, b, r, seg = 12) {
    const va = new THREE.Vector3(...a), vb = new THREE.Vector3(...b);
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, va.distanceTo(vb), seg), mat);
    m.position.copy(va).add(vb).multiplyScalar(0.5);
    m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), vb.clone().sub(va).normalize());
    return m;
  }
  function disc(r, t, mat, x, y, z, axis = "z") {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, t, 32), mat);
    if (axis === "z") m.rotation.x = Math.PI / 2; else if (axis === "x") m.rotation.z = Math.PI / 2;
    m.position.set(x, y, z); return m;
  }

  // alloy wheel, axis along z, outer face towards +z (flipped for the left side)
  function wheel(m, R, w, x, y, z, spokes = 5) {
    const g = new THREE.Group();
    const prof = [[R * 0.7, -w / 2], [R * 0.93, -w / 2], [R * 0.985, -w / 2 + w * 0.12], [R, -w / 2 + w * 0.3],
                  [R, w / 2 - w * 0.3], [R * 0.985, w / 2 - w * 0.12], [R * 0.93, w / 2], [R * 0.7, w / 2]].map(([a, b]) => new THREE.Vector2(a, b));
    const tyre = new THREE.Mesh(new THREE.LatheGeometry(prof, 56), m.tyre); tyre.rotation.x = Math.PI / 2; g.add(tyre);
    const barrel = new THREE.Mesh(new THREE.CylinderGeometry(R * 0.7, R * 0.7, w * 0.9, 40, 1, true), m.gun); barrel.rotation.x = Math.PI / 2; g.add(barrel);
    g.add(disc(R * 0.69, 0.01, m.gun, 0, 0, w * 0.18));                       // dark wheel face (recessed)
    g.add(disc(R * 0.45, 0.012, m.chrome, 0, 0, -w * 0.05));                  // brake disc behind the spokes
    const lip = new THREE.Mesh(new THREE.TorusGeometry(R * 0.7, R * 0.025, 8, 48), m.alloy); lip.position.z = w * 0.42; g.add(lip);
    for (let i = 0; i < spokes; i++) {
      [-0.11, 0.11].forEach((off) => {
        const a = (2 * Math.PI * i) / spokes + off, len = R * 0.56;
        const sp = box(len, R * 0.07, w * 0.12, m.alloy, 0, 0, w * 0.36);
        sp.position.x = Math.cos(a) * (R * 0.13 + len / 2); sp.position.y = Math.sin(a) * (R * 0.13 + len / 2);
        sp.rotation.z = a; g.add(sp);
      });
    }
    g.add(disc(R * 0.17, w * 0.2, m.alloy, 0, 0, w * 0.38));
    g.add(disc(R * 0.08, w * 0.22, m.gun, 0, 0, w * 0.4));
    if (z < 0) g.rotation.y = Math.PI;
    g.position.set(x, y, z);
    return g;
  }

  // ------------------------------------------------------------------ compact electric SUV (4W)
  function car(m) {
    const g = new THREE.Group(), add = (o) => { g.add(o); return o; };
    const L0 = -2.0, L1 = 2.0, W0 = 0.9;
    const FW = [1.32, 0.36, 0.47], RW = [-1.3, 0.36, 0.47];
    const W = (x) => W0 * pinch(L0, L1)(x);
    const bottom = arch(table([[-2.0, 0.5], [-1.82, 0.43], [-1.6, 0.42], [1.65, 0.42], [1.88, 0.44], [2.0, 0.47]]), [FW, RW]);
    const top = table([[-2.0, 1.02], [-1.82, 1.1], [-1.2, 1.08], [0.6, 1.02], [1.0, 1.0], [1.6, 0.93], [1.9, 0.86], [2.0, 0.8]]);
    add(loft(stations(L0, L1, 150), (x) => section(W(x), bottom(x), top(x)), [m.paint]));

    // glasshouse: glass sides and screens, black pillars and roof ("floating roof")
    const roof = table([[-1.93, 1.13], [-1.78, 1.42], [-1.6, 1.56], [-1.2, 1.6], [0.0, 1.62], [0.25, 1.56], [0.98, 1.0]]);
    const gh = (x) => {
      const b = top(x), r = Math.max(roof(x), b + 0.002), Wb = W(x) * 0.92, Wt = W(x) * 0.74, pts = [[-Wb, b], [-Wt, r - 0.07]];
      for (let k = 0; k <= 10; k++) { const z = -Wt * 0.96 + (Wt * 1.92 * k) / 10; pts.push([z, r + 0.025 * (1 - (z / Wt) ** 2)]); }
      return pts.concat([[Wt, r - 0.07], [Wb, b]]);
    };
    const side = (x) => x > -1.72 && x < 0.72 && !(x > -0.43 && x < -0.33);
    add(loft(stations(-1.93, 0.98, 120), gh, [m.glass, m.roof], (x, j) => {
      if (j === 0 || j === 13) return side(x) ? 0 : 1;
      return x > 0.27 || x < -1.66 ? 0 : 1;
    }, false));

    // dark wheel wells (seen through the arches), cladding, bumpers
    [FW, RW].forEach(([xw]) => add(box(0.9, 0.42, 1.5, m.trim, xw, 0.62, 0)));
    [FW, RW].forEach(([xw, yw, r]) => [1, -1].forEach((sd) => {
      const t = add(new THREE.Mesh(new THREE.TorusGeometry(r + 0.015, 0.035, 8, 32, Math.PI), m.trim));
      t.position.set(xw, yw, sd * 0.865); t.scale.z = 1.6;
    }));
    [1, -1].forEach((sd) => add(rbox(1.62, 0.1, 0.05, 0.02, m.trim, 0.01, 0.48, sd * 0.845)));
    add(rbox(0.05, 0.13, 1.0, 0.02, m.trim, 1.995, 0.58, 0));                // lower grille
    add(rbox(0.04, 0.03, 0.8, 0.012, m.chrome, 2.0, 0.49, 0));               // skid plate
    add(rbox(0.035, 0.022, 1.3, 0.01, m.head, 1.985, 0.785, 0));             // full-width light bar
    [1, -1].forEach((sd) => {
      add(rbox(0.05, 0.11, 0.3, 0.025, m.roof, 1.985, 0.68, sd * 0.52));      // lamp pods
      add(rbox(0.055, 0.03, 0.22, 0.01, m.head, 1.99, 0.69, sd * 0.53));
      add(rbox(0.05, 0.03, 0.12, 0.01, m.amber, 1.99, 0.645, sd * 0.57));
    });
    add(rbox(0.04, 0.035, 1.3, 0.012, m.tail, -1.99, 0.99, 0));             // rear light bar
    add(rbox(0.05, 0.13, 1.25, 0.03, m.trim, -1.985, 0.56, 0));            // rear bumper
    // mirrors, roof rails, door seams, charge flap
    [1, -1].forEach((sd) => {
      add(rbox(0.2, 0.1, 0.13, 0.04, m.roof, 0.74, 1.13, sd * 0.93));
      add(rod(m.roof, [0.78, 1.07, sd * 0.8], [0.76, 1.1, sd * 0.88], 0.02));
      add(rod(m.roof, [-1.5, 1.6, sd * 0.56], [-0.05, 1.655, sd * 0.58], 0.018));
      [0.66, -0.38].forEach((x) => add(box(0.006, 0.45, 0.004, m.trim, x, 0.75, sd * (W0 - 0.035))));
      [0.2, -0.85].forEach((x) => add(rbox(0.14, 0.022, 0.02, 0.008, m.chrome, x, 0.95, sd * (W0 - 0.045))));
    });
    // wheels
    [[FW[0], 0.79], [FW[0], -0.79], [RW[0], 0.79], [RW[0], -0.79]].forEach(([x, z]) => add(wheel(m, 0.355, 0.24, x, 0.36, z)));
    // battery pack under the floor
    const bat = add(box(2.3, 0.1, 1.3, m.battery, 0.0, 0.5, 0)); bat.userData.battery = true;
    [-0.77, 0, 0.77].forEach((x) => add(box(0.025, 0.105, 1.305, m.trim, x, 0.5, 0)));
    return g;
  }

  // ------------------------------------------------------------------ electric auto-rickshaw (3W)
  function auto(m) {
    const g = new THREE.Group(), add = (o) => { g.add(o); return o; };
    const L0 = -1.26, L1 = 1.56;
    const FW = [1.15, 0.28, 0.34], RW = [-0.8, 0.28, 0.35];
    const W = (x) => table([[-1.26, 0.66], [0.45, 0.66], [0.85, 0.5], [1.2, 0.43], [1.56, 0.36]])(x) * pinch(L0, L1)(x);
    const bottom = arch(table([[-1.26, 0.52], [-1.1, 0.42], [1.3, 0.42], [1.56, 0.6]]), [FW, RW]);
    const top = table([[-1.26, 1.12], [-0.62, 1.15], [-0.5, 0.78], [0.55, 0.74], [0.75, 1.08], [1.25, 1.1], [1.5, 0.94], [1.56, 0.84]]);
    add(loft(stations(L0, L1, 140), (x) => section(W(x), bottom(x), top(x), 44, 5, 0.05), [m.paint]));
    // canopy (soft top) and its back
    const cy = table([[-1.33, 1.8], [-1.1, 1.88], [0.9, 1.9], [1.24, 1.79]]);
    add(loft(stations(-1.33, 1.24, 80), (x) => section(0.72 * pinch(-1.33, 1.24, 10)(x), cy(x) - 0.045, cy(x) + 0.045, 36, 4, 0), [m.fabric]));
    add(rbox(0.05, 0.72, 1.34, 0.02, m.fabric, -1.27, 1.47, 0));
    [1, -1].forEach((sd) => add(rbox(0.42, 0.06, 0.03, 0.01, m.chrome, -1.0, 1.5, sd * 0.66)));    // grab rails
    // windscreen and posts
    const ws = add(rbox(0.025, 0.7, 0.86, 0.01, m.glass, 1.12, 1.45, 0)); ws.rotation.z = 0.07;
    [1, -1].forEach((sd) => {
      add(rod(m.trim, [1.15, 1.08, sd * 0.43], [1.08, 1.84, sd * 0.6], 0.022));
      add(rod(m.trim, [0.08, 0.75, sd * 0.64], [0.08, 1.86, sd * 0.66], 0.022));
      add(rod(m.trim, [-1.22, 1.13, sd * 0.64], [-1.24, 1.84, sd * 0.68], 0.022));
      add(rbox(0.05, 0.05, 0.1, 0.015, m.amber, 1.47, 0.97, sd * 0.24));
      add(rbox(0.04, 0.09, 0.12, 0.015, m.tail, -1.25, 0.9, sd * 0.5));
    });
    // seats, handlebar
    add(rbox(0.34, 0.1, 0.44, 0.04, m.seat, 0.4, 0.83, 0));
    add(rbox(0.1, 0.3, 0.44, 0.04, m.seat, 0.22, 1.0, 0));
    add(rbox(0.52, 0.12, 1.16, 0.05, m.seat, -0.66, 0.86, 0));
    add(rbox(0.12, 0.48, 1.16, 0.05, m.seat, -0.94, 1.14, 0));
    add(rod(m.trim, [0.92, 1.1, 0], [0.98, 0.9, 0], 0.03));
    add(rod(m.chrome, [0.92, 1.12, -0.36], [0.92, 1.12, 0.36], 0.018));
    [1, -1].forEach((sd) => add(rod(m.trim, [0.92, 1.12, sd * 0.28], [0.92, 1.12, sd * 0.4], 0.03)));
    // headlamp
    add(disc(0.11, 0.05, m.chrome, 1.555, 0.86, 0, "x"));
    add(disc(0.085, 0.06, m.head, 1.56, 0.86, 0, "x"));
    // wheels
    add(wheel(m, 0.27, 0.16, FW[0], 0.28, 0.0001));
    add(wheel(m, 0.27, 0.16, RW[0], 0.28, 0.72));
    add(wheel(m, 0.27, 0.16, RW[0], 0.28, -0.72));
    const bat = add(box(0.5, 0.2, 1.0, m.battery, -0.66, 0.66, 0)); bat.userData.battery = true;
    return g;
  }

  // ------------------------------------------------------------------ electric scooter (2W)
  function scooter(m) {
    const g = new THREE.Group(), add = (o) => { g.add(o); return o; };
    const RW = [-0.72, 0.25, 0.31];
    // rear body
    add(loft(stations(-1.16, -0.2, 90), (x) => section(table([[-1.16, 0.12], [-0.9, 0.19], [-0.3, 0.2], [-0.2, 0.18]])(x) * pinch(-1.16, -0.2)(x),
      arch(table([[-1.16, 0.68], [-0.9, 0.52], [-0.5, 0.42], [-0.2, 0.36]]), [RW])(x),
      table([[-1.16, 0.8], [-0.8, 0.84], [-0.3, 0.84], [-0.2, 0.72]])(x), 36, 4, 0.12), [m.paint]));
    // seat
    add(loft(stations(-1.08, -0.3, 60), (x) => section(0.16 * pinch(-1.08, -0.3, 6)(x), 0.82,
      table([[-1.08, 0.92], [-0.85, 0.96], [-0.6, 0.93], [-0.3, 0.98]])(x), 32, 3, 0.2), [m.seat]));
    // floorboard, leg shield, steering head with headlamp
    add(loft(stations(-0.3, 0.44, 40), (x) => section(0.17 * pinch(-0.3, 0.44, 10)(x), 0.31, 0.38, 28, 6, 0), [m.paint]));
    add(loft(stations(0.33, 0.67, 50), (x) => section(table([[0.33, 0.21], [0.67, 0.16]])(x) * pinch(0.33, 0.67, 6)(x),
      table([[0.33, 0.33], [0.67, 0.52]])(x), table([[0.33, 0.62], [0.42, 0.96], [0.55, 1.08], [0.67, 1.06]])(x), 32, 4, 0.1), [m.paint]));
    add(loft(stations(0.44, 0.74, 40), (x) => section(0.15 * pinch(0.44, 0.74, 6)(x), 1.04, table([[0.44, 1.26], [0.74, 1.2]])(x), 32, 4, 0.1), [m.paint]));
    add(rbox(0.03, 0.07, 0.2, 0.015, m.head, 0.74, 1.13, 0));
    [1, -1].forEach((sd) => add(rbox(0.03, 0.03, 0.05, 0.01, m.amber, 0.72, 1.06, sd * 0.11)));
    add(rbox(0.03, 0.05, 0.14, 0.01, m.tail, -1.16, 0.78, 0));
    // fork, front mudguard, handlebar, mirrors
    [1, -1].forEach((sd) => add(rod(m.chrome, [0.69, 0.25, sd * 0.06], [0.6, 1.05, sd * 0.05], 0.022)));
    const mg = add(new THREE.Mesh(new THREE.TorusGeometry(0.3, 0.04, 8, 24, Math.PI * 0.62), m.paint));
    mg.position.set(0.69, 0.25, 0); mg.rotation.z = Math.PI * 0.2; mg.scale.z = 1.8;
    add(rod(m.chrome, [0.54, 1.27, -0.34], [0.54, 1.27, 0.34], 0.016));
    [1, -1].forEach((sd) => {
      add(rod(m.trim, [0.54, 1.27, sd * 0.3], [0.54, 1.27, sd * 0.42], 0.028));
      add(rod(m.trim, [0.53, 1.28, sd * 0.24], [0.5, 1.46, sd * 0.29], 0.01));
      add(rbox(0.03, 0.08, 0.13, 0.03, m.trim, 0.5, 1.48, sd * 0.3));
    });
    add(wheel(m, 0.25, 0.1, 0.69, 0.25, 0.0001));
    add(wheel(m, 0.25, 0.11, RW[0], 0.25, 0.0001));
    const bat = add(box(0.46, 0.18, 0.22, m.battery, -0.66, 0.66, 0)); bat.userData.battery = true;
    return g;
  }

  const BUILD = { "4W": car, "3W": auto, "2W": scooter };

  // ------------------------------------------------------------------ studio lighting (reflections)
  function studio(renderer) {
    const s = new THREE.Scene();
    s.add(new THREE.Mesh(new THREE.BoxGeometry(24, 12, 24), new THREE.MeshBasicMaterial({ color: 0x4c4f55, side: THREE.BackSide })));
    const panel = (w, h, x, y, z, k) => {
      const p = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: new THREE.Color(k, k, k), side: THREE.DoubleSide }));
      p.position.set(x, y, z); p.lookAt(0, 0, 0); s.add(p);
    };
    panel(12, 5, 0, 5.8, 0, 5);  panel(2, 8, 11, 2, 2, 3); panel(2, 8, -11, 2, -2, 3);
    panel(9, 2.4, 2, 3, 11, 2.2); panel(9, 2.4, -2, 3, -11, 2.2);
    s.add(new THREE.Mesh(new THREE.PlaneGeometry(24, 24).rotateX(-Math.PI / 2).translate(0, -5.9, 0), new THREE.MeshBasicMaterial({ color: 0x8a8d92 })));
    const pm = new THREE.PMREMGenerator(renderer), tex = pm.fromScene(s, 0.04).texture;
    pm.dispose();
    return tex;
  }
  function blobTexture() {
    const c = document.createElement("canvas"); c.width = c.height = 128;
    const x = c.getContext("2d"), gr = x.createRadialGradient(64, 64, 2, 64, 64, 64);
    gr.addColorStop(0, "rgba(0,0,0,0.55)"); gr.addColorStop(0.55, "rgba(0,0,0,0.22)"); gr.addColorStop(1, "rgba(0,0,0,0)");
    x.fillStyle = gr; x.fillRect(0, 0, 128, 128);
    return new THREE.CanvasTexture(c);
  }
  function healthColour(soh) {
    if (soh == null) return 0x3987e5;
    return soh >= 85 ? 0x18b04a : soh >= 75 ? 0xf0a20f : 0xd93a3a;
  }

  // ------------------------------------------------------------------ viewer
  function mount(el, getVehicle) {
    const canvasBox = el.querySelector("[data-v3d-canvas]");
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputEncoding = THREE.sRGBEncoding;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    canvasBox.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.environment = studio(renderer);
    const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x80848c, 0.35));
    const sun = new THREE.DirectionalLight(0xffffff, 1.5);
    sun.position.set(2.5, 8, 3.5); sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024); sun.shadow.radius = 6; sun.shadow.bias = -0.0005;
    Object.assign(sun.shadow.camera, { left: -3.5, right: 3.5, top: 3.5, bottom: -3.5, near: 1, far: 20 });
    scene.add(sun);
    const catcher = new THREE.Mesh(new THREE.PlaneGeometry(14, 14), new THREE.ShadowMaterial({ opacity: 0.2 }));
    catcher.rotation.x = -Math.PI / 2; catcher.receiveShadow = true; scene.add(catcher);
    const blob = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: blobTexture(), transparent: true, depthWrite: false }));
    blob.rotation.x = -Math.PI / 2; blob.position.y = 0.003; scene.add(blob);

    const st = { type: null, name: null, model: null, mats: null, xray: false, auto: true, yaw: 0.85, pitch: 0.2, dist: 7,
                 dragging: false, lx: 0, ly: 0, idleUntil: 0, health: null, healthType: null };
    const xrayBtn = el.querySelector("[data-v3d-xray]");
    const home = () => Math.max(st.size, st.height * 1.75) * 1.6 + 0.5;

    function applyLook() {
      if (!st.model) return;
      st.mats.shell.forEach((mt) => {
        const isGlass = mt === st.mats.glass;
        mt.transparent = st.xray || isGlass;
        mt.opacity = st.xray ? (isGlass ? 0.08 : 0.16) : (isGlass ? 0.93 : 1);
        mt.depthWrite = !st.xray;
        mt.needsUpdate = true;
      });
      st.model.traverse((o) => { if (o.isMesh) o.castShadow = !st.xray || !!o.userData.battery; });
      const soh = st.healthType === st.type ? st.health : null;
      const c = healthColour(soh);
      st.mats.battery.color.setHex(c); st.mats.battery.emissive.setHex(c);
      st.mats.battery.emissiveIntensity = st.xray ? 0.9 : 0.5;
      xrayBtn.setAttribute("aria-pressed", String(st.xray));
      xrayBtn.title = st.xray ? "Hide battery" : "Show battery"; xrayBtn.setAttribute("aria-label", xrayBtn.title);
    }

    function build(type) {
      if (st.model) {
        scene.remove(st.model);
        st.model.traverse((o) => { if (o.isMesh) o.geometry.dispose(); });
        Object.values(st.mats).forEach((mt) => mt.dispose && mt.dispose());
      }
      st.mats = materials(type);
      const model = (BUILD[type] || car)(st.mats);
      const bb = new THREE.Box3().setFromObject(model), size = bb.getSize(new THREE.Vector3()), ctr = bb.getCenter(new THREE.Vector3());
      model.position.set(-ctr.x, 0, -ctr.z);
      st.model = new THREE.Group(); st.model.add(model); scene.add(st.model);
      st.size = Math.max(size.x, size.z); st.height = size.y;
      blob.scale.set(size.x * 1.25, size.z * 1.5 + 0.35, 1);
      st.dist = home();
      applyLook();
    }

    function sync() {
      const v = getVehicle() || {};
      const type = BUILD[v.type] ? v.type : "4W";
      if (type !== st.type) { st.type = type; build(type); cv.setAttribute("aria-label", `3D model: ${LABEL[type]}`); }
    }

    function resize() {
      const w = canvasBox.clientWidth, h = canvasBox.clientHeight;
      if (!w || !h) return false;
      const pr = renderer.getPixelRatio();
      if (renderer.domElement.width !== Math.round(w * pr) || renderer.domElement.height !== Math.round(h * pr)) {
        renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
      }
      return true;
    }

    // drag to turn, wheel to zoom, double-click to reset
    const cv = renderer.domElement;
    cv.style.touchAction = "pan-y";
    cv.addEventListener("pointerdown", (e) => { st.dragging = true; st.lx = e.clientX; st.ly = e.clientY; cv.setPointerCapture(e.pointerId); cv.classList.add("grabbing"); });
    cv.addEventListener("pointermove", (e) => {
      if (!st.dragging) return;
      st.yaw -= (e.clientX - st.lx) * 0.008;
      st.pitch = Math.min(1.2, Math.max(0.04, st.pitch + (e.clientY - st.ly) * 0.006));
      st.lx = e.clientX; st.ly = e.clientY; st.idleUntil = performance.now() + 2500;
    });
    const end = () => { st.dragging = false; cv.classList.remove("grabbing"); };
    cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      st.dist = Math.min(home() * 1.6, Math.max(home() * 0.55, st.dist * (1 + Math.sign(e.deltaY) * 0.08)));
    }, { passive: false });
    cv.addEventListener("dblclick", () => { st.dist = home(); st.pitch = 0.2; st.yaw = 0.85; });
    xrayBtn.addEventListener("click", () => { st.xray = !st.xray; applyLook(); });
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      st.auto = false;
    }

    let last = performance.now();
    function frame(now) {
      requestAnimationFrame(frame);
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      if (document.hidden || !el.offsetParent) return;          // page not on screen
      sync();
      if (!resize()) return;
      if (st.auto && !st.dragging && now > st.idleUntil) st.yaw += dt * 0.3;
      const h = st.height * 0.45;
      camera.position.set(Math.sin(st.yaw) * Math.cos(st.pitch) * st.dist, h + Math.sin(st.pitch) * st.dist, Math.cos(st.yaw) * Math.cos(st.pitch) * st.dist);
      camera.lookAt(0, h, 0);
      renderer.render(scene, camera);
    }
    requestAnimationFrame(frame);

    return {
      setHealth(soh, type) { st.health = soh == null ? null : Number(soh); st.healthType = type || st.type; applyLook(); },
      showBattery(on) { st.xray = !!on; applyLook(); },
    };
  }

  window.Vehicle3D = { mount };
})();
