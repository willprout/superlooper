/* Superlooper airfield v2 — bigger sprites, straight flight line, dense airport */
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
    dirt: '#D9C28D', sand: '#EBDCA6', sandDk: '#D9C68B',
    water: '#4E8CD9', waterLt: '#74AAE6', waterDk: '#3F76BF',
    rock: '#8E9296', rockDk: '#6E7276', rockLt: '#B9BDBF', snow: '#F4F6F8',
    asphalt: '#565D66', asphaltDk: '#494F57', asphaltLt: '#646B74',
    mark: '#EDEDE6', markYl: '#E8C94F',
    taxi: '#8F969C', taxiDk: '#7C838A', apron: '#A9AFB5', apronDk: '#989EA4', apronLn: '#878E94',
    bldg: '#F2E6C8', bldgSh: '#DECFA9', roof: '#3D6FA8', roofLt: '#4E82BD', roofDk: '#325C8C',
    win: '#9FD9E8', winDk: '#7FB8CE', winLit: '#FFD97A',
    hang: '#C2C8CD', hangDk: '#A9AFB5', hangRoof: '#9AA2AA', hangRoofDk: '#878F97',
    twr: '#DCD6C6', twrDk: '#C2BBA9', glass: '#A5DDEB',
    out: '#28343C',
    tail: '#2E5EA8', repair: '#D9482E', chock: '#E8862E',
    sign: '#F4F1E6', red: '#D9482E',
    tree1: '#3E8A46', tree2: '#57A85E', tree3: '#6FBE74', trunk: '#7A5230',
    line: '#F2EEDC'
  };

  // ---------- procedural jet sprite (33x42, facing N) ----------
  function buildJet() {
    const Wd = 33, Ht = 42, cx = 16;
    const g = Array.from({ length: Ht }, () => Array(Wd).fill('.'));
    const set = (x, y, ch) => { if (x >= 1 && x < Wd - 1 && y >= 1 && y < Ht - 1) g[y][x] = ch; };
    const hs = (y, a, b, ch) => { for (let x = a; x <= b; x++) set(x, y, ch); };
    // fuselage
    hs(2, cx, cx, 'W'); hs(3, cx - 1, cx + 1, 'W'); hs(4, cx - 2, cx + 2, 'W');
    for (let y = 5; y <= 31; y++) hs(y, cx - 3, cx + 3, 'W');
    hs(32, cx - 2, cx + 2, 'W'); hs(33, cx - 2, cx + 2, 'W'); hs(34, cx - 1, cx + 1, 'W'); hs(35, cx, cx, 'W');
    // cockpit windshield
    hs(5, cx - 2, cx + 2, 'C'); hs(6, cx - 1, cx + 1, 'C');
    // wings (swept, chord shrinks to tip)
    for (let d = 4; d <= 13; d++) {
      const yl = Math.round(13 + (d - 4) * 0.95);
      const yt = Math.round(21 + (d - 4) * 0.55);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'W'); set(cx + d, y, 'W'); }
      set(cx - d, yt, 'L'); set(cx + d, yt, 'L');
    }
    // winglets (tail color)
    set(cx - 13, 22, 'T'); set(cx - 13, 23, 'T'); set(cx + 13, 22, 'T'); set(cx + 13, 23, 'T');
    // engines (after wings, protruding forward)
    for (let d = 6; d <= 8; d++) for (let y = 12; y <= 18; y++) { set(cx - d, y, 'E'); set(cx + d, y, 'E'); }
    set(cx - 7, 12, 'D'); set(cx + 7, 12, 'D'); // intake lip
    set(cx - 7, 14, 'G'); set(cx + 7, 14, 'G'); // pod highlight
    // horizontal stabilizers (tail color, swept)
    for (let d = 4; d <= 10; d++) {
      const yl = Math.round(29 + (d - 4) * 0.7);
      const yt = Math.round(33 + (d - 4) * 0.3);
      for (let y = yl; y <= yt; y++) { set(cx - d, y, 'T'); set(cx + d, y, 'T'); }
    }
    // vertical fin seen from above
    for (let y = 26; y <= 35; y++) set(cx, y, 'F');
    set(cx - 1, 34, 'F'); set(cx + 1, 34, 'F');
    // fuselage shading (right side)
    for (let y = 5; y <= 31; y++) if (g[y][cx + 3] === 'W') g[y][cx + 3] = 'L';
    set(cx + 2, 4, 'L');
    // outline pass
    for (let y = 0; y < Ht; y++) for (let x = 0; x < Wd; x++) {
      if (g[y][x] !== '.') continue;
      if ((y > 0 && g[y - 1][x] !== '.' && g[y - 1][x] !== 'X') || (y < Ht - 1 && g[y + 1][x] !== '.' && g[y + 1][x] !== 'X') ||
          (x > 0 && g[y][x - 1] !== '.' && g[y][x - 1] !== 'X') || (x < Wd - 1 && g[y][x + 1] !== '.' && g[y][x + 1] !== 'X')) g[y][x] = 'X';
    }
    return g.map(r => r.join(''));
  }
  const JET = buildJet();

  function jetColors(o) {
    o = o || {};
    const T = o.tail || PAL.tail;
    if (o.dim) return { X: '#3E484F', W: '#A2ABB2', L: '#909AA1', C: '#79929A', T: '#5A6B85', F: '#4E5D74', E: '#6B747B', D: '#4A545B', G: '#7C858C' };
    if (o.tint === 'night') return { X: '#1A2229', W: '#C9CFE0', L: '#ACB3C8', C: '#7FB8CE', T: shade(T, 0.85), F: shade(T, 0.6), E: '#3E464E', D: '#262E35', G: '#59626A' };
    if (o.tint === 'dusk') return { X: '#202A32', W: '#EAE6EE', L: '#CDCAD9', C: '#8CCEE0', T: T, F: shade(T, 0.7), E: '#454E56', D: '#2A333B', G: '#646D75' };
    return { X: PAL.out, W: '#F7F9FB', L: '#D6DDE4', C: '#8FD2E4', T: T, F: shade(T, 0.7), E: '#4A525A', D: '#2E373F', G: '#6A737B' };
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
  function spriteSize(dir) {
    return (dir === 'E' || dir === 'W') ? { w: JET.length, h: JET[0].length } : { w: JET[0].length, h: JET.length };
  }
  function plane(c, x, y, dir, o) {
    o = o || {};
    const s = spriteSize(dir);
    const ox = o.air ? 6 : 2, oy = o.air ? 12 : 3;
    // cross-shaped soft shadow (fuselage bar + wing bar)
    c.fillStyle = 'rgba(20,40,28,0.18)';
    if (dir === 'E' || dir === 'W') {
      c.fillRect(x + ox + 3, y + oy + 12, s.w - 8, 9);
      c.fillRect(x + ox + Math.floor(s.w / 2) - 9, y + oy + 3, 12, s.h - 6);
    } else {
      c.fillRect(x + ox + 12, y + oy + 3, 9, s.h - 8);
      c.fillRect(x + ox + 3, y + oy + Math.floor(s.h / 2) - 9, s.w - 6, 12);
    }
    sprite(c, JET, x, y, dir, jetColors(o));
    if (o.chocks) {
      const mx = x + Math.floor(s.w / 2);
      R(c, mx - 5, y + s.h - 5, 2, 2, PAL.chock); R(c, mx + 3, y + s.h - 5, 2, 2, PAL.chock);
      R(c, mx - 3, y + 4, 2, 2, PAL.chock); R(c, mx + 1, y + 4, 2, 2, PAL.chock);
    }
  }
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
  function navLights(c, x, y, dir) {
    const s = spriteSize(dir);
    function gl(px, py, col, halo) { c.fillStyle = halo; c.fillRect(px - 1, py - 1, 3, 3); c.fillStyle = col; c.fillRect(px, py, 1, 1); }
    if (dir === 'E' || dir === 'W') {
      gl(x + Math.floor(s.w / 2), y - 1, '#FF6A5A', 'rgba(255,90,80,0.30)');
      gl(x + Math.floor(s.w / 2), y + s.h, '#6FE08A', 'rgba(90,220,130,0.30)');
    } else {
      gl(x - 1, y + Math.floor(s.h / 2), '#FF6A5A', 'rgba(255,90,80,0.30)');
      gl(x + s.w, y + Math.floor(s.h / 2), '#6FE08A', 'rgba(90,220,130,0.30)');
    }
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
  function tree(c, x, y, size) {
    if (size === 2) {
      // soft round canopy, no hard outline ring
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
  function blob(c, x, y, rows, col) {
    for (let j = 0; j < rows.length; j++) R(c, x + rows[j][0], y + j, rows[j][1], 1, col);
  }

  // ---------- flight line ----------
  function flightLine(c, x0, x1, y, col) {
    for (let x = x0; x < x1; x += 9) R(c, x, y, 5, 1, col);
    for (let x = x0 + 44; x < x1 - 20; x += 88) { // direction chevrons →
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
  function holdLoop(c, cx, cy, rx, ry, col) {
    arcDash(c, cx, cy, rx, ry, 0, Math.PI * 2, col);
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

  // ---------- buildings & props ----------
  function terminal2(c, x, y, w) {
    // y = roof top; total height 40
    R(c, x - 1, y - 1, w + 2, 42, PAL.out);
    R(c, x, y, w, 7, PAL.roof);
    R(c, x, y, w, 2, PAL.roofLt);
    for (let sx = x + 10; sx < x + w - 10; sx += 24) R(c, sx, y + 3, 12, 2, PAL.roofDk); // skylights
    R(c, x, y + 7, w, 15, PAL.bldg);
    // glass band with mullions
    R(c, x + 3, y + 10, w - 6, 8, PAL.win);
    R(c, x + 3, y + 15, w - 6, 3, PAL.winDk);
    for (let mx = x + 3; mx < x + w - 3; mx += 8) R(c, mx, y + 10, 1, 8, PAL.bldgSh);
    R(c, x, y + 22, w, 12, PAL.bldgSh);
    for (let dx = x + 14; dx < x + w - 14; dx += 34) { R(c, dx, y + 24, 8, 9, PAL.out); R(c, dx + 1, y + 25, 6, 8, PAL.winDk); } // doors
    R(c, x, y + 34, w, 6, shade('#DECFA9', 0.85));
    // crest disc on roof center
    const cx = x + Math.floor(w / 2);
    R(c, cx - 6, y + 1, 12, 5, PAL.roofLt);
    R(c, cx - 4, y + 1, 8, 5, '#F4F6F8'); R(c, cx - 3, y + 2, 6, 3, PAL.tail); R(c, cx - 3, y + 3, 6, 1, '#F4F6F8');
  }
  function jetbridge2(c, x, y) {
    R(c, x, y, 3, 9, PAL.out); R(c, x + 1, y + 1, 1, 7, PAL.hang);
    R(c, x - 2, y - 3, 7, 4, PAL.out); R(c, x - 1, y - 2, 5, 2, PAL.hangDk);
  }
  function tower2(c, x, y) {
    // taller tower, checkered base block
    R(c, x + 4, y + 14, 12, 38, PAL.out);
    R(c, x + 5, y + 14, 10, 38, PAL.twr);
    R(c, x + 5, y + 14, 3, 38, '#EFEADB');
    R(c, x + 12, y + 14, 3, 38, PAL.twrDk);
    // checker band
    for (let j = 0; j < 3; j++) for (let i = 0; i < 5; i++) R(c, x + 5 + i * 2, y + 40 + j * 2, 2, 2, (i + j) % 2 ? '#E8862E' : '#F4F1E6');
    // cab
    R(c, x - 2, y, 24, 15, PAL.out);
    R(c, x - 1, y + 1, 22, 13, PAL.twr);
    R(c, x, y + 3, 20, 7, PAL.glass);
    R(c, x, y + 8, 20, 2, '#7FB8C8');
    for (let mx = x + 4; mx < x + 20; mx += 5) R(c, mx, y + 3, 1, 7, PAL.twrDk);
    R(c, x - 1, y + 14, 22, 1, PAL.twrDk);
    R(c, x + 9, y - 4, 1, 4, PAL.out); P(c, x + 9, y - 5, PAL.red);
  }
  const DIG = {
    '0': ['XXX', 'X.X', 'X.X', 'X.X', 'XXX'], '1': ['.X.', 'XX.', '.X.', '.X.', 'XXX'],
    '2': ['XXX', '..X', 'XXX', 'X..', 'XXX'], '3': ['XXX', '..X', 'XXX', '..X', 'XXX'],
    '4': ['X.X', 'X.X', 'XXX', '..X', '..X'], '5': ['XXX', 'X..', 'XXX', '..X', 'XXX'],
    '6': ['XXX', 'X..', 'XXX', 'X.X', 'XXX'], '7': ['XXX', '..X', '.X.', '.X.', '.X.'],
    '8': ['XXX', 'X.X', 'XXX', 'X.X', 'XXX'], '9': ['XXX', 'X.X', 'XXX', '..X', 'XXX']
  };
  function digits(c, x, y, str, col, s) {
    s = s || 1;
    for (let i = 0; i < str.length; i++) {
      const m = DIG[str[i]]; if (!m) continue;
      for (let j = 0; j < 5; j++) for (let k = 0; k < 3; k++) if (m[j][k] === 'X') R(c, x + (i * 4 + k) * s, y + j * s, s, s, col);
    }
  }
  function hangar2(c, x, y, w, h, count) {
    R(c, x - 1, y - 1, w + 2, h + 2, PAL.out);
    // curved roof suggestion: two tone bands
    R(c, x, y, w, 4, '#AEB6BE');
    R(c, x, y + 4, w, 5, PAL.hangRoof);
    for (let xx = x + 3; xx < x + w - 3; xx += 5) R(c, xx, y, 1, 9, PAL.hangRoofDk);
    R(c, x, y + 9, w, h - 9, PAL.hang);
    R(c, x, y + h - 3, w, 3, PAL.hangDk);
    // big door
    R(c, x + Math.floor(w / 2) - 4, y + 13, Math.floor(w / 2) + 2, h - 17, PAL.hangDk);
    for (let xx = 0; xx < 5; xx++) R(c, x + Math.floor(w / 2) - 3 + xx * 6, y + 14, 1, h - 19, PAL.hang);
    // incident sign (big)
    R(c, x + 3, y + 11, 26, 17, PAL.out);
    R(c, x + 4, y + 12, 24, 15, PAL.sign);
    digits(c, x + 6, y + 15, String(count), PAL.red, 2);
    R(c, x + 15, y + 14, 11, 2, '#8A8676'); R(c, x + 15, y + 18, 9, 2, '#8A8676'); R(c, x + 15, y + 22, 11, 2, '#8A8676');
  }
  function fuelTanks(c, x, y) {
    function tank(tx) {
      blob(c, tx, y, [[4, 10], [2, 14], [1, 16], [0, 18], [0, 18], [0, 18], [1, 16], [2, 14], [4, 10]], PAL.out);
      blob(c, tx + 1, y + 1, [[4, 8], [2, 12], [1, 14], [0, 16], [0, 16], [1, 14], [2, 12], [4, 8]], '#E4E7EA');
      R(c, tx + 3, y + 3, 8, 2, '#F4F6F8');
      R(c, tx + 2, y + 5, 14, 2, '#E8862E');
    }
    tank(x); tank(x + 24);
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
    R(c, x, y, 1, 10, PAL.markYl); R(c, x - 2, y, 5, 1, PAL.markYl);
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
  function litWindows2(c, x, y, w) {
    c.fillStyle = 'rgba(255,210,110,0.30)'; c.fillRect(x + 3, y + 9, w - 6, 10);
    for (let wx = x + 5; wx < x + w - 5; wx += 8) if (seeded(wx * 3) < 0.8) R(c, wx, y + 11, 5, 5, PAL.winLit);
  }
  function dimExcept(c, W, H, cx, cy, r, amt) {
    c.save();
    c.beginPath();
    c.rect(0, 0, W, H);
    c.arc(cx, cy, r, 0, Math.PI * 2, true);
    c.fillStyle = 'rgba(18,24,40,' + amt + ')';
    c.fill('evenodd');
    c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2);
    c.strokeStyle = 'rgba(255,235,170,0.35)'; c.lineWidth = 1; c.stroke();
    c.restore();
  }

  // ---------- OVERVIEW v2 (400x270 logical, 800x540 css) ----------
  function drawOverview(canvas, opts) {
    opts = opts || {};
    const time = opts.time || 'day';
    const W = 400, H = 270;
    const c = ctx2d(canvas, W, H, 2);
    const tint = time === 'day' ? null : time;
    const lineCol = time === 'day' ? PAL.line : '#D8E8D2';

    grass(c, W, H, 9);

    // sparse perimeter trees (corners only)
    tree(c, 2, 2, 2); tree(c, 384, 2, 2); tree(c, 372, 12, 1);
    tree(c, 2, 250, 1); tree(c, 300, 152, 1);

    // ===== the flight line (pattern flows left → right) =====
    flightLine(c, 22, 378, 30, lineCol);
    // climb-out arc (left): runway 1 left end up to the line
    arcDash(c, 70, 82, 52, 52, Math.PI, Math.PI * 1.5, lineCol);
    // descent arc (right): line down to final
    arcDash(c, 330, 82, 52, 52, Math.PI * 1.5, Math.PI * 2, lineCol);
    // holding loop (number 2 for landing waits here)
    holdLoop(c, 300, 30, 22, 14, lineCol);

    // ===== landmarks along the leg (real phases) =====
    flagPoint(c, 46, 56);            // Reconcile Point
    pond(c, 88, 48);                  // Build Island
    ridge(c, 172, 48);                // Review Ridge
    shoals(c, 240, 56);               // CI Shoals
    windsock(c, 374, 62);

    // ===== runways (2 lanes) =====
    taxiline(c, 40, 96, 8, 66, true);
    taxiline(c, 352, 96, 8, 66, true);
    taxiline(c, 196, 130, 8, 32, true);
    runway(c, 12, 82, 376, 14);
    runway(c, 12, 118, 376, 14);
    taxiline(c, 12, 152, 376, 9, false);

    // ===== apron + terminal =====
    R(c, 12, 170, 288, 42, PAL.apron);
    R(c, 12, 170, 288, 1, PAL.apronLn);
    R(c, 12, 208, 288, 4, PAL.apronDk);
    for (let gx = 40; gx <= 260; gx += 44) { R(c, gx, 171, 1, 30, PAL.apronLn); gateMark(c, gx + 22, 172); }
    terminal2(c, 12, 212, 288);
    jetbridge2(c, 34, 203); jetbridge2(c, 78, 203); jetbridge2(c, 122, 203); jetbridge2(c, 166, 203); jetbridge2(c, 210, 203); jetbridge2(c, 254, 203);

    // apron clutter
    fuelTruck(c, 60, 182);
    baggageTrain(c, 100, 194);
    containers(c, 150, 190);
    R(c, 196, 186, 7, 4, PAL.out); R(c, 197, 187, 5, 2, '#E8862E'); // pushback tug

    // ===== right complex: tower, hangar, fuel farm =====
    tower2(c, 310, 158);
    hangar2(c, 340, 168, 54, 44, opts.incident != null ? opts.incident : 0);
    fuelTanks(c, 306, 224);
    tree(c, 352, 240, 1); tree(c, 376, 236, 1);

    // ===== ground planes =====
    // parked (stalled) pair at far gates — SL-7, SL-21
    plane(c, 216, 168, 'S', { dim: true, chocks: true });
    plane(c, 258, 168, 'S', { dim: true, chocks: true });
    // SL-23 taxiing in on the parallel taxiway
    plane(c, 130, 141, 'W', {});

    nightMultiply(c, W, H, time);

    if (time !== 'day') {
      runwayLights(c, 12, 82, 376, 14);
      runwayLights(c, 12, 118, 376, 14);
      litWindows2(c, 12, 212, 288);
      glow(c, 319, 153, '#FF6A5A', 'rgba(255,90,80,0.4)');
      c.fillStyle = 'rgba(160,225,240,0.30)'; c.fillRect(309, 161, 20, 8);
      navLights(c, 130, 141, 'W');
    }

    // SL-9 flying the leg (drawn above lighting so the flight pops)
    plane(c, 118, 14, 'E', { air: true, tint: tint });
    contrail(c, 112, 30, 'E', 'crisp');
    if (time !== 'day') navLights(c, 118, 14, 'E');
  }

  // ---------- STATE VIGNETTES v2 (220x140 logical, 440x280 css) ----------
  function drawState(canvas, mode) {
    const W = 220, H = 140;
    const c = ctx2d(canvas, W, H, 2);

    grass(c, W, H, mode.length * 3);
    tree(c, 2, 2, 2); tree(c, 204, 6, 1); tree(c, 190, 118, 2); tree(c, 4, 116, 1);

    if (mode === 'parked') {
      R(c, 44, 28, 176, 74, PAL.apron);
      R(c, 44, 28, 1, 74, PAL.apronLn); R(c, 44, 28, 176, 1, PAL.apronLn);
      R(c, 80, 29, 1, 40, PAL.apronLn); R(c, 144, 29, 1, 40, PAL.apronLn);
      gateMark(c, 108, 30); gateMark(c, 172, 30);
      taxiline(c, 12, 108, 208, 8, false);
      plane(c, 88, 32, 'S', { dim: true, chocks: true });
      R(c, 100, 82, 5, 2, 'rgba(60,66,72,0.5)'); P(c, 106, 84, 'rgba(60,66,72,0.4)');
      P(c, 84, 36, PAL.chock); P(c, 84, 70, PAL.chock); P(c, 124, 36, PAL.chock); P(c, 124, 70, PAL.chock);
      plane(c, 152, 32, 'S', {});
      return;
    }

    if (mode === 'night') {
      flightLine(c, 14, 206, 24, '#D8E8D2');
      runway(c, 10, 88, 200, 14);
      windsock(c, 200, 74);
      nightMultiply(c, W, H, 'night');
      runwayLights(c, 10, 88, 200, 14);
      plane(c, 52, 8, 'E', { air: true, tint: 'night' });
      navLights(c, 52, 8, 'E');
      contrail(c, 46, 24, 'E', 'crisp');
      plane(c, 148, 52, 'W', { air: true, tint: 'night' });
      c.fillStyle = 'rgba(255,245,200,0.20)';
      c.beginPath(); c.moveTo(148, 62); c.lineTo(110, 88); c.lineTo(150, 96); c.closePath(); c.fill();
      navLights(c, 148, 52, 'W');
      contrail(c, 180, 68, 'W', 'thin');
    }

    if (mode === 'spinning') {
      const cx = 104, cy = 44, rx = 30, ry = 20;
      holdLoop(c, cx, cy, rx, ry, '#D8E8D2');
      runway(c, 10, 96, 200, 14);
      windsock(c, 202, 82);
      nightMultiply(c, W, H, 'dusk');
      for (let k = 2; k < 17; k++) {
        const a = -0.6 - k * 0.38;
        const x = Math.round(cx + Math.cos(a) * rx), y = Math.round(cy + Math.sin(a) * ry);
        c.fillStyle = k < 6 ? 'rgba(255,255,255,0.95)' : k < 10 ? 'rgba(245,248,252,0.6)' : 'rgba(235,240,248,0.3)';
        c.fillRect(x, y, k < 6 ? 2 : 1, k < 6 ? 2 : 1);
      }
      plane(c, cx + rx - 14, cy - 24, 'N', { air: true, tint: 'dusk' });
      navLights(c, cx + rx - 14, cy - 24, 'N');
      dimExcept(c, W, H, cx + rx - 2, cy - 2, 46, 0.42);
      runwayLights(c, 10, 96, 200, 14);
    }

    if (mode === 'freeze') {
      flightLine(c, 14, 206, 24, '#D8E8D2');
      runway(c, 10, 88, 200, 14);
      taxiline(c, 40, 112, 170, 8, false);
      plane(c, 96, 104, 'E', { tail: PAL.repair });
      nightMultiply(c, W, H, 'night');
      runwayLights(c, 10, 88, 200, 14);
      plane(c, 40, 8, 'E', { air: true, tint: 'night' });
      navLights(c, 40, 8, 'E');
      contrail(c, 34, 24, 'E', 'crisp');
      for (let k = 0; k < 2; k++) {
        R(c, 176 + k * 7, 89, 4, 12, '#F2B33D');
        c.fillStyle = 'rgba(242,179,61,0.25)'; c.fillRect(175 + k * 7, 88, 6, 14);
      }
      glow(c, 104, 102, '#FFD97A', 'rgba(255,190,90,0.4)');
    }

    if (mode === 'amber') {
      const cx = 108, cy = 46, rx = 32, ry = 20;
      holdLoop(c, cx, cy, rx, ry, '#D8E8D2');
      flightLine(c, 14, 70, 46, '#D8E8D2');
      flightLine(c, 146, 206, 46, '#D8E8D2');
      runway(c, 10, 100, 200, 14);
      nightMultiply(c, W, H, 'dusk');
      plane(c, cx + rx - 14, cy - 26, 'N', { air: true, tint: 'dusk' });
      navLights(c, cx + rx - 14, cy - 26, 'N');
      contrail(c, cx + rx + 2, cy - 4, 'N', 'thin');
      const px = cx + rx + 2, py = cy - 8;
      for (let a = 0; a < 34; a++) {
        const x1 = Math.round(px + Math.cos(a / 5.4) * 22), y1 = Math.round(py + Math.sin(a / 5.4) * 22);
        P(c, x1, y1, '#F2B33D');
        if (a % 2 === 0) { const x2 = Math.round(px + Math.cos(a / 5.4) * 28), y2 = Math.round(py + Math.sin(a / 5.4) * 28); P(c, x2, y2, 'rgba(242,179,61,0.6)'); }
      }
      dimExcept(c, W, H, px, py, 44, 0.34);
      runwayLights(c, 10, 100, 200, 14);
    }
  }

  window.Airfield2 = { drawOverview: drawOverview, drawState: drawState };
})();
