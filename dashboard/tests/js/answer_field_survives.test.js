/* Issue #475 — the operator's typed answer must survive the 2s poll redraw.
 *
 * The bug the owner hit live: the Answer textarea is rendered INSIDE a container that the poll
 * rebuilds with an `innerHTML` assignment, so a rebuilt textarea came back empty about a second
 * after he started typing — the dashboard's Answer verb was unusable.
 *
 * These are BEHAVIOURAL: they load the real `static/*.js` into the minimal DOM in `minidom.js`,
 * type into the real rendered field, then drive real redraw cycles and look at what is in the
 * field afterwards. The bar from the issue is "survives at least two redraw cycles", so every
 * survival case here drives three. Run by `tests/test_static_answer_field_dom.py` under pytest. */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeWindow } = require("./minidom.js");

const STATIC = path.resolve(__dirname, "..", "..", "static");

let failures = 0;
let checks = 0;

function ok(cond, what) {
  checks++;
  if (cond) return;
  failures++;
  console.error("FAIL: " + what);
}
function show(v) {
  if (v && v.nodeType === 1) {
    return "<" + v.tagName.toLowerCase() + " class=\"" + v.className + "\" data-num=\"" +
           (v.getAttribute("data-num") || "") + "\">";
  }
  if (v === null) return "null";
  if (v === undefined) return "undefined";
  return JSON.stringify(v);
}
function eq(actual, expected, what) {
  ok(actual === expected, what + "\n    expected: " + show(expected) +
     "\n    actual:   " + show(actual));
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

/** A fresh sandbox with the named static bundles loaded, exactly as index.html loads them. */
function bootstrap(files) {
  const win = makeWindow();
  const sandbox = { window: win, document: win.document, console: console,
                    setTimeout: setTimeout, clearTimeout: clearTimeout };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const f of files) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, f), "utf8"), sandbox, { filename: f });
  }
  return { win, doc: win.document, sandbox };
}

/** The server-shaped drawer object for a #163 QUESTION flight, whose go-ahead verb takes words. */
function drawerObj(journalText) {
  return {
    repo: "willprout/superlooper", num: 475, flight: "SL-475", airline: "SUPERLOOPER",
    title: "typed text in the answer field", circuit: [], off_path: null, clearance: [],
    memos: ["what should the field do?"], cargo: { present: false }, go_arounds: 0,
    links: { issue: "https://example.invalid/475", pr: null, branch: null },
    journal: [{ ts: 1, hhmm: "09:00", kind: "event", text: journalText, raw: journalText }],
    decision: { approve_act: "answer", approve_label: "Answer", approve_input: "answer",
                discuss_default: false },
  };
}

/** The server-shaped Needs You card whose Answer verb takes words. */
function cardObj(num, memo) {
  return {
    repo: "willprout/superlooper", num: num, badge: "awaiting-answer", kind: "question",
    headline: "SL-" + num + " asked you something", gloss: { plain: "waiting on you", term: "awaiting-answer" },
    memo: memo, issue_url: "https://example.invalid/" + num, dossier: null,
    actions: [{ act: "answer", label: "Answer", input: "answer", tone: "primary",
                consequence: "posts your words to the issue" },
              { act: "discuss", label: "Discuss →", tone: "link", consequence: "copies a briefing" }],
  };
}

const TYPED = "the field should keep what I typed";

// ---------------------------------------------------------------- the drawer (the reported surface)
test("drawer: typed answer survives three poll redraws, with focus and caret", () => {
  const { win, doc } = bootstrap(["keepinput.js", "drawer.js"]);
  win.Drawer.init({ onAction: function () {} });
  win.Drawer.open(drawerObj("first line"));

  const field = doc.querySelector("textarea.answer-field");
  ok(!!field, "the drawer renders an Answer field for an input-taking verb");
  field.value = TYPED;
  field.focus();
  field.setSelectionRange(4, 9);

  for (let i = 2; i <= 4; i++) {
    win.Drawer.update(drawerObj("line " + i));            // one poll redraw
    const now = doc.querySelector("textarea.answer-field");
    ok(!!now, "redraw " + i + ": the Answer field is still rendered");
    eq(now.value, TYPED, "redraw " + i + ": the typed answer survived the redraw");
    eq(doc.activeElement, now, "redraw " + i + ": focus stayed in the Answer field");
    eq(now.selectionStart, 4, "redraw " + i + ": the caret/selection start survived");
    eq(now.selectionEnd, 9, "redraw " + i + ": the caret/selection end survived");
  }
});

