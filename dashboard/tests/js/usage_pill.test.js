/* Issue #533 — what the usage pill actually renders when the five-hour window is absent.
 *
 * BEHAVIOURAL: loads the real `static/usagepill.js`, feeds it the server-shaped `usage` object, and
 * reads the markup it returns. The trap this exists to catch is `Math.round(null) === 0`: a missing
 * five-hour figure must render as empty, and a figure that is really zero must still say 0%. Run by
 * `tests/test_static_usage_pill.py` under pytest. */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.resolve(__dirname, "..", "..", "static");

let failures = 0;
let checks = 0;

function ok(cond, what) {
  checks++;
  if (cond) return;
  failures++;
  console.error("FAIL: " + what);
}
function has(html, needle, what) {
  ok(html.indexOf(needle) !== -1, what + "\n    wanted: " + needle + "\n    in:     " + html);
}
function hasNot(html, needle, what) {
  ok(html.indexOf(needle) === -1, what + "\n    unwanted: " + needle + "\n    in:       " + html);
}
function test(name, fn) {
  const before = failures;
  try {
    fn();
  } catch (e) {
    failures++;
    console.error("FAIL: " + name + " threw\n    " + (e && e.stack ? e.stack : e));
  }
  console.log((failures === before ? "  ok  " : "  FAIL") + "  " + name);
}

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(STATIC, "usagepill.js"), "utf8"), sandbox,
                { filename: "usagepill.js" });
const html = sandbox.window.UsagePill.html;

test("no five-hour window: the seven-day figure shows and the five-hour is empty", () => {
  const out = html({ known: true, status: "ok", five_hour_pct: null, seven_day_pct: 24 });
  has(out, ">7d<", "the seven-day window is labelled");
  has(out, "24%", "the real seven-day figure shows");
  has(out, ">5h<", "the five-hour window keeps its slot");
  has(out, "—", "the five-hour figure is an empty dash");
  hasNot(out, "0%", "a missing figure must never read as 0%");
  hasNot(out, "?", "a healthy reading is not the unknown state");
});

test("a five-hour figure missing from the object entirely renders the same way", () => {
  const out = html({ known: true, status: "ok", seven_day_pct: 24 });
  has(out, "24%", "the seven-day figure shows");
  has(out, "—", "the five-hour figure is empty");
  hasNot(out, "0%", "undefined must not read as 0%");
  hasNot(out, "NaN", "undefined must not read as NaN");
});

test("both windows present: both figures show", () => {
  const out = html({ known: true, status: "ok", five_hour_pct: 42, seven_day_pct: 71 });
  has(out, "42%", "the five-hour figure shows");
  has(out, "71%", "the seven-day figure shows");
  has(out, "width:42%", "the five-hour bar is bound to its figure");
  has(out, "width:71%", "the seven-day bar is bound to its figure");
  hasNot(out, "—", "nothing is empty");
});

test("a real zero is still a figure, not an empty window", () => {
  const out = html({ known: true, status: "ok", five_hour_pct: 0, seven_day_pct: 24 });
  has(out, "0%", "a five-hour figure of zero says 0%");
  hasNot(out, "—", "zero is not missing");
});

test("figures are rounded for display", () => {
  const out = html({ known: true, status: "ok", five_hour_pct: null, seven_day_pct: 24.6 });
  has(out, "25%", "24.6 displays as 25%");
});

test("an unknown reading keeps its honest question mark and explains why", () => {
  const out = html({ known: false, status: "no_keychain", five_hour_pct: null, seven_day_pct: null });
  has(out, "usage ?", "the unknown state reads usage ?");
  has(out, 'title="no_keychain"', "the reason is on the tooltip");
  hasNot(out, "%", "no figure is invented");
});

test("the unknown state tolerates a missing status and escapes what it is given", () => {
  has(html({ known: false }), 'title="unknown"', "no status reads as unknown");
  has(html({}), "usage ?", "an empty object is unknown");
  has(html(null), "usage ?", "no object at all is unknown");
  hasNot(html({ known: false, status: '"><b>' }), "<b>", "the status is escaped");
});

console.log(checks + " checks, " + failures + " failure(s)");
process.exit(failures ? 1 : 0);
