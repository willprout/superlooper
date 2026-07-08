/* Superlooper airfield — 16-bit pixel renderer (canvas, no DOM text) */
(function () {
  'use strict';

  // ---------- tiny helpers ----------
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

  // ---------- palette ----------
  const PAL = {
    grass1: '#5CB350', grass2: '#54A849', tuft: '#4A9740', flower1: '#F2EFE4', flower2: '#F2D449',
    dirt: '#D9C28D', sand: '#EBDCA6', sandDk: '#D9C68B',
    water: '#4E8CD9', waterLt: '#74AAE6', waterDk: '#3F76BF',
    rock: '#8E9296', rockDk: '#6E7276', rockLt: '#B9BDBF', snow: '#F4F6F8',
    asphalt: '#565D66', asphaltDk: '#494F57', asphaltLt: '#646B74',
    mark: '#EDEDE6', markYl: '#E8C94F',
    taxi: '#8F969C', taxiDk: '#7C838A', apron: '#A7ADB3', apronLn: '#8F969C',
    bldg: '#F2E6C8', bldgSh: '#DECFA9', roof: '#3D6FA8', roofLt: '#4E82BD',
    win: '#9FD9E8', winLit: '#FFD97A',
    hang: '#BFC5CA', hangDk: '#A6ACB2', hangRoof: '#98A0A8', hangRoofDk: '#858D95',
    twr: '#D8D2C2', twrDk: '#BFB8A6', glass: '#A5DDEB',
    out: '#28343C',
    tail: '#2E5EA8',
    repair: '#D9482E',
    chock: '#E8862E',
    sign: '#F4F1E6', red: '#D9482E',
    tree1: '#3E8A46', tree2: '#57A85E', tree3: '#6FBE74', trunk: '#7A5230'
  };

  // ---------- jet sprite (17w x 23h, facing N) ----------
  const JET = [
    '........X........',
    '.......XWX.......',
    '.......XCX.......',
    '.......XWX.......',
    '......XWWLX......',
    '......XWWLX......',
    '......XWWLX......',
    '.X....XWWLX....X.',
    '.XX...XWWLX...XX.',
    '.XWX..XWWLX..XWX.',
    '.XWWX.XWWLX.XWWX.',
    'XWWWWXXWWLXXWWWLX',
    'XWWWWWWWWLWWWWWLX',
    'XXEEXWWWWLWWXEEXX',
    '..XXX.XWWLX.XXX..',
    '......XWWLX......',
    '......XWWLX......',
    '......XWWLX......',
    '..X...XWWLX...X..',
    '.XTX..XWWLX..XTX.',
    '.XTTX.XWWLX.XTTX.',
    '.XTTTXXWWLXXTTTX.',
    '..XXXX.XXX.XXXX..'
  ];
  function jetColors(o) {
    o = o || {};
    if (o.dim) return { X: '#3A444C', W: '#9AA4AC', L: '#8A949C', C: '#6E8890', T: '#4A5E80', E: '#333B42' };
    if (o.tint === 'night') return { X: '#1C242C', W: '#C6CCDE', L: '#A9B0C6', C: '#7FB8CE', T: shade(o.tail || PAL.tail, 0.85), E: '#2A3138' };
    if (o.tint === 'dusk') return { X: '#222C34', W: '#E6E2EC', L: '#C9C6D6', C: '#8CCEE0', T: o.tail || PAL.tail, E: '#333B42' };
    return { X: PAL.out, W: '#F6F8FA', L: '#D5DDE4', C: '#8FD2E4', T: o.tail || PAL.tail, E: '#39424A' };
  }
  function shade(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    const r = Math.round(((n >> 16) & 255) * f), g = Math.round(((n >> 8) & 255) * f), b = Math.round((n & 255) * f);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
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
  function spriteSize(map, dir) {
    const h = map.length, w = map[0].length;
    return (dir === 'E' || dir === 'W') ? { w: h, h: w } : { w: w, h: h };
  }
  function planeShadow(c, x, y, dir, alt) {
    const s = spriteSize(JET, dir);
    const ox = alt ? 4 : 1, oy = alt ? 9 : 2;
    c.fillStyle = 'rgba(20,40,28,0.26)';
    if (dir === 'E' || dir === 'W') c.fillRect(x + ox + 3, y + oy + 4, s.w - 6, 9);
    else c.fillRect(x + ox + 4, y + oy + 3, 9, s.h - 6);
  }
  function plane(c, x, y, dir, o) {
    o = o || {};
    planeShadow(c, x, y, dir, o.air);
    sprite(c, JET, x, y, dir, jetColors(o));
    if (o.chocks) {
      const s = spriteSize(JET, dir);
      P(c, x + Math.floor(s.w / 2) - 3, y + s.h - 3, PAL.chock);
      P(c, x + Math.floor(s.w / 2) + 2, y + s.h - 3, PAL.chock);
      P(c, x + Math.floor(s.w / 2) - 2, y + 2, PAL.chock);
      P(c, x + Math.floor(s.w / 2) + 1, y + 2, PAL.chock);
    }
  }
  function contrail(c, x, y, dir, kind) {
    if (!kind || kind === 'none') return;
    const dx = dir === 'W' ? 1 : dir === 'E' ? -1 : 0;
    const dy = dir === 'N' ? 1 : dir === 'S' ? -1 : 0;
    for (let k = 0; k < 13; k++) {
      if (kind === 'sputter' && (k === 2 || k === 3 || k === 6 || k === 7 || k === 8)) continue;
      if (kind === 'thin' && k % 2) continue;
      const d = 4 + k * (kind === 'crisp' ? 4 : 5);
      const px0 = x + dx * d, py0 = y + dy * d;
      const big = k < 5 && kind === 'crisp';
      c.fillStyle = k < 4 ? 'rgba(255,255,255,0.95)' : k < 8 ? 'rgba(245,248,252,0.65)' : 'rgba(235,240,248,0.35)';
      c.fillRect(px0 - (big ? 1 : 0), py0 - (big ? 1 : 0), big ? 2 : 1, big ? 2 : 1);
    }
  }

  // ---------- terrain ----------
  function grass(c, W, H, seed) {
    R(c, 0, 0, W, H, PAL.grass1);
    for (let y = 0; y < H; y += 14) R(c, 0, y, W, 7, PAL.grass2);
    for (let i = 0; i < W * H / 240; i++) {
      const x = Math.floor(seeded(i * 3 + seed) * W), y = Math.floor(seeded(i * 7 + seed + 1) * H);
      const r = seeded(i * 13 + seed + 2);
      if (r < 0.62) { P(c, x, y, PAL.tuft); P(c, x + 1, y, PAL.tuft); }
      else if (r < 0.84) P(c, x, y, PAL.flower1);
      else P(c, x, y, PAL.flower2);
    }
  }
  // Pokemon-style round tree. size: 1 small (9px canopy), 2 big (13px)
  function tree(c, x, y, size) {
    if (size === 2) {
      // canopy 13x11
      R(c, x + 3, y, 7, 1, PAL.out);
      R(c, x + 1, y + 1, 11, 1, PAL.out); R(c, x + 2, y + 1, 9, 1, PAL.tree2);
      R(c, x, y + 2, 13, 7, PAL.out);
      R(c, x + 1, y + 2, 11, 6, PAL.tree1);
      R(c, x + 2, y + 2, 9, 3, PAL.tree2);
      R(c, x + 3, y + 2, 4, 2, PAL.tree3);
      P(c, x + 2, y + 6, PAL.tree2); P(c, x + 9, y + 5, PAL.tree2);
      R(c, x + 2, y + 9, 9, 1, PAL.out);
      R(c, x + 5, y + 9, 3, 3, PAL.out); R(c, x + 6, y + 9, 1, 3, PAL.trunk);
      // grass shadow
      R(c, x + 2, y + 12, 9, 1, 'rgba(30,70,40,0.25)');
    } else {
      R(c, x + 2, y, 5, 1, PAL.out);
      R(c, x + 1, y + 1, 7, 1, PAL.out); R(c, x + 2, y + 1, 5, 1, PAL.tree2);
      R(c, x, y + 2, 9, 5, PAL.out);
      R(c, x + 1, y + 2, 7, 4, PAL.tree1);
      R(c, x + 2, y + 2, 5, 2, PAL.tree2);
      R(c, x + 3, y + 2, 2, 1, PAL.tree3);
      R(c, x + 2, y + 7, 5, 1, PAL.out);
      R(c, x + 4, y + 7, 1, 2, PAL.trunk); P(c, x + 4, y + 9, PAL.out);
    }
  }
  // organic blob from row spans: rows = [[xoff,width],...]
  function blob(c, x, y, rows, col) {
    for (let j = 0; j < rows.length; j++) R(c, x + rows[j][0], y + j, rows[j][1], 1, col);
  }
  const POND_SAND = [[12, 22], [7, 32], [4, 38], [2, 42], [1, 44], [0, 46], [0, 46], [0, 47], [1, 46], [1, 45], [2, 43], [4, 40], [7, 34], [12, 24]];
  const POND_WATER = [[13, 20], [9, 28], [6, 34], [4, 38], [3, 40], [2, 42], [2, 42], [3, 42], [3, 41], [4, 39], [6, 35], [9, 29], [14, 18]];
  function pond(c, x, y) {
    blob(c, x, y + 1, POND_SAND, PAL.sand);
    blob(c, x + 1, y + 1, POND_SAND, PAL.sandDk);
    blob(c, x, y + 1, POND_WATER.map(r => [r[0], r[1]]), PAL.water);
    // shoals: pale shallow water on the east lobe
    R(c, x + 34, y + 5, 7, 2, PAL.waterLt); R(c, x + 36, y + 8, 6, 2, PAL.waterLt);
    R(c, x + 33, y + 11, 8, 2, PAL.waterLt); P(c, x + 40, y + 7, PAL.sand); P(c, x + 39, y + 12, PAL.sand);
    // ripples
    R(c, x + 8, y + 6, 6, 1, PAL.waterLt); R(c, x + 12, y + 12, 5, 1, PAL.waterDk);
    // island (Build Island)
    blob(c, x + 15, y + 5, [[3, 8], [1, 12], [0, 14], [0, 14], [1, 12], [3, 8]], PAL.tree2);
    R(c, x + 16, y + 10, 12, 1, PAL.sand);
    tree(c, x + 18, y + 1, 1);
  }
  function ridge(c, x, y) {
    function peak(px0, w, h) {
      for (let r = 0; r < h; r++) {
        const half = Math.round((r + 1) * (w / 2) / h);
        R(c, px0 + Math.round(w / 2) - half - 1, y + r + (10 - h), 1, 1, PAL.out);
        R(c, px0 + Math.round(w / 2) + half, y + r + (10 - h), 1, 1, PAL.out);
        R(c, px0 + Math.round(w / 2) - half, y + r + (10 - h), half * 2, 1, r < 2 ? PAL.snow : (r % 3 === 0 ? PAL.rockLt : PAL.rock));
      }
    }
    peak(x, 16, 9); peak(x + 13, 20, 10); peak(x + 30, 14, 7);
    R(c, x + 1, y + 10, 42, 2, PAL.rockDk);
    R(c, x + 1, y + 12, 42, 1, 'rgba(30,70,40,0.25)');
  }
  function flagPoint(c, x, y) {
    R(c, x - 1, y + 4, 10, 4, PAL.dirt); R(c, x, y + 5, 8, 2, PAL.sand);
    R(c, x + 3, y - 4, 1, 9, PAL.out);
    R(c, x + 4, y - 4, 5, 2, PAL.red); R(c, x + 4, y - 2, 3, 1, PAL.red);
  }
  function windsock(c, x, y) {
    R(c, x, y, 1, 8, PAL.out);
    R(c, x + 1, y, 3, 3, PAL.chock); R(c, x + 4, y, 2, 3, '#F2A45E'); R(c, x + 6, y + 1, 1, 1, '#F2C08A');
  }

  // ---------- pavement ----------
  function runway(c, x, y, w, h) {
    R(c, x, y - 1, w, 1, PAL.asphaltDk);
    R(c, x, y, w, h, PAL.asphalt);
    R(c, x, y + h, w, 1, PAL.asphaltDk);
    for (let i = 0; i < 4; i++) {
      R(c, x + 3 + i * 3, y + 2, 1, h - 4, PAL.mark);
      R(c, x + w - 4 - i * 3, y + 2, 1, h - 4, PAL.mark);
    }
    const cy = y + Math.floor(h / 2);
    for (let cx = x + 20; cx < x + w - 20; cx += 9) R(c, cx, cy, 5, 1, PAL.mark);
    R(c, x + 22, y + 2, 2, 2, PAL.mark); R(c, x + 22, y + h - 4, 2, 2, PAL.mark);
    R(c, x + w - 24, y + 2, 2, 2, PAL.mark); R(c, x + w - 24, y + h - 4, 2, 2, PAL.mark);
    for (let i = 0; i < 14; i++) {
      P(c, x + 10 + Math.floor(seeded(i * 5 + y) * (w - 20)), y + 2 + Math.floor(seeded(i * 11 + y) * (h - 4)), PAL.asphaltLt);
    }
  }
  function taxiline(c, x, y, w, h, vert) {
    R(c, x, y, w, h, PAL.taxi);
    if (vert) { R(c, x, y, 1, h, PAL.taxiDk); R(c, x + w - 1, y, 1, h, PAL.taxiDk); for (let yy = y + 2; yy < y + h; yy += 5) R(c, x + Math.floor(w / 2), yy, 1, 3, PAL.markYl); }
    else { R(c, x, y, w, 1, PAL.taxiDk); R(c, x, y + h - 1, w, 1, PAL.taxiDk); for (let xx = x + 2; xx < x + w; xx += 5) R(c, xx, y + Math.floor(h / 2), 3, 1, PAL.markYl); }
  }
  function circuit(c, x0, y0, x1, y1, col) {
    for (let x = x0 + 6; x < x1 - 4; x += 7) { R(c, x, y0, 3, 1, col); R(c, x, y1, 3, 1, col); }
    for (let y = y0 + 6; y < y1 - 4; y += 7) { R(c, x0, y, 1, 3, col); R(c, x1, y, 1, 3, col); }
    P(c, x0 + 2, y0 + 2, col); P(c, x1 - 2, y0 + 2, col); P(c, x0 + 2, y1 - 2, col); P(c, x1 - 2, y1 - 2, col);
  }

  // ---------- buildings ----------
  function terminal(c, x, y, w) {
    R(c, x - 1, y - 1, w + 2, 16, PAL.out);
    R(c, x, y, w, 5, PAL.roof); R(c, x, y, w, 2, PAL.roofLt);
    R(c, x, y + 5, w, 9, PAL.bldg);
    R(c, x, y + 12, w, 2, PAL.bldgSh);
    for (let wx = x + 3; wx < x + w - 3; wx += 6) R(c, wx, y + 7, 3, 3, PAL.win);
    const cx = x + Math.floor(w / 2);
    R(c, cx - 3, y + 1, 6, 3, PAL.roofLt); R(c, cx - 2, y + 1, 4, 3, '#F4F6F8'); R(c, cx - 1, y + 2, 2, 1, PAL.tail);
  }
  function jetbridge(c, x, y) { R(c, x, y, 2, 6, PAL.hangDk); R(c, x, y, 2, 1, PAL.hang); }
  function tower(c, x, y) {
    R(c, x + 2, y + 8, 6, 22, PAL.twrDk);
    R(c, x + 3, y + 8, 3, 22, PAL.twr);
    R(c, x - 1, y, 12, 9, PAL.out);
    R(c, x, y + 1, 10, 7, PAL.twr);
    R(c, x + 1, y + 2, 8, 4, PAL.glass);
    R(c, x + 1, y + 5, 8, 1, '#7FB8C8');
    P(c, x + 4, y - 1, PAL.out);
    P(c, x + 4, y - 2, PAL.red);
  }
  const DIG = {
    '0': ['XXX', 'X.X', 'X.X', 'X.X', 'XXX'], '1': ['.X.', 'XX.', '.X.', '.X.', 'XXX'],
    '2': ['XXX', '..X', 'XXX', 'X..', 'XXX'], '3': ['XXX', '..X', 'XXX', '..X', 'XXX'],
    '4': ['X.X', 'X.X', 'XXX', '..X', '..X'], '5': ['XXX', 'X..', 'XXX', '..X', 'XXX'],
    '6': ['XXX', 'X..', 'XXX', 'X.X', 'XXX'], '7': ['XXX', '..X', '.X.', '.X.', '.X.'],
    '8': ['XXX', 'X.X', 'XXX', 'X.X', 'XXX'], '9': ['XXX', 'X.X', 'XXX', '..X', 'XXX']
  };
  function digits(c, x, y, str, col) {
    for (let i = 0; i < str.length; i++) {
      const m = DIG[str[i]]; if (!m) continue;
      for (let j = 0; j < 5; j++) for (let k = 0; k < 3; k++) if (m[j][k] === 'X') P(c, x + i * 4 + k, y + j, col);
    }
  }
  function hangar(c, x, y, w, h, count) {
    R(c, x - 1, y - 1, w + 2, h + 2, PAL.out);
    R(c, x, y, w, 6, PAL.hangRoof);
    for (let xx = x + 2; xx < x + w - 2; xx += 4) R(c, xx, y, 1, 6, PAL.hangRoofDk);
    R(c, x, y + 6, w, h - 6, PAL.hang);
    R(c, x, y + h - 2, w, 2, PAL.hangDk);
    R(c, x + Math.floor(w / 2) - 8, y + 9, 16, h - 11, PAL.hangDk);
    for (let xx = 0; xx < 4; xx++) R(c, x + Math.floor(w / 2) - 7 + xx * 4, y + 10, 1, h - 13, PAL.hang);
    R(c, x + 2, y + 7, 22, 11, PAL.out);
    R(c, x + 3, y + 8, 20, 9, PAL.sign);
    digits(c, x + 5, y + 10, String(count), PAL.red);
    R(c, x + 11, y + 10, 10, 1, '#8A8676'); R(c, x + 11, y + 12, 8, 1, '#8A8676'); R(c, x + 11, y + 14, 10, 1, '#8A8676');
  }

  // ---------- lighting ----------
  function nightMultiply(c, W, H, time) {
    if (time === 'day') return;
    c.globalCompositeOperation = 'multiply';
    R(c, 0, 0, W, H, time === 'dusk' ? '#AFA0C6' : '#5F679E');
    if (time === 'night') { R(c, 0, 0, W, H, '#9BA0C8'); }
    c.globalCompositeOperation = 'source-over';
  }
  function glow(c, x, y, col, halo) {
    c.fillStyle = halo; c.fillRect(x - 1, y - 1, 3, 3);
    c.fillStyle = col; c.fillRect(x, y, 1, 1);
  }
  function runwayLights(c, x, y, w, h) {
    for (let cx = x + 2; cx <= x + w - 2; cx += 12) {
      glow(c, cx, y - 2, '#FFD97A', 'rgba(255,190,90,0.30)');
      glow(c, cx, y + h + 1, '#FFD97A', 'rgba(255,190,90,0.30)');
    }
    for (let k = 0; k < 3; k++) {
      glow(c, x + 1, y + 2 + k * 4, '#6FE08A', 'rgba(90,220,130,0.30)');
      glow(c, x + w - 2, y + 2 + k * 4, '#FF7A6A', 'rgba(255,110,90,0.28)');
    }
    glow(c, x + 26, y + h + 3, '#FF7A6A', 'rgba(255,110,90,0.25)');
    glow(c, x + 30, y + h + 3, '#FF7A6A', 'rgba(255,110,90,0.25)');
    glow(c, x + 34, y + h + 3, '#FFFFFF', 'rgba(255,255,255,0.25)');
    glow(c, x + 38, y + h + 3, '#FFFFFF', 'rgba(255,255,255,0.25)');
  }
  function taxiLights(c, x, y, w, h, vert) {
    if (vert) { for (let yy = y + 3; yy < y + h; yy += 10) { glow(c, x - 1, yy, '#7FD9FF', 'rgba(90,190,255,0.22)'); glow(c, x + w, yy, '#7FD9FF', 'rgba(90,190,255,0.22)'); } }
    else { for (let xx = x + 3; xx < x + w; xx += 10) { glow(c, xx, y - 1, '#7FD9FF', 'rgba(90,190,255,0.22)'); glow(c, xx, y + h, '#7FD9FF', 'rgba(90,190,255,0.22)'); } }
  }
  function litWindows(c, x, y, w) {
    for (let wx = x + 3; wx < x + w - 3; wx += 6) {
      if (seeded(wx * 3) < 0.75) { c.fillStyle = 'rgba(255,210,110,0.35)'; c.fillRect(wx - 1, y + 6, 5, 5); R(c, wx, y + 7, 3, 3, PAL.winLit); }
    }
  }
  function apronFlood(c, x, y) {
    R(c, x, y, 1, 5, PAL.out); P(c, x, y - 1, '#FFE9B0');
    c.fillStyle = 'rgba(255,220,130,0.14)';
    c.beginPath(); c.moveTo(x + 0.5, y); c.lineTo(x - 7, y + 14); c.lineTo(x + 8, y + 14); c.closePath(); c.fill();
  }
  function navLights(c, x, y, dir) {
    const s = spriteSize(JET, dir);
    if (dir === 'E' || dir === 'W') {
      glow(c, x + Math.floor(s.w / 2), y - 1, '#FF6A5A', 'rgba(255,90,80,0.30)');
      glow(c, x + Math.floor(s.w / 2), y + s.h, '#6FE08A', 'rgba(90,220,130,0.30)');
    } else {
      glow(c, x - 1, y + Math.floor(s.h / 2), '#FF6A5A', 'rgba(255,90,80,0.30)');
      glow(c, x + s.w, y + Math.floor(s.h / 2), '#6FE08A', 'rgba(90,220,130,0.30)');
    }
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

  // ---------- OVERVIEW SCENE (320x240) ----------
  function drawOverview(canvas, opts) {
    opts = opts || {};
    const time = opts.time || 'dusk';
    const W = 320, H = 240;
    const c = ctx2d(canvas, W, H, 2);
    const tint = time === 'day' ? null : time; // air-plane tint

    grass(c, W, H, 5);

    // perimeter trees
    tree(c, 4, 4, 2); tree(c, 18, 10, 1); tree(c, 296, 4, 2); tree(c, 286, 14, 1);
    tree(c, 4, 116, 1); tree(c, 300, 118, 1); tree(c, 6, 174, 2); tree(c, 224, 132, 1);

    // landmarks inside the circuit
    flagPoint(c, 52, 64);                    // Reconcile Point
    pond(c, 84, 56);                          // Build Island + CI Shoals
    ridge(c, 178, 56);                        // Review Ridge
    tree(c, 236, 92, 1); tree(c, 60, 100, 1);

    // circuit (traffic pattern)
    circuit(c, 36, 24, 284, 128, time === 'day' ? '#BCE3B2' : '#C9E8C2');

    // taxiway connectors UNDER runways
    taxiline(c, 44, 150, 7, 44, true);
    taxiline(c, 250, 150, 7, 44, true);
    taxiline(c, 148, 180, 7, 14, true);
    // runways (2 lanes)
    runway(c, 22, 146, 276, 13);
    runway(c, 22, 172, 276, 13);
    // parallel taxiway
    taxiline(c, 22, 194, 276, 7, false);
    windsock(c, 30, 132);

    // apron + terminal
    R(c, 20, 205, 150, 23, PAL.apron);
    R(c, 20, 205, 150, 1, PAL.apronLn);
    for (let x = 36; x < 160; x += 26) R(c, x, 206, 1, 12, PAL.apronLn);
    taxiline(c, 88, 201, 7, 4, true);
    terminal(c, 24, 224, 142);
    jetbridge(c, 50, 218); jetbridge(c, 102, 218); jetbridge(c, 128, 218);

    tower(c, 182, 200);
    hangar(c, 238, 200, 56, 32, opts.incident != null ? opts.incident : 0);

    // ground service specks
    R(c, 152, 208, 4, 3, '#C8A34E'); R(c, 152, 208, 4, 1, '#E0BC66');
    R(c, 146, 214, 5, 2, '#7C838A'); P(c, 146, 213, PAL.chock);

    // parked (stalled) pair — SL-7, SL-21
    plane(c, 28, 204, 'S', { dim: true, chocks: true });
    plane(c, 56, 204, 'S', { dim: true, chocks: true });

    // SL-23 taxiing in (ground — lit like the world)
    plane(c, 94, 189, 'W', {});

    nightMultiply(c, W, H, time);

    if (time !== 'day') {
      runwayLights(c, 22, 146, 276, 13);
      runwayLights(c, 22, 172, 276, 13);
      taxiLights(c, 22, 194, 276, 7, false);
      litWindows(c, 24, 224, 142);
      apronFlood(c, 24, 202); apronFlood(c, 168, 202);
      glow(c, 187, 198, '#FF6A5A', 'rgba(255,90,80,0.4)');
      c.fillStyle = 'rgba(160,225,240,0.30)'; c.fillRect(181, 201, 12, 6);
      navLights(c, 94, 189, 'W');
    }

    // SL-9 downwind — drawn ABOVE the lighting so the flight always pops
    plane(c, 144, 16, 'W', { air: true, tint: tint });
    contrail(c, 170, 24, 'W', 'crisp');
    if (time !== 'day') navLights(c, 144, 16, 'W');
  }

  // ---------- STATE VIGNETTES (200x130) ----------
  function drawState(canvas, mode) {
    const W = 200, H = 130;
    const c = ctx2d(canvas, W, H, 2);

    grass(c, W, H, mode.length);
    tree(c, 4, 4, 2); tree(c, 184, 8, 1); tree(c, 168, 106, 2); tree(c, 6, 104, 1);

    if (mode === 'parked') {
      R(c, 60, 34, 140, 66, PAL.apron);
      R(c, 60, 34, 1, 66, PAL.apronLn); R(c, 60, 34, 140, 1, PAL.apronLn);
      for (let x = 90; x < 200; x += 40) R(c, x, 36, 1, 34, PAL.apronLn);
      taxiline(c, 20, 104, 180, 7, false);
      plane(c, 100, 40, 'S', { dim: true, chocks: true });
      R(c, 110, 72, 4, 2, 'rgba(60,66,72,0.5)'); P(c, 115, 74, 'rgba(60,66,72,0.4)');
      P(c, 94, 42, PAL.chock); P(c, 94, 64, PAL.chock); P(c, 126, 42, PAL.chock); P(c, 126, 64, PAL.chock);
      plane(c, 156, 40, 'S', {});
      return;
    }

    runway(c, 12, 84, 176, 13);
    windsock(c, 178, 70);

    if (mode === 'night') {
      circuit(c, 24, 12, 176, 66, '#C9E8C2');
      nightMultiply(c, W, H, 'night');
      runwayLights(c, 12, 84, 176, 13);
      // downwind flight
      plane(c, 88, 4, 'W', { air: true, tint: 'night' });
      navLights(c, 88, 4, 'W');
      contrail(c, 114, 12, 'W', 'crisp');
      // short final with landing-light beam
      plane(c, 148, 54, 'W', { air: true, tint: 'night' });
      c.fillStyle = 'rgba(255,245,200,0.20)';
      c.beginPath(); c.moveTo(148, 62); c.lineTo(114, 86); c.lineTo(148, 92); c.closePath(); c.fill();
      navLights(c, 148, 54, 'W');
      contrail(c, 172, 62, 'W', 'thin');
    }

    if (mode === 'spinning') {
      const cx = 100, cy = 38, r = 24;
      for (let a = 0; a < 44; a++) {
        if (a % 3 === 0) continue;
        const x = Math.round(cx + Math.cos(a / 7) * r), y = Math.round(cy + Math.sin(a / 7) * r * 0.72);
        P(c, x, y, '#C9E8C2');
      }
      nightMultiply(c, W, H, 'dusk');
      // contrail bending around the loop
      for (let k = 2; k < 16; k++) {
        const a = -0.55 - k * 0.40;
        const x = Math.round(cx + Math.cos(a) * r), y = Math.round(cy + Math.sin(a) * r * 0.72);
        c.fillStyle = k < 6 ? 'rgba(255,255,255,0.95)' : k < 10 ? 'rgba(245,248,252,0.6)' : 'rgba(235,240,248,0.3)';
        c.fillRect(x, y, k < 6 ? 2 : 1, k < 6 ? 2 : 1);
      }
      plane(c, cx + r - 8, cy - 12, 'N', { air: true, tint: 'dusk' });
      navLights(c, cx + r - 8, cy - 12, 'N');
      dimExcept(c, W, H, cx + r + 1, cy + 0, 36, 0.42);
      runwayLights(c, 12, 84, 176, 13);
    }

    if (mode === 'freeze') {
      circuit(c, 24, 12, 176, 66, '#C9E8C2');
      taxiline(c, 60, 106, 130, 7, false);
      taxiline(c, 150, 94, 7, 14, true);
      // repair aircraft taxiing (ground, red tail)
      plane(c, 110, 100, 'E', { tail: PAL.repair });
      nightMultiply(c, W, H, 'night');
      runwayLights(c, 12, 84, 176, 13);
      taxiLights(c, 60, 106, 130, 7, false);
      // builds keep flying
      plane(c, 56, 4, 'W', { air: true, tint: 'night' });
      navLights(c, 56, 4, 'W');
      contrail(c, 82, 12, 'W', 'crisp');
      // landing clearance suspended — calm amber bars at the threshold
      for (let k = 0; k < 2; k++) {
        R(c, 158 + k * 6, 85, 3, 11, '#F2B33D');
        c.fillStyle = 'rgba(242,179,61,0.25)'; c.fillRect(157 + k * 6, 84, 5, 13);
      }
      glow(c, 116, 98, '#FFD97A', 'rgba(255,190,90,0.4)');
    }

    if (mode === 'amber') {
      circuit(c, 24, 12, 176, 66, '#C9E8C2');
      nightMultiply(c, W, H, 'dusk');
      const px0 = 128, py0 = 26;
      plane(c, px0, py0, 'W', { air: true, tint: 'dusk' });
      navLights(c, px0, py0, 'W');
      contrail(c, px0 + 26, py0 + 8, 'W', 'thin');
      const cx = px0 + 11, cy = py0 + 8;
      for (let a = 0; a < 30; a++) {
        const x1 = Math.round(cx + Math.cos(a / 4.77) * 17), y1 = Math.round(cy + Math.sin(a / 4.77) * 17);
        P(c, x1, y1, '#F2B33D');
        if (a % 2 === 0) { const x2 = Math.round(cx + Math.cos(a / 4.77) * 22), y2 = Math.round(cy + Math.sin(a / 4.77) * 22); P(c, x2, y2, 'rgba(242,179,61,0.6)'); }
      }
      dimExcept(c, W, H, cx, cy, 36, 0.34);
      runwayLights(c, 12, 84, 176, 13);
    }
  }

  // ---------- CREST (22x22 globe + wing) ----------
  function drawCrest(canvas, opts) {
    opts = opts || {};
    const c = ctx2d(canvas, 22, 22, Math.max(2, Math.round((canvas.clientWidth || 44) / 22)));
    const hw = [3, 5, 7, 8, 9, 9, 10, 10, 10, 10];
    const cx = 11, cy = 11;
    for (let j = 0; j < 10; j++) {
      const w = hw[j];
      R(c, cx - w - 1, cy - 10 + j, 2 * w + 2, 1, PAL.out);
      R(c, cx - w - 1, cy + 9 - j, 2 * w + 2, 1, PAL.out);
    }
    for (let j = 0; j < 10; j++) {
      const w = hw[j];
      R(c, cx - w, cy - 10 + j, 2 * w, 1, opts.bg || PAL.tail);
      R(c, cx - w, cy + 9 - j, 2 * w, 1, opts.bg || PAL.tail);
    }
    R(c, cx - 9, cy - 4, 18, 1, '#EAF2FA'); R(c, cx - 10, cy, 20, 1, '#EAF2FA'); R(c, cx - 9, cy + 4, 18, 1, '#EAF2FA');
    R(c, cx, cy - 9, 1, 18, '#EAF2FA');
    R(c, cx - 6, cy - 7, 1, 14, 'rgba(234,242,250,0.65)');
    R(c, cx + 6, cy - 7, 1, 14, 'rgba(234,242,250,0.65)');
    R(c, cx - 10, cy + 1, 8, 2, '#F4F6F8'); R(c, cx - 12, cy + 3, 6, 2, '#F4F6F8');
  }

  window.Airfield = { drawOverview: drawOverview, drawState: drawState, drawCrest: drawCrest };
})();
