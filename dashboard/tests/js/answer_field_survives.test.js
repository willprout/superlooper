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
  const btn = doc.querySelectorAll("button").filter((b) => b.getAttribute("data-act") === "answer")[0];
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

test("card: two flights' answer fields never cross-wire", () => {
  const { win, doc } = bootstrap(["keepinput.js", "needsyou.js"]);
  const root = doc.createElement("div");
  doc.body.appendChild(root);
  const cards = () => [cardObj(475, "a"), cardObj(476, "b")];
  root.innerHTML = win.NeedsYou.panelHTML(cards(), null);

  const fields = root.querySelectorAll("textarea.answer-field");
  eq(fields.length, 2, "both waiting decisions render their own field");
  fields[1].value = "words for 476";
  fields[1].focus();

  win.KeepInput.preserve(root, function () { root.innerHTML = win.NeedsYou.panelHTML(cards(), null); });
  win.KeepInput.preserve(root, function () { root.innerHTML = win.NeedsYou.panelHTML(cards(), null); });

  const after = root.querySelectorAll("textarea.answer-field");
  eq(after[0].value, "", "the untouched flight's field stayed empty");
  eq(after[0].getAttribute("data-num"), "475", "…and is still the first flight's field");
  eq(after[1].value, "words for 476", "the typed flight's words survived, on its own field");
  eq(after[1].getAttribute("data-num"), "476", "…bound to the flight the operator typed for");
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
