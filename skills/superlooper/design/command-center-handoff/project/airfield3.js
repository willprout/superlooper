/* Superlooper airfield v3 — hub terminal, landmark tower, banner tags, fleet */
(function () {
  'use strict';

  function ctx2d(canvas, W, H, S) {
    canvas.width = W * S; canvas.height = H * S;
    const c = canvas.getContext('2d');
    c.imageSmoothingEnabled = false;
    c.setTransform(S, 0, 0, S, 0, 0);
    return c;
  }
  function R(c, x, y, w, h, col) { c.fillStyle = col; c.fillRect(x, y, w, h); }
  function P(c, x, y, col) { R(c, x, y, 1, 1, col); }
  function seeded(n) { const x = Math.sin(n * 99.173) * 43758.5453; return x - Math.floor(x); }
  function shade(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    return 'rgb(' + Math.round(((n >> 16) & 255) * f) + ',' + Math.round(((n >> 8) & 255) * f) + ',' + Math.round((n & 255) * f) + ')';
  }

  const PAL = {
    grass1: '#5CB350', grass2: '#54A849', tuft: '#4A9740', flower1: '#F2EFE4', flower2: '#F2D449',
    dirt: '#D9C28D', dirtDk: '#C6AE77', sand: '#EBDCA6', sandDk: '#D9C68B',
    water: '#4E8CD9', waterLt: '#74AAE6', waterDk: '#3F76BF',
    rock: '#8E9296', rockDk: '#6E7276', rockLt: '#B9BDBF', snow: '#F4F6F8',
    asphalt: '#565D66', asphaltDk: '#494F57', asphaltLt: '#646B74',
    mark: '#EDEDE6', markYl: '#E8C94F',
    taxi: '#8F969C', taxiDk: '#7C838A', apron: '#A9AFB5', apronDk: '#989EA4', apronLn: '#878E94',
    bldg: '#F2E6C8', bldgSh: '#DECFA9', roof: '#3D6FA8', roofLt: '#4E82BD', roofDk: '#325C8C',
    win: '#9FD9E8', winDk: '#7FB8CE', winLit: '#FFD97A',
    hang: '#C2C8CD', hangDk: '#A9AFB5', hangRoof: '#9AA2AA', hangRoofDk: '#878F97',
    twr: '#E4DFD0', twrDk: '#C6BFAD', glass: '#3A4652', glassLt: '#5A7484',
    out: '#28343C',
    tail: '#2E5EA8', repair: '#D9482E', chock: '#E8862E',
    sign: '#F7F4EA', red: '#D9482E',
    tree1: '#3E8A46', tree2: '#57A85E', tree3: '#6FBE74', trunk: '#7A5230',
    line: '#F2EEDC', path: '#CBCFD3'
  };

  // ---------- jets ----------
  function buildJet() {
    const Wd = 33, Ht = 42, cx = 16;
    const g = Array.from({ length: Ht }, () => Array(Wd).fill('.'));
    const set = (x, y, ch) => { if (x >= 1 && x < Wd - 1 && y >= 1 && y < Ht - 1) g[y][x] = ch; };
    const hs = (y, a, b, ch) => { for (let x = a; x <= b; x++) set(x, y, ch); };
    hs(2, cx, cx, 'W'); hs(3, cx - 1, cx + 1, 'W'); hs(4, cx - 2, cx + 2, 'W');
    for (let y = 5; y <= 31; y++) hs(y, cx - 3, cx + 3, 'W');
    hs(32, cx - 2, cx + 2, 'W'); hs(33, cx - 2, cx + 2, 'W'); hs(34, cx - 1, cx + 1, 'W'); hs(35, cx, cx, 'W');
    hs(5, cx - 2, cx + 2, 'C'); hs(6, cx - 1, cx + 1, 'C');
    for (let d = 4; d <= 13; d++) {
      const yl = Math.round(13 + (d - 4) * 0.95);
      const yt = Math.round(21 + (d - 4) * 0.55);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'W'); set(cx + d, y, 'W'); }
      set(cx - d, yt, 'L'); set(cx + d, yt, 'L');
    }
    set(cx - 13, 22, 'T'); set(cx - 13, 23, 'T'); set(cx + 13, 22, 'T'); set(cx + 13, 23, 'T');
    for (let d = 6; d <= 8; d++) for (let y = 12; y <= 18; y++) { set(cx - d, y, 'E'); set(cx + d, y, 'E'); }
    set(cx - 7, 12, 'D'); set(cx + 7, 12, 'D');
    set(cx - 7, 14, 'G'); set(cx + 7, 14, 'G');
    for (let d = 4; d <= 10; d++) {
      const yl = Math.round(29 + (d - 4) * 0.7);
      const yt = Math.round(33 + (d - 4) * 0.3);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'T'); set(cx + d, y, 'T'); }
    }
    for (let y = 26; y <= 35; y++) set(cx, y, 'F');
    set(cx - 1, 34, 'F'); set(cx + 1, 34, 'F');
    for (let y = 5; y <= 31; y++) if (g[y][cx + 3] === 'W') g[y][cx + 3] = 'L';
    set(cx + 2, 4, 'L');
    outline(g, Wd, Ht);
    return g.map(r => r.join(''));
  }
  function buildJetSmall() {
    const Wd = 25, Ht = 32, cx = 12;
    const g = Array.from({ length: Ht }, () => Array(Wd).fill('.'));
    const set = (x, y, ch) => { if (x >= 1 && x < Wd - 1 && y >= 1 && y < Ht - 1) g[y][x] = ch; };
    const hs = (y, a, b, ch) => { for (let x = a; x <= b; x++) set(x, y, ch); };
    hs(2, cx, cx, 'W'); hs(3, cx - 1, cx + 1, 'W');
    for (let y = 4; y <= 24; y++) hs(y, cx - 2, cx + 2, 'W');
    hs(25, cx - 1, cx + 1, 'W'); hs(26, cx, cx, 'W');
    hs(4, cx - 1, cx + 1, 'C');
    for (let d = 3; d <= 9; d++) {
      const yl = Math.round(9 + (d - 3) * 0.9);
      const yt = Math.round(15 + (d - 3) * 0.45);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'W'); set(cx + d, y, 'W'); }
      set(cx - d, yt, 'L'); set(cx + d, yt, 'L');
    }
    set(cx - 9, 15, 'T'); set(cx + 9, 15, 'T');
    for (let d = 4; d <= 5; d++) for (let y = 8; y <= 12; y++) { set(cx - d, y, 'E'); set(cx + d, y, 'E'); }
    for (let d = 3; d <= 7; d++) {
      const yl = Math.round(20 + (d - 3) * 0.7);
      const yt = Math.round(23 + (d - 3) * 0.3);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'T'); set(cx + d, y, 'T'); }
    }
    for (let y = 18; y <= 25; y++) set(cx, y, 'F');
    set(cx - 1, 24, 'F'); set(cx + 1, 24, 'F');
    for (let y = 4; y <= 24; y++) if (g[y][cx + 2] === 'W') g[y][cx + 2] = 'L';
    outline(g, Wd, Ht);
    return g.map(r => r.join(''));
  }
  function buildAwacs() {
    const base = buildJet().map(r => r.split(''));
    const cx = 16, cy = 17, rr = 6;
    for (let dy = -rr; dy <= rr; dy++) {
      const half = Math.round(Math.sqrt(rr * rr - dy * dy));
      for (let dx = -half; dx <= half; dx++) {
        const edge = Math.abs(dx) >= half || Math.abs(dy) === rr;
        base[cy + dy][cx + dx] = (edge || dy === 0) ? 'R' : 'r';
      }
    }
    return base.map(r => r.join(''));
  }
  function outline(g, Wd, Ht) {
    for (let y = 0; y < Ht; y++) for (let x = 0; x < Wd; x++) {
      if (g[y][x] !== '.') continue;
      if ((y > 0 && g[y - 1][x] !== '.' && g[y - 1][x] !== 'X') || (y < Ht - 1 && g[y + 1][x] !== '.' && g[y + 1][x] !== 'X') ||
          (x > 0 && g[y][x - 1] !== '.' && g[y][x - 1] !== 'X') || (x < Wd - 1 && g[y][x + 1] !== '.' && g[y][x + 1] !== 'X')) g[y][x] = 'X';
    }
  }
  const JET = buildJet(), JET_S = buildJetSmall(), AWACS = buildAwacs();

  function jetColors(o) {
    o = o || {};
    const T = o.tail || PAL.tail;
    const base = { X: PAL.out, W: '#F7F9FB', L: '#D6DDE4', C: '#8FD2E4', T: T, F: shade(T, 0.7), E: '#4A525A', D: '#2E373F', G: '#6A737B', R: '#5A646C', r: '#B9C2C9' };
    if (o.dim) return { X: '#3E484F', W: '#A2ABB2', L: '#909AA1', C: '#79929A', T: '#5A6B85', F: '#4E5D74', E: '#6B747B', D: '#4A545B', G: '#7C858C', R: '#5A646C', r: '#9AA4AC' };
    if (o.tint === 'night') return { X: '#1A2229', W: '#C9CFE0', L: '#ACB3C8', C: '#7FB8CE', T: shade(T, 0.85), F: shade(T, 0.6), E: '#3E464E', D: '#262E35', G: '#59626A', R: '#4A545C', r: '#9AA4B4' };
    if (o.tint === 'dusk') return { X: '#202A32', W: '#EAE6EE', L: '#CDCAD9', C: '#8CCEE0', T: T, F: shade(T, 0.7), E: '#454E56', D: '#2A333B', G: '#646D75', R: '#525C64', r: '#AEB7BE' };
    return base;
  }
  function sprite(c, map, x, y, dir, colors) {
    const h = map.length, w = map[0].length;
    for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) {
      const k = map[j][i]; if (k === '.') continue;
      const col = colors[k]; if (!col) continue;
      let tx, ty;
      if (dir === 'N') { tx = i; ty = j; }
      else if (dir === 'S') { tx = w - 1 - i; ty = h - 1 - j; }
      else if (dir === 'E') { tx = h - 1 - j; ty = i; }
      else { tx = j; ty = w - 1 - i; }
      P(c, x + tx, y + ty, col);
    }
  }
  function planeGeneric(c, map, x, y, dir, o) {
    o = o || {};
    const h = map.length, w = map[0].length;
    const sw = (dir === 'E' || dir === 'W') ? h : w, sh = (dir === 'E' || dir === 'W') ? w : h;
    const ox = o.air ? 6 : 2, oy = o.air ? 12 : 3;
    c.fillStyle = 'rgba(20,40,28,0.18)';
    if (dir === 'E' || dir === 'W') {
      c.fillRect(x + ox + 3, y + oy + Math.floor(sh / 2) - 4, sw - 8, 8);
      c.fillRect(x + ox + Math.floor(sw / 2) - 8, y + oy + 2, 11, sh - 5);
    } else {
      c.fillRect(x + ox + Math.floor(sw / 2) - 4, y + oy + 3, 8, sh - 8);
      c.fillRect(x + ox + 2, y + oy + Math.floor(sh / 2) - 8, sw - 5, 11);
    }
    sprite(c, map, x, y, dir, jetColors(o));
    if (o.chocks) {
      const mx = x + Math.floor(sw / 2);
      R(c, mx - 4, y + sh - 4, 2, 2, PAL.chock); R(c, mx + 2, y + sh - 4, 2, 2, PAL.chock);
      R(c, mx - 3, y + 3, 2, 2, PAL.chock); R(c, mx + 1, y + 3, 2, 2, PAL.chock);
    }
  }
  function plane(c, x, y, dir, o) { planeGeneric(c, JET, x, y, dir, o); }
  function planeSmall(c, x, y, dir, o) { planeGeneric(c, JET_S, x, y, dir, o); }
  function planeAwacs(c, x, y, dir, o) { planeGeneric(c, AWACS, x, y, dir, o); }

  function contrail(c, x, y, dir, kind) {
    if (!kind || kind === 'none') return;
    const dx = dir === 'W' ? 1 : dir === 'E' ? -1 : 0;
    const dy = dir === 'N' ? 1 : dir === 'S' ? -1 : 0;
    for (let k = 0; k < 14; k++) {
      if (kind === 'sputter' && (k === 2 || k === 3 || k === 6 || k === 7 || k === 8)) continue;
      if (kind === 'thin' && k % 2) continue;
      const d = 5 + k * (kind === 'crisp' ? 6 : 7);
      const big = k < 5 && kind === 'crisp';
      c.fillStyle = k < 4 ? 'rgba(255,255,255,0.95)' : k < 8 ? 'rgba(245,248,252,0.65)' : 'rgba(235,240,248,0.35)';
      c.fillRect(x + dx * d - (big ? 1 : 0), y + dy * d - (big ? 1 : 0), big ? 3 : 2, big ? 3 : 2);
    }
  }
  function banner(c, tailX, tailY, bx, by, bw, bh) {
    P(c, tailX - 2, tailY, PAL.out); P(c, tailX - 4, tailY + 1, PAL.out);
    P(c, bx + bw + 1, by + Math.floor(bh / 2) - 1, PAL.out); P(c, bx + bw + 2, by + Math.floor(bh / 2) - 2, PAL.out);
    R(c, bx - 1, by - 1, bw + 2, bh + 2, PAL.out);
    R(c, bx, by, bw, bh, '#FBFAF3');
    R(c, bx, by + bh - 2, bw, 2, '#E4E0D2');
    P(c, bx + Math.floor(bw * 0.33), by + 1, '#E4E0D2'); P(c, bx + Math.floor(bw * 0.33), by + bh - 3, '#E4E0D2');
    P(c, bx + Math.floor(bw * 0.66), by + 1, '#E4E0D2'); P(c, bx + Math.floor(bw * 0.66), by + bh - 3, '#E4E0D2');
    R(c, bx - 3, by + Math.floor(bh / 2) - 1, 2, 2, PAL.out);
  }

  // ---------- terrain ----------
  function grass(c, W, H, seed) {
    R(c, 0, 0, W, H, PAL.grass1);
    for (let y = 0; y < H; y += 14) R(c, 0, y, W, 7, PAL.grass2);
    for (let i = 0; i < W * H / 260; i++) {
      const x = Math.floor(seeded(i * 3 + seed) * W), y = Math.floor(seeded(i * 7 + seed + 1) * H);
      const r = seeded(i * 13 + seed + 2);
      if (r < 0.62) { P(c, x, y, PAL.tuft); P(c, x + 1, y, PAL.tuft); }
      else if (r < 0.84) P(c, x, y, PAL.flower1);
      else P(c, x, y, PAL.flower2);
    }
  }
  function blob(c, x, y, rows, col) {
    for (let j = 0; j < rows.length; j++) R(c, x + rows[j][0], y + j, rows[j][1], 1, col);
  }
  function tree(c, x, y, size) {
    if (size === 2) {
      blob(c, x, y, [[4, 5], [2, 9], [1, 11], [0, 13], [0, 13], [0, 13], [1, 11], [2, 9]], PAL.tree1);
      blob(c, x + 1, y, [[4, 4], [2, 6], [1, 5], [1, 4]], PAL.tree2);
      R(c, x + 3, y + 1, 3, 2, PAL.tree3); P(c, x + 8, y + 2, PAL.tree2); P(c, x + 3, y + 5, PAL.tree2);
      R(c, x + 1, y + 8, 11, 1, shade(PAL.tree1, 0.72));
      R(c, x + 5, y + 8, 3, 3, shade(PAL.tree1, 0.6)); R(c, x + 6, y + 9, 1, 3, PAL.trunk);
      R(c, x + 3, y + 12, 8, 1, 'rgba(30,70,40,0.22)');
    } else {
      blob(c, x, y, [[3, 3], [1, 7], [0, 9], [0, 9], [1, 7]], PAL.tree1);
      blob(c, x + 1, y, [[2, 3], [1, 4]], PAL.tree2);
      P(c, x + 2, y + 1, PAL.tree3);
      R(c, x + 1, y + 5, 7, 1, shade(PAL.tree1, 0.72));
      R(c, x + 4, y + 5, 1, 3, PAL.trunk);
      R(c, x + 2, y + 8, 5, 1, 'rgba(30,70,40,0.22)');
    }
  }
  function flowerBed(c, x, y, w) {
    R(c, x, y, w, 3, shade(PAL.tree2, 0.9));
    for (let i = 0; i < w; i += 3) { P(c, x + i, y + (i % 2), i % 6 ? PAL.flower2 : '#E86A8A'); P(c, x + i + 1, y + 1 + (i % 2 ? 0 : 1), PAL.flower1); }
  }

  function flightLine(c, x0, x1, y, col) {
    for (let x = x0; x < x1; x += 9) R(c, x, y, 5, 1, col);
    for (let x = x0 + 44; x < x1 - 20; x += 88) {
      P(c, x, y - 2, col); P(c, x + 1, y - 1, col); P(c, x + 2, y, col); P(c, x + 1, y + 1, col); P(c, x, y + 2, col);
    }
  }
  function arcDash(c, cx, cy, rx, ry, a0, a1, col) {
    const steps = Math.max(8, Math.round(Math.abs(a1 - a0) / 0.13));
    for (let i = 0; i <= steps; i++) {
      if (i % 2) continue;
      const a = a0 + (a1 - a0) * i / steps;
      R(c, Math.round(cx + Math.cos(a) * rx), Math.round(cy + Math.sin(a) * ry), 2, 1, col);
    }
  }

  // ---------- landmarks ----------
  function flagPoint(c, x, y) {
    R(c, x - 2, y + 6, 12, 5, PAL.dirt); R(c, x - 1, y + 7, 10, 3, PAL.sand);
    R(c, x + 3, y - 4, 1, 11, PAL.out);
    R(c, x + 4, y - 4, 6, 3, PAL.red); R(c, x + 4, y - 1, 4, 1, PAL.red);
  }
  const POND_SAND = [[12, 26], [7, 38], [4, 46], [2, 51], [1, 53], [0, 55], [0, 55], [0, 56], [1, 55], [1, 53], [2, 51], [4, 47], [7, 40], [12, 28]];
  const POND_WATER = [[13, 24], [9, 34], [6, 42], [4, 47], [3, 49], [2, 51], [2, 51], [3, 51], [3, 50], [4, 48], [6, 43], [9, 35], [14, 22]];
  function pond(c, x, y) {
    blob(c, x, y + 1, POND_SAND, PAL.sand);
    blob(c, x + 1, y + 1, POND_SAND, PAL.sandDk);
    blob(c, x, y + 1, POND_WATER, PAL.water);
    R(c, x + 8, y + 6, 7, 1, PAL.waterLt); R(c, x + 14, y + 12, 6, 1, PAL.waterDk);
    R(c, x + 40, y + 9, 5, 1, PAL.waterLt);
    blob(c, x + 18, y + 5, [[3, 9], [1, 13], [0, 15], [0, 15], [1, 13], [3, 9]], PAL.tree2);
    R(c, x + 19, y + 10, 13, 1, PAL.sand);
    tree(c, x + 21, y + 1, 1);
  }
  function ridge(c, x, y) {
    function peak(px0, w, h) {
      for (let r = 0; r < h; r++) {
        const half = Math.round((r + 1) * (w / 2) / h);
        R(c, px0 + Math.round(w / 2) - half - 1, y + r + (11 - h), 1, 1, PAL.out);
        R(c, px0 + Math.round(w / 2) + half, y + r + (11 - h), 1, 1, PAL.out);
        R(c, px0 + Math.round(w / 2) - half, y + r + (11 - h), half * 2, 1, r < 2 ? PAL.snow : (r % 3 === 0 ? PAL.rockLt : PAL.rock));
      }
    }
    peak(x, 18, 10); peak(x + 15, 22, 11); peak(x + 34, 16, 8);
    R(c, x + 1, y + 11, 48, 2, PAL.rockDk);
    R(c, x + 1, y + 13, 48, 1, 'rgba(30,70,40,0.25)');
  }
  function shoals(c, x, y) {
    blob(c, x, y, [[8, 18], [4, 26], [2, 30], [1, 32], [2, 30], [4, 26], [8, 18]], PAL.sand);
    blob(c, x + 1, y + 1, [[8, 16], [5, 23], [3, 27], [3, 27], [5, 23], [9, 15]], PAL.waterLt);
    R(c, x + 8, y + 3, 6, 1, '#9CC4EE'); R(c, x + 18, y + 5, 5, 1, '#9CC4EE');
    P(c, x + 12, y + 4, PAL.sand); P(c, x + 22, y + 3, PAL.sand); P(c, x + 16, y + 6, PAL.sand);
  }
  function windsock(c, x, y) {
    R(c, x, y, 1, 9, PAL.out);
    R(c, x + 1, y, 4, 3, PAL.chock); R(c, x + 5, y, 2, 3, '#F2A45E'); R(c, x + 7, y + 1, 1, 1, '#F2C08A');
  }

  // ---------- pavement ----------
  function runway(c, x, y, w, h) {
    R(c, x, y - 1, w, 1, PAL.asphaltDk);
    R(c, x, y, w, h, PAL.asphalt);
    R(c, x, y + h, w, 1, PAL.asphaltDk);
    for (let i = 0; i < 5; i++) {
      R(c, x + 3 + i * 3, y + 2, 1, h - 4, PAL.mark);
      R(c, x + w - 4 - i * 3, y + 2, 1, h - 4, PAL.mark);
    }
    const cy = y + Math.floor(h / 2);
    for (let cx = x + 24; cx < x + w - 24; cx += 16) R(c, cx, cy, 7, 1, PAL.mark);
    R(c, x + 26, y + 2, 3, 2, PAL.mark); R(c, x + 26, y + h - 4, 3, 2, PAL.mark);
    R(c, x + w - 29, y + 2, 3, 2, PAL.mark); R(c, x + w - 29, y + h - 4, 3, 2, PAL.mark);
    for (let i = 0; i < 18; i++) {
      P(c, x + 10 + Math.floor(seeded(i * 5 + y) * (w - 20)), y + 2 + Math.floor(seeded(i * 11 + y) * (h - 4)), PAL.asphaltLt);
    }
  }
  function taxiline(c, x, y, w, h, vert) {
    R(c, x, y, w, h, PAL.taxi);
    if (vert) { R(c, x, y, 1, h, PAL.taxiDk); R(c, x + w - 1, y, 1, h, PAL.taxiDk); for (let yy = y + 2; yy < y + h; yy += 5) R(c, x + Math.floor(w / 2), yy, 1, 3, PAL.markYl); }
    else { R(c, x, y, w, 1, PAL.taxiDk); R(c, x, y + h - 1, w, 1, PAL.taxiDk); for (let xx = x + 2; xx < x + w; xx += 5) R(c, xx, y + Math.floor(h / 2), 3, 1, PAL.markYl); }
  }

  // ---------- buildings ----------
  function wing(c, x, y, w) {
    R(c, x - 1, y - 1, w + 2, 30, PAL.out);
    R(c, x, y, w, 6, PAL.roof); R(c, x, y, w, 2, PAL.roofLt);
    for (let sx = x + 22; sx < x + w - 8; sx += 22) R(c, sx, y + 1, 1, 5, PAL.roofDk); // roof seams
    for (let sx = x + 10; sx < x + w - 14; sx += 44) { R(c, sx, y + 1, 7, 4, PAL.out); R(c, sx + 1, y + 2, 5, 2, '#D8DCE0'); P(c, sx + 2, y + 2, '#B9C0C6'); P(c, sx + 4, y + 2, '#B9C0C6'); } // rooftop HVAC
    R(c, x, y + 6, w, 11, PAL.bldg);
    R(c, x + 2, y + 8, w - 4, 6, PAL.winDk);
    R(c, x + 2, y + 8, w - 4, 2, PAL.win);
    for (let mx = x + 2; mx < x + w - 2; mx += 7) R(c, mx, y + 8, 1, 6, PAL.bldgSh);
    R(c, x, y + 17, w, 8, PAL.bldgSh);
    for (let mx = x + 6; mx < x + w - 6; mx += 14) R(c, mx, y + 19, 4, 4, PAL.win); // lower windows
    R(c, x, y + 25, w, 3, shade('#DECFA9', 0.85));
  }
  function gateCanopy(c, x, y) {
    R(c, x - 1, y - 1, 12, 6, PAL.out);
    R(c, x, y, 10, 3, '#F4F6F8'); R(c, x, y + 3, 10, 1, PAL.repair);
    P(c, x + 4, y - 2, '#6FE08A');
  }
  // jet-age gate-pier wing (field version)
  function wing5(c, x, y, w) {
    R(c, x - 1, y - 1, w + 2, 30, PAL.out);
    R(c, x, y, w, 4, '#F4F6F8');
    R(c, x, y + 4, w, 1, PAL.repair);
    for (let sx = x + 20; sx < x + w - 6; sx += 20) R(c, sx, y, 1, 4, '#DDE2E6');
    for (let sx = x + 12; sx < x + w - 14; sx += 44) { R(c, sx, y + 1, 6, 3, PAL.out); R(c, sx + 1, y + 2, 4, 1, '#C9D2DA'); }
    R(c, x, y + 5, w, 12, PAL.bldg);
    R(c, x + 2, y + 7, w - 4, 7, PAL.winDk);
    R(c, x + 2, y + 7, w - 4, 2, PAL.win);
    for (let mx = x + 2; mx < x + w - 2; mx += 7) R(c, mx, y + 7, 1, 7, PAL.bldgSh);
    R(c, x, y + 17, w, 8, PAL.bldg);
    for (let px = x + 8; px < x + w - 8; px += 16) { R(c, px, y + 19, 4, 4, PAL.out); R(c, px + 1, y + 20, 2, 2, PAL.win); }
    R(c, x, y + 25, w, 3, PAL.bldgSh);
  }
  // jet-age central pavilion with observation deck, radar + beacon (field version)
  function pavilion5(c, cx, y) {
    // deck props
    R(c, cx - 30, y, 60, 1, '#DDE2E6');
    for (let rx = cx - 30; rx < cx + 30; rx += 4) R(c, rx, y - 1, 1, 3, '#B9C2C9');
    blob(c, cx - 28, y - 7, [[3, 6], [1, 10], [0, 12], [0, 12]], PAL.out);
    blob(c, cx - 27, y - 6, [[3, 4], [1, 8], [0, 10]], '#F4F6F8');
    c.fillStyle = 'rgba(255,106,90,0.25)'; c.fillRect(cx + 20, y - 9, 5, 5);
    R(c, cx + 22, y - 6, 1, 6, PAL.out); P(c, cx + 22, y - 7, PAL.repair);
    P(c, cx - 8, y - 3, '#F2C9A0'); R(c, cx - 8, y - 2, 1, 2, '#D9482E');
    P(c, cx + 10, y - 3, '#F2C9A0'); R(c, cx + 10, y - 2, 1, 2, '#2E5EA8');
    // body
    R(c, cx - 35, y + 2, 70, 48, PAL.out);
    R(c, cx - 34, y + 3, 68, 3, '#F4F6F8');
    R(c, cx - 34, y + 6, 68, 8, PAL.glass);
    R(c, cx - 34, y + 6, 68, 2, PAL.glassLt);
    for (let mx = cx - 30; mx < cx + 32; mx += 6) R(c, mx, y + 6, 1, 8, '#242E38');
    R(c, cx - 34, y + 14, 68, 2, PAL.repair);
    R(c, cx - 34, y + 16, 68, 22, PAL.bldg);
    R(c, cx - 7, y + 19, 14, 9, PAL.out); R(c, cx - 6, y + 20, 12, 7, '#F4F6F8');
    R(c, cx - 4, y + 21, 8, 5, PAL.tail); R(c, cx - 4, y + 23, 8, 1, '#F4F6F8'); R(c, cx, y + 21, 1, 5, '#F4F6F8');
    R(c, cx - 34, y + 38, 68, 11, PAL.bldgSh);
    R(c, cx - 16, y + 39, 32, 9, PAL.out); R(c, cx - 15, y + 40, 30, 8, PAL.glassLt); R(c, cx - 1, y + 40, 2, 8, '#F4F6F8');
  }
  function jetbridgeSmall(c, x, y) {
    R(c, x, y, 3, 7, PAL.out); R(c, x + 1, y + 1, 1, 5, PAL.hang);
  }
  function hub(c, cx, y) {
    const H2 = 46;
    for (let j = 0; j < H2; j++) {
      const half = Math.min(25, 11 + Math.min(j, H2 - 1 - j) * 3);
      R(c, cx - half - 1, y + j, half * 2 + 2, 1, PAL.out);
    }
    for (let j = 1; j < H2 - 1; j++) {
      const half = Math.min(24, 10 + Math.min(j, H2 - 1 - j) * 3);
      let col;
      if (j < 18) col = PAL.roof;
      else if (j < 20) col = '#F4F6F8';
      else if (j < 27) col = PAL.winDk;
      else if (j < 38) col = PAL.bldg;
      else col = PAL.bldgSh;
      R(c, cx - half, y + j, half * 2, 1, col);
    }
    for (let j = 2; j < 16; j += 3) { const half = Math.min(24, 10 + j * 3) - 3; R(c, cx - 1, y + j, 2, 2, PAL.roofLt); R(c, cx - half, y + j + 1, 3, 1, PAL.roofLt); R(c, cx + half - 3, y + j + 1, 3, 1, PAL.roofLt); }
    for (let mx = -20; mx <= 20; mx += 5) R(c, cx + mx, y + 20, 1, 7, PAL.bldgSh);
    R(c, cx - 24, y + 20, 48, 2, PAL.win);
    R(c, cx - 7, y + 5, 14, 9, PAL.out);
    R(c, cx - 6, y + 6, 12, 7, '#F4F6F8');
    R(c, cx - 4, y + 7, 8, 5, PAL.tail);
    R(c, cx - 4, y + 9, 8, 1, '#F4F6F8'); R(c, cx, y + 7, 1, 5, '#F4F6F8');
    R(c, cx - 12, y + H2 - 1, 24, 4, PAL.out);
    R(c, cx - 11, y + H2, 22, 2, PAL.roofLt);
    R(c, cx - 6, y + H2 - 4, 4, 4, PAL.glassLt); R(c, cx + 2, y + H2 - 4, 4, 4, PAL.glassLt);
  }
  // landmark tower: beacon → cab → tapered shaft → base annex. y = beacon top.
  function tower3(c, x, y, status, quiet) {
    const beam = status === 'ok' ? '#6FE08A' : status === 'alert' ? '#FF6A5A' : '#F2B33D';
    const halo = status === 'ok' ? 'rgba(110,224,138,0.28)' : status === 'alert' ? 'rgba(255,106,90,0.3)' : 'rgba(242,179,61,0.30)';
    R(c, x, y + 6, 1, 6, PAL.out);
    if (!quiet) {
      c.fillStyle = halo; c.fillRect(x - 6, y - 6, 13, 13);
      c.fillStyle = 'rgba(255,255,255,0.4)'; c.fillRect(x - 1, y - 1, 3, 3);
      R(c, x - 1, y, 3, 2, beam); P(c, x, y - 1, beam);
      P(c, x - 9, y, halo); P(c, x + 9, y, halo); P(c, x, y - 8, halo);
    } else {
      R(c, x - 1, y, 3, 2, '#4A4038');
    }
    // cab
    R(c, x - 24, y + 12, 48, 4, PAL.out);
    R(c, x - 23, y + 13, 46, 2, '#F4F6F8');
    R(c, x - 22, y + 16, 44, 13, PAL.out);
    R(c, x - 21, y + 16, 42, 12, PAL.glass);
    R(c, x - 21, y + 16, 42, 2, PAL.glassLt);
    for (let mx = -15; mx <= 15; mx += 6) R(c, x + mx, y + 16, 1, 12, '#242E38');
    R(c, x - 22, y + 28, 44, 2, PAL.twrDk);
    R(c, x - 24, y + 30, 48, 2, PAL.out); R(c, x - 23, y + 30, 46, 1, PAL.twr);
    // dish on cab roof
    R(c, x + 15, y + 9, 5, 3, '#C9D2DA'); P(c, x + 16, y + 8, '#C9D2DA');
    // tapered shaft
    for (let j = 0; j < 42; j++) {
      const half = 10 - Math.floor(j / 14);
      R(c, x - half - 1, y + 32 + j, half * 2 + 2, 1, PAL.out);
      R(c, x - half, y + 32 + j, half * 2, 1, PAL.twr);
      R(c, x - half, y + 32 + j, 2, 1, '#F2EEDF');
      R(c, x + half - 2, y + 32 + j, 2, 1, PAL.twrDk);
    }
    for (let j = 0; j < 2; j++) for (let i = 0; i < 9; i++) R(c, x - 9 + i * 2, y + 34 + j * 2, 2, 2, (i + j) % 2 ? '#E8862E' : '#F4F1E6');
    for (let j = 0; j < 4; j++) R(c, x - 2, y + 44 + j * 7, 4, 3, PAL.glassLt);
    // base annex
    R(c, x - 17, y + 74, 34, 14, PAL.out);
    R(c, x - 16, y + 75, 32, 12, PAL.twr);
    R(c, x - 16, y + 75, 32, 3, '#F2EEDF');
    R(c, x - 5, y + 80, 10, 6, PAL.glassLt);
    R(c, x - 16, y + 85, 32, 2, PAL.twrDk);
  }
  // hangar: front-facing arch, open door, plane nose inside
  function hangar3(c, x, y, w, h) {
    // arched silhouette (outline first)
    const arch = [[12, w - 24], [6, w - 12], [3, w - 6], [1, w - 2], [0, w]];
    for (let j = 0; j < arch.length; j++) R(c, x + arch[j][0] - 1, y + j - 1, arch[j][1] + 2, 3, PAL.out);
    R(c, x - 1, y + 4, w + 2, h - 3, PAL.out);
    // roof bands
    R(c, x + 12, y, w - 24, 2, '#C6CDD4');
    R(c, x + 6, y + 2, w - 12, 2, '#B4BCC4');
    R(c, x + 3, y + 4, w - 6, 2, PAL.hangRoof);
    R(c, x + 1, y + 6, w - 2, 2, PAL.hangRoofDk);
    // face
    R(c, x, y + 8, w, h - 9, PAL.hang);
    R(c, x, y + h - 3, w, 2, PAL.hangDk);
    // checker header over the door
    for (let i = 0; i < Math.floor((w - 8) / 5); i++) R(c, x + 4 + i * 5, y + 10, 5, 3, i % 2 ? '#F4F1E6' : PAL.repair);
    // door opening (dark) — left 2/3 open, right 1/3 door panel
    const ox = x + 5, ow = w - 10, oy = y + 14, oh = h - 18;
    R(c, ox - 1, oy - 1, ow + 2, oh + 2, PAL.out);
    R(c, ox, oy, ow, oh, '#333B44');
    R(c, ox, oy, ow, 2, '#22282F');
    // plane nose peeking inside (front view)
    const pcx = ox + Math.floor(ow * 0.38);
    blob(c, pcx - 6, oy + 5, [[2, 8], [1, 10], [0, 12], [0, 12], [1, 10]], '#F0F3F6');
    R(c, pcx - 4, oy + 6, 8, 2, '#8FD2E4'); // windshield
    R(c, pcx - 6, oy + 10, 12, 1, '#C9D2DA'); // wing line
    R(c, pcx - 9, oy + 9, 3, 3, '#4A525A'); R(c, pcx + 6, oy + 9, 3, 3, '#4A525A'); // engines
    // sliding door panel (right third)
    const dx = ox + Math.floor(ow * 0.62);
    R(c, dx, oy - 1, ox + ow - dx, oh + 2, PAL.hangDk);
    for (let xx = dx + 2; xx < ox + ow - 1; xx += 4) R(c, xx, oy + 1, 1, oh - 2, PAL.hang);
    R(c, dx, oy - 1, 1, oh + 2, PAL.out);
  }
  // incident sign on posts (text overlaid as HTML)
  function incidentSign(c, x, y, w, h) {
    R(c, x + 5, y + h, 2, 6, PAL.out); R(c, x + w - 7, y + h, 2, 6, PAL.out);
    R(c, x + 5, y + h, 1, 6, '#4A5661'); R(c, x + w - 7, y + h, 1, 6, '#4A5661');
    R(c, x - 1, y - 1, w + 2, h + 2, PAL.out);
    R(c, x, y, w, h, PAL.sign);
    R(c, x, y, w, 2, '#FFFFFF');
    R(c, x, y + h - 2, w, 2, '#E0DCCC');
    R(c, x + 2, y + h + 4, w - 4, 2, 'rgba(30,70,40,0.22)');
  }
  // grass tie-down pad for parked aircraft
  function tiedown(c, x, y, w, h) {
    blob(c, x, y, [[4, w - 8], [1, w - 2], [0, w], [0, w]], PAL.dirt);
    R(c, x, y + 4, w, h - 8, PAL.dirt);
    blob(c, x, y + h - 4, [[0, w], [0, w], [1, w - 2], [4, w - 8]], PAL.dirt);
    R(c, x + 2, y + 3, w - 4, h - 6, PAL.sand);
    for (let i = 0; i < 12; i++) P(c, x + 3 + Math.floor(seeded(i * 7 + x) * (w - 6)), y + 4 + Math.floor(seeded(i * 3 + x) * (h - 8)), PAL.dirtDk);
    // tie posts
    P(c, x + 2, y + 2, PAL.out); P(c, x + w - 3, y + 2, PAL.out); P(c, x + 2, y + h - 3, PAL.out); P(c, x + w - 3, y + h - 3, PAL.out);
  }
  function fuelTruck(c, x, y) {
    R(c, x, y, 14, 6, PAL.out);
    R(c, x + 1, y + 1, 4, 4, '#F2C94C');
    R(c, x + 5, y + 1, 8, 4, '#E8B93E');
    R(c, x + 6, y + 2, 6, 2, '#F2D46A');
    P(c, x + 2, y + 6, PAL.out); P(c, x + 11, y + 6, PAL.out);
  }
  function baggageTrain(c, x, y) {
    R(c, x, y, 5, 5, PAL.out); R(c, x + 1, y + 1, 3, 3, '#4E8FE0');
    R(c, x + 6, y + 1, 5, 4, PAL.out); R(c, x + 7, y + 2, 3, 2, '#C2C8CD');
    R(c, x + 12, y + 1, 5, 4, PAL.out); R(c, x + 13, y + 2, 3, 2, '#C2C8CD');
  }
  function containers(c, x, y) {
    R(c, x, y, 6, 5, PAL.out); R(c, x + 1, y + 1, 4, 3, '#C8A34E');
    R(c, x + 7, y, 6, 5, PAL.out); R(c, x + 8, y + 1, 4, 3, '#8FA6C0');
    R(c, x + 3, y - 4, 6, 5, PAL.out); R(c, x + 4, y - 3, 4, 3, '#D9C28D');
  }
  function gateMark(c, x, y) {
    R(c, x, y, 1, 8, PAL.markYl); R(c, x - 2, y, 5, 1, PAL.markYl);
  }

  // ---------- lighting ----------
  function nightMultiply(c, W, H, time) {
    if (time === 'day') return;
    c.globalCompositeOperation = 'multiply';
    R(c, 0, 0, W, H, time === 'dusk' ? '#AFA0C6' : '#5F679E');
    if (time === 'night') R(c, 0, 0, W, H, '#9BA0C8');
    c.globalCompositeOperation = 'source-over';
  }
  function glow(c, x, y, col, halo) {
    c.fillStyle = halo; c.fillRect(x - 1, y - 1, 3, 3);
    c.fillStyle = col; c.fillRect(x, y, 1, 1);
  }
  function runwayLights(c, x, y, w, h) {
    for (let cx = x + 2; cx <= x + w - 2; cx += 14) {
      glow(c, cx, y - 2, '#FFD97A', 'rgba(255,190,90,0.30)');
      glow(c, cx, y + h + 1, '#FFD97A', 'rgba(255,190,90,0.30)');
    }
    for (let k = 0; k < 3; k++) {
      glow(c, x + 1, y + 2 + k * 4, '#6FE08A', 'rgba(90,220,130,0.30)');
      glow(c, x + w - 2, y + 2 + k * 4, '#FF7A6A', 'rgba(255,110,90,0.28)');
    }
  }
  function navLights(c, x, y, dir, big) {
    const map = big ? JET : JET_S;
    const sw = (dir === 'E' || dir === 'W') ? map.length : map[0].length;
    const sh = (dir === 'E' || dir === 'W') ? map[0].length : map.length;
    if (dir === 'E' || dir === 'W') {
      glow(c, x + Math.floor(sw / 2), y - 1, '#FF6A5A', 'rgba(255,90,80,0.30)');
      glow(c, x + Math.floor(sw / 2), y + sh, '#6FE08A', 'rgba(90,220,130,0.30)');
    } else {
      glow(c, x - 1, y + Math.floor(sh / 2), '#FF6A5A', 'rgba(255,90,80,0.30)');
      glow(c, x + sw, y + Math.floor(sh / 2), '#6FE08A', 'rgba(90,220,130,0.30)');
    }
  }

  // ---------- OVERVIEW v3 (400x270 logical, 800x540 css) ----------
  function drawOverview(canvas, opts) {
    opts = opts || {};
    const time = opts.time || 'day';
    const status = opts.status || 'attention';
    const W = 400, H = 270;
    const c = ctx2d(canvas, W, H, 2);
    const tint = time === 'day' ? null : time;
    const lineCol = time === 'day' ? PAL.line : '#D8E8D2';

    grass(c, W, H, 9);

    tree(c, 2, 2, 2); tree(c, 384, 2, 2); tree(c, 372, 12, 1);

    // flight line spans runway extent exactly (12 → 388), same margin both sides
    flightLine(c, 12, 388, 30, lineCol);
    arcDash(c, 64, 82, 52, 52, Math.PI, Math.PI * 1.5, lineCol);
    arcDash(c, 336, 82, 52, 52, Math.PI * 1.5, Math.PI * 2, lineCol);

    // landmarks — evenly spaced across the whole leg
    flagPoint(c, 56, 56);
    pond(c, 122, 48);
    ridge(c, 215, 48);
    shoals(c, 314, 56);
    windsock(c, 376, 64);

    // runways + taxiways
    taxiline(c, 40, 96, 8, 66, true);
    taxiline(c, 300, 96, 8, 66, true);
    taxiline(c, 196, 130, 8, 32, true);
    runway(c, 12, 82, 376, 14);
    runway(c, 12, 118, 376, 14);
    // hold-short bars where connectors meet the runways
    [[41, 98], [41, 114], [301, 98], [301, 114], [197, 134]].forEach(b => { R(c, b[0], b[1], 6, 1, PAL.markYl); R(c, b[0], b[1] + 2, 6, 1, PAL.markYl); });
    taxiline(c, 12, 152, 322, 9, false);

    // apron — packed earth, not concrete
    R(c, 12, 168, 318, 36, PAL.dirt);
    R(c, 14, 169, 314, 33, PAL.sand);
    R(c, 12, 168, 318, 1, PAL.dirtDk);
    R(c, 12, 201, 318, 3, PAL.dirt);
    for (let i = 0; i < 60; i++) P(c, 14 + Math.floor(seeded(i * 7) * 314), 170 + Math.floor(seeded(i * 3 + 5) * 30), PAL.dirtDk);
    // service lane along the apron top
    R(c, 12, 169, 318, 4, '#E0D096');
    for (let xx = 16; xx < 326; xx += 10) R(c, xx, 170, 4, 1, '#F5F0DE');
    const bays = [54, 86, 118, 234, 266, 298];
    [38, 70, 102, 134, 218, 250, 282, 314].forEach(dx => R(c, dx, 169, 1, 24, PAL.dirtDk));
    bays.forEach(bx => {
      gateMark(c, bx, 170);
      for (let yy = 176, k = 0; yy < 196; yy += 4, k++) R(c, bx, yy, 1, 2, PAL.mark);
    });
    // floodlight masts
    [[17, 172], [323, 172]].forEach(m => {
      R(c, m[0], m[1], 1, 13, PAL.out); R(c, m[0] - 2, m[1], 5, 2, PAL.out);
      P(c, m[0] - 2, m[1] + 2, '#FFD97A'); P(c, m[0] + 2, m[1] + 2, '#FFD97A');
      R(c, m[0] - 1, m[1] + 13, 3, 1, PAL.dirtDk);
    });

    // terminal: gate-pier wings + jet-age pavilion (5b folded in)
    wing5(c, 12, 206, 134);
    wing5(c, 196, 206, 134);
    bays.forEach(bx => { gateCanopy(c, bx - 5, 202); jetbridgeSmall(c, bx - 1, 196); });
    pavilion5(c, 171, 192);

    // plaza & greenery
    R(c, 158, 243, 26, 24, PAL.path);
    R(c, 158, 243, 1, 24, '#B4B8BC'); R(c, 183, 243, 1, 24, '#B4B8BC');
    for (let yy = 246; yy < 265; yy += 5) R(c, 170, yy, 2, 2, '#B4B8BC');
    flowerBed(c, 118, 246, 34); flowerBed(c, 190, 246, 34);
    tree(c, 26, 244, 2); tree(c, 66, 250, 1); tree(c, 226, 250, 1); tree(c, 262, 242, 2);
    tree(c, 130, 256, 1); tree(c, 206, 256, 1); tree(c, 302, 248, 2);
    // perimeter fence (breaks at the plaza path)
    [[12, 156], [186, 200]].forEach(seg => {
      R(c, seg[0], 264, seg[1] - seg[0] + 2, 1, '#B8B4A0');
      for (let fx = seg[0]; fx <= seg[1]; fx += 8) R(c, fx, 263, 1, 4, '#8A8168');
    });
    // wayfinding pylons + people
    [[112, 246], [226, 246]].forEach(f => {
      R(c, f[0] - 1, f[1] - 1, 7, 20, PAL.out);
      R(c, f[0], f[1], 5, 18, PAL.tail);
      R(c, f[0] + 1, f[1] + 2, 3, 2, '#F4F6F8'); R(c, f[0] + 1, f[1] + 6, 3, 2, '#F4F6F8'); R(c, f[0] + 1, f[1] + 10, 3, 2, '#F4F6F8');
    });
    [[164, 256, '#D9482E'], [175, 250, '#2E5EA8'], [169, 262, '#E8B93E'], [70, 186, '#2E5EA8'], [260, 190, '#D9482E']].forEach(p => {
      P(c, p[0], p[1], '#F2C9A0'); R(c, p[0], p[1] + 1, 1, 2, p[2]);
    });

    // apron clutter
    fuelTruck(c, 40, 178);
    baggageTrain(c, 74, 190);
    containers(c, 108, 186);
    R(c, 204, 188, 7, 4, PAL.out); R(c, 205, 189, 5, 2, '#E8862E');

    // parked (stalled) pair at the east gates — on earth, not concrete
    planeSmall(c, 222, 166, 'S', { dim: true, chocks: true });
    planeSmall(c, 254, 166, 'S', { dim: true, chocks: true });

    // landmark tower + incident sign (hangar cut by owner taste)
    tower3(c, 360, 150, status, opts.liveBeacon);
    incidentSign(c, 334, 248, 56, 14);

    nightMultiply(c, W, H, time);

    if (time !== 'day') {
      runwayLights(c, 12, 82, 376, 14);
      runwayLights(c, 12, 118, 376, 14);
      c.fillStyle = 'rgba(255,210,110,0.28)'; c.fillRect(14, 212, 314, 10);
      navLights(c, 130, 139, 'W', true);
    }

    // SL-23 taxiing in (kept above lighting pass for readability)
    plane(c, 130, 139, 'W', {});

    // SL-9 flying the leg + towed banner (drawn above lighting; skipped for the live/animated variant)
    if (!opts.noFlight) {
      plane(c, 148, 14, 'E', { air: true, tint: tint });
      contrail(c, 142, 22, 'E', 'crisp');
      banner(c, 148, 30, 64, 26, 74, 14);
      if (time !== 'day') navLights(c, 148, 14, 'E', true);
    }
  }

  // ---------- FLEET LINEUP (310x104 logical, 620x208 css) ----------
  function drawFleet(canvas) {
    const W = 310, H = 104;
    const c = ctx2d(canvas, W, H, 2);
    R(c, 0, 0, W, H, PAL.apron);
    for (let y = 0; y < H; y += 16) R(c, 0, y, W, 8, '#A4AAB0');
    R(c, 0, 0, W, 2, PAL.apronDk); R(c, 0, H - 2, W, 2, PAL.apronDk);
    for (let i = 1; i < 4; i++) R(c, i * 77, 8, 1, H - 16, PAL.apronLn);
    plane(c, 22, 30, 'N', {});
    plane(c, 100, 30, 'N', { tail: PAL.repair });
    planeAwacs(c, 178, 30, 'N', {});
    planeSmall(c, 260, 36, 'N', { dim: true, chocks: true });
  }

  // ---------- TERMINAL BUILDING OPTIONS (340x100 logical, 680x200 css) ----------
  function drawTerminalOption(canvas, kind) {
    const W = 340, H = 100;
    const c = ctx2d(canvas, W, H, 2);
    grass(c, W, H, kind.length * 7);
    // earth apron strip + jetbridges
    R(c, 0, 0, W, 16, PAL.dirt); R(c, 2, 0, W - 4, 14, PAL.sand);
    for (let i = 0; i < 24; i++) P(c, 4 + Math.floor(seeded(i * 5) * (W - 8)), 2 + Math.floor(seeded(i * 9 + 2) * 11), PAL.dirtDk);
    [60, 165, 270].forEach(jx => { R(c, jx, 8, 3, 9, PAL.out); R(c, jx + 1, 9, 1, 7, PAL.hang); });
    // fence + trees below
    R(c, 6, 92, 328, 1, '#B8B4A0');
    for (let fx = 8; fx < 334; fx += 8) R(c, fx, 91, 1, 4, '#8A8168');
    tree(c, 10, 78, 1); tree(c, 318, 76, 2);

    if (kind === 'wave') {
      const bx = 10, bw = 320, by = 20;
      for (let i = 0; i < bw; i++) {
        const t = i / bw;
        const rt = by + Math.round(15 * (1 - Math.sin(Math.PI * t)));
        R(c, bx + i, rt - 1, 1, 1, PAL.out);
        R(c, bx + i, rt, 1, 2, PAL.roofLt);
        R(c, bx + i, rt + 2, 1, 5, i % 26 < 1 ? PAL.roofDk : PAL.roof);
        R(c, bx + i, rt + 7, 1, 70 - (rt + 7), (i % 9 < 1) ? '#F4F6F8' : PAL.winDk);
        if ((bx + i) % 2 === 0) P(c, bx + i, 52, 'rgba(244,246,248,0.7)'); // floor line
        R(c, bx + i, rt + 7, 1, 2, PAL.win);
      }
      R(c, bx - 1, 70, bw + 2, 8, PAL.out);
      R(c, bx, 71, bw, 6, '#EAE4D4');
      [70, 170, 270].forEach(dx => R(c, bx + dx - 16, 72, 12, 5, PAL.glassLt));
      // suspension masts + cables
      [58, 160, 262].forEach(mx => {
        const t = mx / bw, rt = by + Math.round(15 * (1 - Math.sin(Math.PI * t)));
        R(c, bx + mx, 6, 2, rt - 6, '#F4F6F8'); R(c, bx + mx - 1, 6, 1, rt - 6, PAL.out); R(c, bx + mx + 2, 6, 1, rt - 6, PAL.out);
        R(c, bx + mx - 1, 5, 4, 1, PAL.out);
        for (let k = 1; k < 5; k++) { P(c, bx + mx - k * 7, 8 + k * 3, '#DDE2E6'); P(c, bx + mx + 1 + k * 7, 8 + k * 3, '#DDE2E6'); }
      });
      // crest at center apex
      R(c, 163, 14, 14, 9, PAL.out); R(c, 164, 15, 12, 7, '#F4F6F8'); R(c, 166, 16, 8, 5, PAL.tail); R(c, 166, 18, 8, 1, '#F4F6F8'); R(c, 169, 16, 1, 5, '#F4F6F8');
    }

    if (kind === 'jetage') {
      // gull wings (rooflines rise toward the outer tips)
      function gull(x0, x1, dirOut) {
        const wsp = x1 - x0;
        for (let i = 0; i < wsp; i++) {
          const t = dirOut ? i / wsp : 1 - i / wsp; // t=1 at outer edge
          const rt = 36 - Math.round(14 * t * t);
          R(c, x0 + i, rt - 1, 1, 1, PAL.out);
          R(c, x0 + i, rt, 1, 2, '#F4F6F8');
          P(c, x0 + i, rt + 2, PAL.repair);
          R(c, x0 + i, rt + 3, 1, 46 - (rt + 3), PAL.bldg);
          R(c, x0 + i, 46, 1, 12, (i % 10 < 1) ? PAL.bldgSh : PAL.winDk);
          R(c, x0 + i, 46, 1, 2, PAL.win);
          R(c, x0 + i, 58, 1, 14, PAL.bldg);
          R(c, x0 + i, 70, 1, 2, PAL.bldgSh);
        }
        // porthole windows
        for (let px0 = x0 + 10; px0 < x1 - 8; px0 += 16) { R(c, px0, 61, 4, 4, PAL.out); R(c, px0 + 1, 62, 2, 2, PAL.win); }
        R(c, x0 - 1, 72, wsp + 2, 1, PAL.out);
      }
      gull(10, 136, false); gull(204, 330, true);
      // central drum pavilion
      for (let j = 0; j < 46; j++) {
        const half = Math.min(36, 20 + Math.min(j, 45 - j) * 4);
        R(c, 170 - half - 1, 26 + j, half * 2 + 2, 1, PAL.out);
      }
      for (let j = 1; j < 45; j++) {
        const half = Math.min(35, 19 + Math.min(j, 45 - j) * 4);
        R(c, 170 - half, 26 + j, half * 2, 1, j < 6 ? '#F4F6F8' : j < 8 ? PAL.repair : j < 30 ? PAL.bldg : j < 42 ? PAL.winDk : PAL.bldgSh);
      }
      for (let mx = -30; mx <= 30; mx += 6) R(c, 170 + mx, 56, 1, 14, PAL.bldgSh);
      // observation drum on top
      R(c, 150, 12, 40, 16, PAL.out);
      R(c, 151, 13, 38, 5, '#F4F6F8');
      R(c, 151, 18, 38, 8, PAL.glass);
      for (let mx = 155; mx < 188; mx += 6) R(c, mx, 18, 1, 8, '#242E38');
      R(c, 151, 26, 38, 2, PAL.repair);
      R(c, 169, 4, 1, 8, PAL.out); P(c, 169, 3, PAL.repair);
      c.fillStyle = 'rgba(255,106,90,0.3)'; c.fillRect(166, 0, 7, 7);
      // crest on drum face
      R(c, 163, 34, 14, 9, PAL.out); R(c, 164, 35, 12, 7, '#F4F6F8'); R(c, 166, 36, 8, 5, PAL.tail); R(c, 166, 38, 8, 1, '#F4F6F8'); R(c, 169, 36, 1, 5, '#F4F6F8');
    }

    if (kind === 'cozy') {
      function wingC(x0, w) {
        // pitched roof with eaves
        R(c, x0 - 3, 34, w + 6, 3, PAL.out);
        R(c, x0 - 2, 30, w + 4, 5, PAL.roof);
        R(c, x0 - 2, 30, w + 4, 2, PAL.roofLt);
        R(c, x0, 26, w, 4, PAL.roof); R(c, x0, 26, w, 1, PAL.roofLt);
        R(c, x0 - 1, 25, w + 2, 1, PAL.out);
        // dormers
        for (let dx = x0 + 18; dx < x0 + w - 14; dx += 38) { R(c, dx, 26, 8, 6, PAL.out); R(c, dx + 1, 27, 6, 5, PAL.bldg); R(c, dx + 2, 28, 4, 3, PAL.win); }
        // walls
        R(c, x0 - 1, 37, w + 2, 34, PAL.out);
        R(c, x0, 37, w, 30, PAL.bldg);
        R(c, x0, 63, w, 6, PAL.bldgSh);
        // windows with striped awnings
        for (let wx = x0 + 8; wx < x0 + w - 10; wx += 22) {
          R(c, wx, 46, 8, 8, PAL.out); R(c, wx + 1, 47, 6, 6, PAL.win); R(c, wx + 1, 49, 6, 1, '#F4F6F8');
          for (let a = 0; a < 4; a++) R(c, wx - 1 + a * 3, 43, 3, 2, a % 2 ? '#F4F1E6' : PAL.repair);
          R(c, wx - 1, 45, 10, 1, 'rgba(30,50,60,0.25)');
        }
        R(c, x0 - 1, 70, w + 2, 1, PAL.out);
      }
      wingC(12, 122); wingC(206, 122);
      // central gable hall
      for (let j = 0; j < 22; j++) {
        const half = 8 + j * 2;
        R(c, 170 - half - 1, 14 + j, half * 2 + 2, 1, PAL.out);
        R(c, 170 - half, 14 + j, half * 2, 1, j < 2 ? PAL.roofLt : PAL.roof);
        P(c, 170 - half, 14 + j, PAL.roofLt); P(c, 170 + half - 1, 14 + j, PAL.roofLt);
      }
      R(c, 170 - 1, 12, 2, 3, PAL.out); P(c, 170, 11, PAL.markYl); // finial
      R(c, 139, 36, 62, 36, PAL.out);
      R(c, 140, 37, 60, 34, PAL.bldg);
      R(c, 140, 65, 60, 6, PAL.bldgSh);
      // clock in the gable
      R(c, 164, 20, 12, 12, PAL.out);
      blob(c, 165, 21, [[3, 4], [1, 8], [0, 10], [0, 10], [0, 10], [1, 8], [3, 4]], '#F7F4EA');
      R(c, 170, 24, 1, 3, PAL.out); R(c, 170, 26, 2, 1, PAL.out);
      // grand arched entrance
      blob(c, 156, 42, [[4, 20], [2, 24], [1, 26], [0, 28]], PAL.out);
      blob(c, 157, 43, [[4, 18], [2, 22], [1, 24], [0, 26]], PAL.glassLt);
      R(c, 156, 46, 28, 24, PAL.out);
      R(c, 157, 46, 26, 23, PAL.glassLt);
      R(c, 169, 46, 2, 23, '#F4F6F8');
      R(c, 157, 66, 26, 3, '#4A6472');
      // lanterns
      [150, 190].forEach(lx => { R(c, lx, 52, 1, 8, PAL.out); R(c, lx - 1, 50, 3, 3, PAL.out); P(c, lx, 51, PAL.winLit); });
    }

    // ----- shared jet-age vocabulary (straight lines only) -----
    function pinRoof(x0, w, yTop) {
      R(c, x0 - 1, yTop - 1, w + 2, 8, PAL.out);
      R(c, x0, yTop, w, 3, '#F4F6F8');
      R(c, x0, yTop + 3, w, 1, PAL.repair);
      R(c, x0, yTop + 4, w, 2, PAL.bldg);
    }
    function portholes(x0, x1, y) {
      for (let px0 = x0; px0 < x1; px0 += 16) { R(c, px0, y, 4, 4, PAL.out); R(c, px0 + 1, y + 1, 2, 2, PAL.win); }
    }
    function beaconTop(x, y) {
      c.fillStyle = 'rgba(255,106,90,0.25)'; c.fillRect(x - 2, y - 3, 5, 5);
      R(c, x, y, 1, 9, PAL.out); P(c, x, y - 1, PAL.repair);
    }
    function radarDome(x, y) {
      blob(c, x, y, [[3, 6], [1, 10], [0, 12], [0, 12]], PAL.out);
      blob(c, x + 1, y + 1, [[3, 4], [1, 8], [0, 10]], '#F4F6F8');
      P(c, x + 3, y + 1, '#FFFFFF');
    }
    function crestPx(cx, y) {
      R(c, cx - 7, y, 14, 9, PAL.out); R(c, cx - 6, y + 1, 12, 7, '#F4F6F8');
      R(c, cx - 4, y + 2, 8, 5, PAL.tail); R(c, cx - 4, y + 4, 8, 1, '#F4F6F8'); R(c, cx, y + 2, 1, 5, '#F4F6F8');
    }
    function railing(x0, w, y) {
      R(c, x0, y + 2, w, 1, '#DDE2E6');
      for (let rx = x0; rx < x0 + w; rx += 4) R(c, rx, y, 1, 3, '#B9C2C9');
    }
    function personPx(x, y, shirt) { P(c, x, y, '#F2C9A0'); R(c, x, y + 1, 1, 2, shirt); }
    function moreBridges() { [40, 95, 240, 295].forEach(jx => { R(c, jx, 9, 3, 8, PAL.out); R(c, jx + 1, 10, 1, 6, PAL.hang); }); }
    function jetPavilion(deckY, topper) {
      // central pavilion with observation deck
      R(c, 136, deckY + 4, 68, 74 - deckY, PAL.out);
      railing(140, 60, deckY);
      R(c, 137, deckY + 5, 66, 4, '#F4F6F8');
      R(c, 137, deckY + 9, 66, 10, PAL.glass);
      R(c, 137, deckY + 9, 66, 2, PAL.glassLt);
      for (let mx = 141; mx < 200; mx += 6) R(c, mx, deckY + 9, 1, 10, '#242E38');
      R(c, 137, deckY + 19, 66, 2, PAL.repair);
      R(c, 137, deckY + 21, 66, 77 - (deckY + 21) - 12, PAL.bldg);
      crestPx(170, deckY + 25);
      R(c, 137, 65, 66, 12, PAL.bldgSh);
      R(c, 153, 66, 34, 11, PAL.out); R(c, 154, 67, 32, 10, PAL.glassLt); R(c, 169, 67, 2, 10, '#F4F6F8');
      personPx(146, deckY + 1, '#D9482E'); personPx(190, deckY + 1, '#2E5EA8');
      if (topper === 'radar') { radarDome(158, deckY - 5); beaconTop(192, deckY - 4); }
      else beaconTop(170, deckY - 5);
    }

    if (kind === 'jetA') {
      moreBridges();
      [[10, 126], [204, 126]].forEach(seg => {
        const x0 = seg[0], w = seg[1];
        pinRoof(x0, w, 26);
        R(c, x0 - 1, 32, w + 2, 40, PAL.out);
        R(c, x0, 33, w, 38, PAL.bldg);
        R(c, x0 + 2, 35, w - 4, 8, PAL.winDk); R(c, x0 + 2, 35, w - 4, 2, PAL.win);
        for (let mx = x0 + 2; mx < x0 + w - 2; mx += 8) R(c, mx, 35, 1, 8, PAL.bldgSh);
        portholes(x0 + 8, x0 + w - 8, 50);
        R(c, x0, 62, w, 9, PAL.bldgSh);
        for (let mx = x0 + 10; mx < x0 + w - 10; mx += 20) R(c, mx, 64, 5, 5, PAL.win);
      });
      jetPavilion(16, 'beacon');
    }

    if (kind === 'jetB') {
      moreBridges();
      [[10, 126], [204, 126]].forEach(seg => {
        const x0 = seg[0], w = seg[1];
        pinRoof(x0, w, 28);
        R(c, x0 - 1, 34, w + 2, 38, PAL.out);
        R(c, x0, 35, w, 36, PAL.bldg);
        R(c, x0 + 2, 37, w - 4, 6, PAL.winDk); R(c, x0 + 2, 37, w - 4, 2, PAL.win);
        for (let mx = x0 + 2; mx < x0 + w - 2; mx += 8) R(c, mx, 37, 1, 6, PAL.bldgSh);
        // numbered-gate modules: door + gate light
        for (let dx = x0 + 10; dx < x0 + w - 12; dx += 21) {
          R(c, dx - 1, 49, 10, 14, PAL.out); R(c, dx, 50, 8, 13, PAL.glassLt);
          R(c, dx + 3, 50, 1, 13, '#F4F6F8');
          P(c, dx + 3, 46, '#6FE08A');
          R(c, dx - 1, 47, 10, 2, '#F4F6F8');
        }
        R(c, x0, 68, w, 3, PAL.bldgSh);
      });
      jetPavilion(16, 'radar');
      // wayfinding pylons
      [[126, 50], [212, 50]].forEach(p => {
        R(c, p[0] - 1, p[1] - 1, 7, 26, PAL.out);
        R(c, p[0], p[1], 5, 24, PAL.tail);
        R(c, p[0] + 1, p[1] + 2, 3, 2, '#F4F6F8'); R(c, p[0] + 1, p[1] + 6, 3, 2, '#F4F6F8'); R(c, p[0] + 1, p[1] + 10, 3, 2, '#F4F6F8');
      });
    }

    if (kind === 'jetC') {
      moreBridges();
      [[10, 126], [204, 126]].forEach(seg => {
        const x0 = seg[0], w = seg[1];
        pinRoof(x0, w, 24);
        R(c, x0 - 1, 30, w + 2, 42, PAL.out);
        R(c, x0, 31, w, 14, PAL.bldg);
        portholes(x0 + 8, x0 + w - 8, 35);
        R(c, x0, 51, w, 15, PAL.winDk);
        for (let mx = x0 + 4; mx < x0 + w - 4; mx += 9) R(c, mx, 51, 1, 15, '#2E3A44');
        R(c, x0, 51, w, 2, PAL.win);
        R(c, x0, 66, w, 5, PAL.bldgSh);
      });
      // the grand canopy on columns, full width
      R(c, 14, 44, 312, 1, PAL.out);
      R(c, 14, 45, 312, 3, '#F4F6F8');
      R(c, 14, 48, 312, 1, PAL.repair);
      R(c, 14, 49, 312, 1, 'rgba(30,50,60,0.3)');
      for (let cx2 = 30; cx2 < 320; cx2 += 34) { R(c, cx2 - 1, 50, 4, 21, PAL.out); R(c, cx2, 50, 2, 20, '#EAE4D4'); }
      jetPavilion(12, 'beacon');
    }
  }

  // ---------- CONTRAIL / LIVENESS TIERS (200x80 logical) ----------
  function drawTier(canvas, tier) {
    const W = 200, H = 80;
    const c = ctx2d(canvas, W, H, 2);
    grass(c, W, H, tier.length);
    tree(c, 4, 58, 1); tree(c, 184, 6, 1);
    flightLine(c, 10, 190, 40, PAL.line);
    if (tier === 'fresh') { plane(c, 100, 24, 'E', { air: true }); contrail(c, 94, 40, 'E', 'crisp'); }
    if (tier === 'idle') { plane(c, 100, 24, 'E', { air: true }); contrail(c, 94, 40, 'E', 'sputter'); }
    if (tier === 'frozen') { plane(c, 100, 24, 'E', { air: true, dim: true }); }
  }

  // ---------- SEASONAL GROUND DRESSING (200x80 logical) ----------
  function drawSeason(canvas, season) {
    const W = 200, H = 80;
    const c = ctx2d(canvas, W, H, 2);
    if (season === 'winter') {
      R(c, 0, 0, W, H, '#DFE6EC');
      for (let y = 0; y < H; y += 14) R(c, 0, y, W, 7, '#D5DEE6');
      for (let i = 0; i < 44; i++) P(c, Math.floor(seeded(i * 3) * W), Math.floor(seeded(i * 7 + 1) * H), '#FFFFFF');
      blob(c, 18, 56, [[4, 20], [1, 26], [0, 28], [2, 24]], '#F6FAFC');
      blob(c, 148, 10, [[3, 16], [1, 20], [2, 18]], '#F6FAFC');
      tree(c, 6, 6, 1); R(c, 7, 6, 6, 2, '#F6FAFC');
      tree(c, 184, 52, 1); R(c, 185, 52, 6, 2, '#F6FAFC');
      runway(c, 10, 40, 180, 12);
      plane(c, 80, 4, 'E', { air: true }); contrail(c, 74, 20, 'E', 'crisp');
    } else {
      R(c, 0, 0, W, H, '#83A64B');
      for (let y = 0; y < H; y += 14) R(c, 0, y, W, 7, '#7A9C45');
      for (let i = 0; i < 44; i++) P(c, Math.floor(seeded(i * 3) * W), Math.floor(seeded(i * 7 + 1) * H), i % 3 ? '#D9822E' : '#C9612E');
      [[6, 6, '#D9822E', '#E8A44C'], [180, 50, '#C9612E', '#E08A3C'], [56, 58, '#E0A030', '#EDBE58']].forEach(t => {
        blob(c, t[0], t[1], [[3, 3], [1, 7], [0, 9], [0, 9], [1, 7]], t[2]);
        blob(c, t[0] + 1, t[1], [[2, 3], [1, 4]], t[3]);
        R(c, t[0] + 4, t[1] + 5, 1, 3, PAL.trunk);
      });
      runway(c, 10, 40, 180, 12);
      plane(c, 80, 4, 'E', { air: true }); contrail(c, 74, 20, 'E', 'crisp');
    }
  }

  // ---------- MULTI-REPO FIELD (600x140 logical, 1200x280 css) ----------
  function drawMultiField(canvas) {
    const W = 600, H = 140;
    const c = ctx2d(canvas, W, H, 2);
    grass(c, W, H, 4);
    tree(c, 306, 16, 2); tree(c, 330, 96, 1); tree(c, 348, 54, 1); tree(c, 6, 6, 1); tree(c, 584, 120, 1);
    // main field (left, bigger — more active)
    flightLine(c, 14, 268, 22, PAL.line);
    runway(c, 14, 56, 250, 12);
    taxiline(c, 14, 76, 250, 7, false);
    R(c, 30, 88, 200, 16, PAL.dirt); R(c, 32, 89, 196, 14, PAL.sand);
    wing5(c, 30, 106, 200);
    R(c, 118, 98, 20, 12, PAL.out); R(c, 119, 99, 18, 10, '#F4F6F8'); R(c, 122, 100, 12, 8, PAL.tail); R(c, 122, 103, 12, 1, '#F4F6F8');
    // mini tower
    R(c, 243, 86, 12, 6, PAL.out); R(c, 244, 87, 10, 4, PAL.glass);
    R(c, 247, 92, 4, 22, PAL.out); R(c, 248, 92, 2, 22, PAL.twr);
    P(c, 248, 84, PAL.red);
    plane(c, 110, 6, 'E', { air: true }); contrail(c, 104, 22, 'E', 'crisp');
    // second field (right, smaller — the dashboard repo's own loop)
    const T2 = '#2E8B8B';
    flightLine(c, 372, 592, 34, PAL.line);
    runway(c, 372, 74, 208, 10);
    R(c, 396, 90, 140, 13, PAL.dirt); R(c, 398, 91, 136, 11, PAL.sand);
    R(c, 395, 103, 142, 23, PAL.out);
    R(c, 396, 104, 140, 3, '#F4F6F8'); R(c, 396, 107, 140, 1, T2);
    R(c, 396, 108, 140, 10, PAL.bldg);
    R(c, 398, 110, 136, 5, PAL.winDk);
    R(c, 396, 118, 140, 7, PAL.bldgSh);
    R(c, 458, 96, 18, 11, PAL.out); R(c, 459, 97, 16, 9, '#F4F6F8'); R(c, 462, 98, 10, 7, T2); R(c, 462, 101, 10, 1, '#F4F6F8');
    planeSmall(c, 470, 16, 'E', { air: true, tail: T2 }); contrail(c, 466, 28, 'E', 'thin');
    // camera viewport around the main field
    const vx0 = 6, vy0 = 6, vx1 = 296, vy1 = 134;
    c.fillStyle = 'rgba(255,255,255,0.85)';
    for (let x = vx0; x < vx1; x += 8) { c.fillRect(x, vy0, 4, 1); c.fillRect(x, vy1, 4, 1); }
    for (let y = vy0; y < vy1; y += 8) { c.fillRect(vx0, y, 1, 4); c.fillRect(vx1, y, 1, 4); }
    c.fillRect(vx0, vy0, 6, 2); c.fillRect(vx0, vy0, 2, 6);
    c.fillRect(vx1 - 5, vy0, 6, 2); c.fillRect(vx1, vy0, 2, 6);
    c.fillRect(vx0, vy1 - 1, 6, 2); c.fillRect(vx0, vy1 - 5, 2, 6);
    c.fillRect(vx1 - 5, vy1 - 1, 6, 2); c.fillRect(vx1, vy1 - 5, 2, 6);
  }

  // ---------- MULTI-REPO AIRPORT (600x340 logical, 1200x680 device) ----------
  // one overall airport: terminals on a skybridge ring around a central plaza
  function drawMultiWorld(canvas) {
    const W = 600, H = 340;
    const c = ctx2d(canvas, W, H, 2);
    grass(c, W, H, 4);
    [[8, 8, 2], [560, 300, 2], [30, 296, 1], [566, 10, 1], [76, 170, 1], [520, 186, 1], [110, 34, 1], [470, 300, 1]].forEach(t => tree(c, t[0], t[1], t[2]));
    const CX = 300, CY = 160;
    // outer runways serving the terminals
    runway(c, 60, 314, 480, 12);
    runway(c, 380, 22, 200, 10);
    taxiline(c, 448, 34, 6, 52, true);
    // skybridge ring — elevated glass corridor linking every terminal
    for (let a = 0; a < Math.PI * 2; a += 0.02) {
      R(c, Math.round(CX + Math.cos(a) * 135) - 2, Math.round(CY + Math.sin(a) * 95) - 2, 5, 5, PAL.out);
    }
    for (let a = 0.18; a < Math.PI * 2; a += 0.36) {
      R(c, Math.round(CX + Math.cos(a) * 135), Math.round(CY + Math.sin(a) * 95) + 3, 1, 6, PAL.out);
    }
    for (let a = 0; a < Math.PI * 2; a += 0.02) {
      R(c, Math.round(CX + Math.cos(a) * 135) - 1, Math.round(CY + Math.sin(a) * 95) - 1, 3, 3, PAL.hang);
    }
    for (let a = 0; a < Math.PI * 2; a += 0.11) {
      P(c, Math.round(CX + Math.cos(a) * 135), Math.round(CY + Math.sin(a) * 95), PAL.winDk);
    }
    // central plaza with the airline crest
    for (let dy = -42; dy <= 42; dy++) {
      const half = Math.round(Math.sqrt(Math.max(0, 42 * 42 - dy * dy)));
      R(c, CX - half, CY + dy, half * 2, 1, PAL.apron);
    }
    flowerBed(c, CX - 30, CY - 34, 60); flowerBed(c, CX - 30, CY + 31, 60);
    tree(c, CX - 38, CY - 8, 1); tree(c, CX + 30, CY - 8, 1);
    R(c, CX - 13, CY - 9, 26, 18, PAL.out);
    R(c, CX - 12, CY - 8, 24, 16, '#F4F6F8');
    R(c, CX - 9, CY - 6, 18, 12, PAL.tail);
    R(c, CX - 9, CY - 1, 18, 2, '#F4F6F8'); R(c, CX - 1, CY - 6, 2, 12, '#F4F6F8');
    // SUPERLOOPER terminal (bottom, biggest — most active) + bridge stub into the ring
    R(c, 297, 250, 6, 20, PAL.out); R(c, 298, 251, 4, 18, PAL.hang); P(c, 299, 255, PAL.winDk); P(c, 299, 261, PAL.winDk);
    wing5(c, 190, 268, 220);
    R(c, 288, 258, 24, 12, PAL.out); R(c, 289, 259, 22, 10, '#F4F6F8'); R(c, 292, 260, 16, 8, PAL.tail); R(c, 292, 263, 16, 1, '#F4F6F8');
    taxiline(c, 200, 300, 200, 7, false);
    R(c, 434, 262, 12, 6, PAL.out); R(c, 435, 263, 10, 4, PAL.glass);
    R(c, 438, 268, 4, 24, PAL.out); R(c, 439, 268, 2, 24, PAL.twr);
    P(c, 439, 260, PAL.red);
    // DASHBOARD terminal (upper right, teal — the loop maintains its own face)
    const T2 = '#2E8B8B';
    R(c, 339, 87, 122, 26, PAL.out);
    R(c, 340, 88, 120, 3, '#F4F6F8'); R(c, 340, 91, 120, 1, T2);
    R(c, 340, 92, 120, 12, PAL.bldg);
    R(c, 342, 94, 116, 6, PAL.winDk);
    R(c, 340, 104, 120, 8, PAL.bldgSh);
    R(c, 391, 80, 18, 11, PAL.out); R(c, 392, 81, 16, 9, '#F4F6F8'); R(c, 395, 82, 10, 7, T2); R(c, 395, 85, 10, 1, '#F4F6F8');
    // RESERVED pad (upper left) — room on the ring for the next adopted repo
    R(c, 158, 86, 100, 42, PAL.dirt); R(c, 160, 87, 96, 40, PAL.sand);
    c.fillStyle = 'rgba(255,255,255,0.85)';
    for (let x = 158; x < 258; x += 8) { c.fillRect(x, 86, 4, 1); c.fillRect(x, 127, 4, 1); }
    for (let y = 86; y < 128; y += 8) { c.fillRect(158, y, 1, 4); c.fillRect(257, y, 1, 4); }
    R(c, 197, 102, 2, 12, PAL.out);
    R(c, 185, 94, 26, 10, PAL.out); R(c, 186, 95, 24, 8, PAL.sign);
  }

  window.Airfield3 = {
    drawOverview: drawOverview, drawFleet: drawFleet, drawTerminalOption: drawTerminalOption,
    drawTier: drawTier, drawSeason: drawSeason, drawMultiField: drawMultiField, drawMultiWorld: drawMultiWorld,
    live: { plane: plane, planeSmall: planeSmall, navLights: navLights, banner: banner }
  };
})();
