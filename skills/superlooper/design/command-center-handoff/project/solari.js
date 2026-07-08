/* Solari split-flap board — flagship animation. window.Solari.mount(container) */
(function () {
  'use strict';
  const CH = ' ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:·-#';
  const ROWS = [
    '18:57 SL-23 ADD A MOTTO FOOTER',
    '06:12 SL-16 GREETING MORE FORMAL',
    '05:47 SL-15 TIDY HEADER GREETING'
  ];
  const WID = Math.max.apply(null, ROWS.map(r => r.length));
  const GLYPH_FONT = "600 24px/44px 'IBM Plex Sans Condensed', sans-serif";

  function styl(el, s) { for (const k in s) el.style[k] = s[k]; }

  function makeTile() {
    const t = document.createElement('span');
    styl(t, {
      position: 'relative', display: 'inline-block', width: '30px', height: '44px',
      background: '#14181E', borderRadius: '4px', overflow: 'hidden',
      boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.09), inset 0 -10px 12px rgba(0,0,0,0.3)'
    });
    function half(top) {
      const h = document.createElement('span');
      styl(h, { position: 'absolute', left: '0', right: '0', height: '22px', overflow: 'hidden', top: top ? '0' : '22px', background: top ? '#2A313B' : '#1B2129' });
      const g = document.createElement('span');
      styl(g, { display: 'block', width: '30px', height: '44px', textAlign: 'center', font: GLYPH_FONT, color: '#F2F4EF', transform: top ? 'none' : 'translateY(-22px)' });
      h.appendChild(g);
      t.appendChild(h);
      return g;
    }
    t._top = half(true);
    t._bot = half(false);
    const seam = document.createElement('span');
    styl(seam, { position: 'absolute', left: '0', right: '0', top: '50%', height: '1px', background: 'rgba(0,0,0,0.65)', zIndex: '3' });
    t.appendChild(seam);
    t._ch = ' ';
    return t;
  }
  function setTile(t, ch) { t._top.textContent = ch; t._bot.textContent = ch; t._ch = ch; }

  // one mechanical flip: flap carrying the old glyph falls over the new one
  function flipStep(t, next, dur) {
    const flap = document.createElement('span');
    styl(flap, {
      position: 'absolute', left: '0', right: '0', top: '0', height: '22px', overflow: 'hidden',
      background: '#2A313B', zIndex: '2', transformOrigin: 'bottom center', borderRadius: '4px 4px 0 0',
      boxShadow: '0 1px 2px rgba(0,0,0,0.4)'
    });
    const g = document.createElement('span');
    styl(g, { display: 'block', width: '30px', height: '44px', textAlign: 'center', font: GLYPH_FONT, color: '#F2F4EF' });
    g.textContent = t._ch;
    flap.appendChild(flap._g = g);
    t.appendChild(flap);
    t._top.textContent = next;
    const done = function () { if (flap.parentNode) { flap.remove(); t._bot.textContent = next; t._ch = next; } };
    if (flap.animate) {
      const a = flap.animate([{ transform: 'rotateX(0deg)' }, { transform: 'rotateX(-88deg)' }], { duration: dur, easing: 'ease-in' });
      a.onfinish = done;
      setTimeout(done, dur + 40); // fallback if WAAPI is throttled
    } else {
      done();
    }
  }
  // short glyph runway ending at target (always real glyphs, never blur)
  function pathTo(target, k) {
    const ti = Math.max(0, CH.indexOf(target));
    const seq = [];
    for (let j = k; j >= 0; j--) seq.push(CH[(ti - j + CH.length * 3) % CH.length]);
    return seq;
  }

  window.Solari = {
    mount: function (container) {
      container.innerHTML = '';
      const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const rows = [];
      for (let r = 0; r < 2; r++) {
        const row = document.createElement('div');
        styl(row, { display: 'flex', gap: '3px' });
        const tiles = [];
        for (let i = 0; i < WID; i++) { const t = makeTile(); row.appendChild(t); tiles.push(t); }
        container.appendChild(row);
        rows.push(tiles);
      }
      function land(tiles, str) {
        const s = str.toUpperCase();
        for (let i = 0; i < WID; i++) {
          const target = i < s.length ? s[i] : ' ';
          const t = tiles[i];
          if (t._ch === target) continue;
          if (reduced) { setTile(t, target); continue; }
          const k = 3 + ((i * 7) % 7); // 4–10 flips per tile, staggered left→right
          const seq = pathTo(target, k);
          seq.forEach(function (ch2, j) {
            setTimeout(function () { flipStep(t, ch2, j === seq.length - 1 ? 92 : 82); }, i * 40 + j * 90);
          });
        }
      }
      let idx = 0;
      setTimeout(function () { land(rows[0], ROWS[0]); }, 350);
      setTimeout(function () { land(rows[1], ROWS[1]); }, 850);
      return {
        replay: function () {
          idx = (idx + 1) % ROWS.length;
          land(rows[0], ROWS[idx]);
          setTimeout(function () { land(rows[1], ROWS[(idx + 1) % ROWS.length]); }, 320);
        }
      };
    }
  };
})();