test("drawer: the live data behind the field still refreshes from the poll", () => {
  const { win, doc } = bootstrap(["keepinput.js", "drawer.js"]);
  win.Drawer.init({ onAction: function () {} });
  win.Drawer.open(drawerObj("first line"));
  const field = doc.querySelector("textarea.answer-field");
  field.value = TYPED;
  field.focus();

  win.Drawer.update(drawerObj("fresher line"));
  win.Drawer.update(drawerObj("freshest line"));

  const journal = doc.querySelector(".drawer-journal").textContent;
  ok(journal.indexOf("freshest line") !== -1, "the drawer shows the newest poll's journal line");
  ok(journal.indexOf("first line") === -1, "the drawer is not frozen on the stale journal line");
  eq(doc.querySelector("textarea.answer-field").value, TYPED, "…and the typed answer still survived");
});

test("drawer: the restored field is still the sibling doAnswer reads from", () => {
  const { win, doc } = bootstrap(["keepinput.js", "drawer.js"]);
  win.Drawer.init({ onAction: function () {} });
  win.Drawer.open(drawerObj("first line"));
  doc.querySelector("textarea.answer-field").value = TYPED;
  win.Drawer.update(drawerObj("second line"));

  // shell.js's doAnswer: `btn.parentNode.querySelector("textarea.answer-field")`.
  const btn = doc.querySelector('button[data-act="answer"]');
  ok(!!btn, "the Answer button is rendered");
  const read = btn.parentNode.querySelector("textarea.answer-field");
  ok(!!read, "the submit handler still finds the field beside its button");
  eq(read.value, TYPED, "…and reads the operator's surviving words, so the verb can fire");
});

test("drawer: an untouched field is not pinned — a closed and reopened drawer starts empty", () => {
  const { win, doc } = bootstrap(["keepinput.js", "drawer.js"]);
  win.Drawer.init({ onAction: function () {} });
  win.Drawer.open(drawerObj("first line"));
  doc.querySelector("textarea.answer-field").value = TYPED;
  win.Drawer.close();
  win.Drawer.open(drawerObj("first line"));
  eq(doc.querySelector("textarea.answer-field").value, "",
     "a fresh open starts from a blank field, never yesterday's draft");
});

// ------------------------------------------------- the Needs You card (the same rebuild path, #root)
test("card: typed answer survives three rebuilds of the whole panel", () => {
  const { win, doc } = bootstrap(["keepinput.js", "needsyou.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  root.innerHTML = win.NeedsYou.panelHTML([cardObj(475, "first memo")], null);

  const field = root.querySelector("textarea.answer-field");
  ok(!!field, "the card renders an Answer field");
  field.value = TYPED;
  field.focus();

  for (let i = 2; i <= 4; i++) {
    win.KeepInput.preserve(root, function () {
      root.innerHTML = win.NeedsYou.panelHTML([cardObj(475, "memo " + i)], null);
    });
    const now = root.querySelector("textarea.answer-field");
    eq(now.value, TYPED, "rebuild " + i + ": the typed answer survived the panel rebuild");
    eq(doc.activeElement, now, "rebuild " + i + ": focus stayed in the Answer field");
  }
  ok(root.textContent.indexOf("memo 4") !== -1, "the card's live memo still refreshed from the poll");
  ok(root.textContent.indexOf("first memo") === -1, "the card is not frozen on the stale memo");
});

test("card: a half-written answer follows its own flight when the panel reorders", () => {
  // Needs You is whole-field and re-derived every poll: a decision can appear, be answered, or
  // change places between two ticks. Restoring by POSITION would pour a half-written answer into
  // another flight's field — and that field POSTs to a DIFFERENT GitHub issue. So the panel is
  // driven through a reorder, an arrival and a departure, not just a redraw of the same list.
  const { win, doc } = bootstrap(["keepinput.js", "needsyou.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  const paint = (nums) => win.NeedsYou.panelHTML(nums.map((n) => cardObj(n, "memo " + n)), null);
  const byNum = () => {
    const map = {};
    root.querySelectorAll("textarea.answer-field").forEach(function (f) {
      map[f.getAttribute("data-num")] = f.value;
    });
    return map;
  };

  root.innerHTML = paint([475, 476]);
  const fields = root.querySelectorAll("textarea.answer-field");
  eq(fields.length, 2, "both waiting decisions render their own field");
  fields[1].value = "words for 476";
  fields[1].focus();

  // redraw 1: the ordinary poll, same order
  win.KeepInput.preserve(root, function () { root.innerHTML = paint([475, 476]); });
  eq(byNum()["476"], "words for 476", "same-order redraw: the words stayed on 476");
  eq(byNum()["475"], "", "same-order redraw: 475's field stayed empty");

  // redraw 2: the two flights swap places
  win.KeepInput.preserve(root, function () { root.innerHTML = paint([476, 475]); });
  eq(byNum()["476"], "words for 476", "after a reorder the draft followed its own flight");
  eq(byNum()["475"], "", "…and never landed in the other flight's field");
  eq(doc.activeElement.getAttribute("data-num"), "476", "focus followed the flight, not the slot");

  // redraw 3: a new decision arrives ABOVE the one being answered
  win.KeepInput.preserve(root, function () { root.innerHTML = paint([477, 476, 475]); });
  eq(byNum()["476"], "words for 476", "a decision arriving above did not shift the draft");
  eq(byNum()["477"], "", "the new decision's field is empty");
  eq(byNum()["475"], "", "and 475's field is still empty");

  // redraw 4: the flight being answered leaves the field entirely
  win.KeepInput.preserve(root, function () { root.innerHTML = paint([477, 475]); });
  eq(byNum()["476"], undefined, "the flight left the field, so its field is gone");
  eq(byNum()["477"], "", "its draft did not fall through to the flight that took its slot");
  eq(byNum()["475"], "", "nor to any other flight");
});

// ------------------------------------------------------------------ the general rule KeepInput sets
test("keepinput: a field the operator never touched keeps refreshing from the server", () => {
  const { win, doc } = bootstrap(["keepinput.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  root.innerHTML = '<input type="text" name="server-owned" value="one">';

  win.KeepInput.preserve(root, function () {
    root.innerHTML = '<input type="text" name="server-owned" value="two">';
  });
  eq(root.querySelector("input").value, "two",
     "an untouched field is server-owned and must not be frozen by the preservation");

  root.querySelector("input").value = "mine";      // now the operator has touched it
  win.KeepInput.preserve(root, function () {
    root.innerHTML = '<input type="text" name="server-owned" value="three">';
  });
  eq(root.querySelector("input").value, "mine", "once dirty, the operator's text wins over the poll");
});

test("keepinput: the reader keeps their place — a restored field does not yank the scroll", () => {
  // Measured in a real Chrome on the fixed build before this guard existed: with a draft focused in
  // the drawer and the panel taller than the window, scrolling UP to re-read the question was undone
  // by the very next poll (scrollTop 0 → 1223, every 2s). The operator could not read the question
  // they were answering. Restoring focus must not move the page, and the scroll position the
  // rebuild threw away has to come back with the draft.
  const { win, doc } = bootstrap(["keepinput.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  const paint = () => '<div class="panel"><textarea class="answer-field" data-num="475"></textarea></div>';
  root.innerHTML = paint();

  const field = root.querySelector("textarea.answer-field");
  field.value = "half an answer";
  field.focus();
  root.querySelector(".panel").scrollTop = 140;      // the operator scrolls back up to re-read

  win.KeepInput.preserve(root, function () { root.innerHTML = paint(); });

  eq(root.querySelector("textarea.answer-field").value, "half an answer", "the draft survived");
  eq(doc.activeElement, root.querySelector("textarea.answer-field"), "focus survived");
  eq(root.querySelector(".panel").scrollTop, 140, "…and the operator kept the place they scrolled to");
});

test("keepinput: a rebuild that changes the field's depth does not scroll the wrong box", () => {
  // The chain is walked by index. Today every renderer puts the field at a fixed depth, but the
  // module advertises itself to any future free-text field on this path — and one behind a
  // conditional wrapper would, with a naive index walk, hand the container a scroll position that
  // belongs to a box nobody ever scrolled. The chain therefore carries each ancestor's shape and
  // stops the moment it stops matching, rather than guessing.
  const { win, doc } = bootstrap(["keepinput.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  root.innerHTML = '<div class="panel"><div class="wrap">' +
    '<textarea class="answer-field" data-num="475"></textarea></div></div>';

  const field = root.querySelector("textarea.answer-field");
  field.value = "half an answer";
  field.focus({ preventScroll: true });
  root.querySelector(".panel").scrollTop = 500;      // the operator's place, in the real scroller

  // the next poll renders the same field one level shallower
  win.KeepInput.preserve(root, function () {
    root.innerHTML = '<div class="panel"><textarea class="answer-field" data-num="475"></textarea></div>';
  });

  eq(root.querySelector("textarea.answer-field").value, "half an answer", "the draft still survived");
  eq(root.scrollTop, 0, "the container was never scrolled, so it must not be given a scroll position");
  eq(root.querySelector(".panel").scrollTop, 0,
     "a box whose place we can no longer identify is left where the render put it, not guessed at");
});

test("keepinput: a rebuild with nothing typed is left exactly as the poll rendered it", () => {
  const { win, doc } = bootstrap(["keepinput.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  root.innerHTML = '<textarea class="answer-field"></textarea>';
  win.KeepInput.preserve(root, function () {
    root.innerHTML = '<textarea class="answer-field"></textarea>';
  });
  eq(root.querySelector("textarea").value, "", "an empty field stays empty");
  eq(doc.activeElement, null, "and nothing steals focus that the operator never gave");
});

console.log("\n" + checks + " checks, " + failures + " failure(s)");
process.exit(failures ? 1 : 0);
