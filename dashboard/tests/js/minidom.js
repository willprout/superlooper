/* A minimal DOM, enough to RUN the dashboard's own render code under plain `node`.
 *
 * Why this exists: this repo has no JS engine in its test suite (Python 3 stdlib + pytest only,
 * and CI installs nothing but pytest), so every existing static test is a STRING guard on the
 * bundle. Issue #475's acceptance bar is behavioural — "typed text survives at least two redraw
 * cycles" — and a string guard cannot prove that. Node ships on the CI runner and on the
 * deployment Mac, so the behaviour is driven here, against the REAL static files, with no npm
 * install and no change to the CI workflow (a bright line).
 *
 * This is deliberately the smallest DOM that the drawer + Needs You renderers and KeepInput
 * actually touch: element/text nodes, an innerHTML that really re-parses (so a rebuild really does
 * destroy and recreate the textarea, exactly as the browser does), class lists, a small selector
 * engine, and the focus/selection state KeepInput restores. It is NOT a browser and makes no claim
 * to be one — the rendered proof still comes from a real browser in the PR's screenshot evidence. */
"use strict";

var VOID = { br: 1, hr: 1, img: 1, input: 1, meta: 1, link: 1, source: 1, track: 1, wbr: 1 };
var RAW_TEXT = { textarea: 1, script: 1, style: 1 };
var NON_TEXT_INPUT = {
  button: 1, checkbox: 1, color: 1, date: 1, "datetime-local": 1, file: 1, hidden: 1, image: 1,
  month: 1, radio: 1, range: 1, reset: 1, submit: 1, time: 1, week: 1,
};

function decode(s) {
  return String(s)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
}

// ---- nodes ----
function TextNode(doc, text) {
  this.ownerDocument = doc;
  this.nodeType = 3;
  this.data = text;
  this.parentNode = null;
}
Object.defineProperty(TextNode.prototype, "textContent", {
  get: function () { return this.data; },
  set: function (v) { this.data = String(v); },
});

function ClassList(el) { this._el = el; }
ClassList.prototype._list = function () {
  return String(this._el.attributes["class"] || "").split(/\s+/).filter(Boolean);
};
ClassList.prototype._write = function (list) { this._el.attributes["class"] = list.join(" "); };
ClassList.prototype.contains = function (c) { return this._list().indexOf(c) !== -1; };
ClassList.prototype.add = function (c) {
  var l = this._list(); if (l.indexOf(c) === -1) { l.push(c); this._write(l); }
};
ClassList.prototype.remove = function (c) {
  this._write(this._list().filter(function (x) { return x !== c; }));
};
ClassList.prototype.toggle = function (c, on) {
  var has = this.contains(c);
  var want = on === undefined ? !has : !!on;
  if (want) this.add(c); else this.remove(c);
};

function Element(doc, tag) {
  this.ownerDocument = doc;
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.attributes = {};
  this.childNodes = [];
  this.parentNode = null;
  this.classList = new ClassList(this);
  this._listeners = {};
  this._value = null;          // null ⇒ never assigned; falls back to defaultValue
  this.selectionStart = 0;
  this.selectionEnd = 0;
  // A freshly parsed element starts scrolled to the top, exactly as the browser's does — which is
  // the whole reason a rebuild loses the reader's place.
  this.scrollTop = 0;
  this.scrollLeft = 0;
}

Element.prototype.getAttribute = function (n) {
  return Object.prototype.hasOwnProperty.call(this.attributes, n) ? this.attributes[n] : null;
};
Element.prototype.setAttribute = function (n, v) { this.attributes[n] = String(v); };
Element.prototype.hasAttribute = function (n) {
  return Object.prototype.hasOwnProperty.call(this.attributes, n);
};
Element.prototype.appendChild = function (child) {
  if (child.parentNode) child.parentNode.removeChild(child);
  child.parentNode = this;
  this.childNodes.push(child);
  return child;
};
Element.prototype.removeChild = function (child) {
  var i = this.childNodes.indexOf(child);
  if (i !== -1) { this.childNodes.splice(i, 1); child.parentNode = null; }
  return child;
};
Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
Element.prototype.removeEventListener = function () {};

Object.defineProperty(Element.prototype, "id", {
  get: function () { return this.attributes.id || ""; },
  set: function (v) { this.attributes.id = String(v); },
});
Object.defineProperty(Element.prototype, "className", {
  get: function () { return this.attributes["class"] || ""; },
  set: function (v) { this.attributes["class"] = String(v); },
});
Object.defineProperty(Element.prototype, "name", {
  get: function () { return this.attributes.name || ""; },
});
Object.defineProperty(Element.prototype, "type", {
  get: function () {
    if (this.tagName === "INPUT") return (this.attributes.type || "text").toLowerCase();
    return (this.attributes.type || "").toLowerCase();
  },
});

// The browser's own dirty-value model, which KeepInput leans on: `defaultValue` is what the HTML
// rendered (a textarea's text, an input's value attribute) and `value` is what the field holds now.
Object.defineProperty(Element.prototype, "defaultValue", {
  get: function () {
    if (this.tagName === "TEXTAREA") return this.textContent;
    return this.attributes.value || "";
  },
});
Object.defineProperty(Element.prototype, "value", {
  get: function () { return this._value === null ? this.defaultValue : this._value; },
  set: function (v) {
    this._value = String(v);
    this.selectionStart = this.selectionEnd = this._value.length;
  },
});
Element.prototype.setSelectionRange = function (a, b) {
  this.selectionStart = a; this.selectionEnd = b;
};
Element.prototype.focus = function (opts) {
  this.ownerDocument.activeElement = this;
  // A real browser scrolls the focused element into view unless preventScroll is passed; model the
  // scroll as "the nearest scrolling ancestor jumps to the element", which is what makes an
  // unguarded focus() steal the reader's place on every poll.
  if (opts && opts.preventScroll) return;
  for (var n = this.parentNode; n && n.nodeType === 1; n = n.parentNode) n.scrollTop = SCROLLED_TO_FOCUS;
};
var SCROLLED_TO_FOCUS = 9999;
Element.prototype.blur = function () {
  if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null;
};

Object.defineProperty(Element.prototype, "textContent", {
  get: function () {
    return this.childNodes.map(function (c) { return c.textContent; }).join("");
  },
  set: function (v) {
    this.childNodes.forEach(function (c) { c.parentNode = null; });
    this.childNodes = [];
    this.appendChild(new TextNode(this.ownerDocument, String(v)));
  },
});

function contains(root, node) {
  for (var n = node; n; n = n.parentNode) if (n === root) return true;
  return false;
}

Object.defineProperty(Element.prototype, "innerHTML", {
  get: function () { return this.childNodes.map(serialize).join(""); },
  set: function (html) {
    var doc = this.ownerDocument;
    // A browser blurs to <body> when the focused element is detached from the document. Model it,
    // or a test could "prove" focus survived when nothing had actually restored it.
    if (doc && doc.activeElement && contains(this, doc.activeElement)) doc.activeElement = null;
    this.childNodes.forEach(function (c) { c.parentNode = null; });
    this.childNodes = [];
    var self = this;
    parse(String(html), doc).forEach(function (n) { self.appendChild(n); });
  },
});

function serialize(node) {
  if (node.nodeType === 3) return node.data;
  var tag = node.tagName.toLowerCase();
  var attrs = Object.keys(node.attributes).map(function (k) {
    return " " + k + '="' + node.attributes[k] + '"';
  }).join("");
  if (VOID[tag]) return "<" + tag + attrs + ">";
  return "<" + tag + attrs + ">" + node.childNodes.map(serialize).join("") + "</" + tag + ">";
}

// ---- the parser: a real re-parse, so a rebuild really does replace every node ----
function parse(html, doc) {
  var out = [], stack = [], i = 0;
  function push(node) {
    if (stack.length) stack[stack.length - 1].appendChild(node);
    else out.push(node);
  }
  while (i < html.length) {
    var lt = html.indexOf("<", i);
    if (lt === -1) { if (i < html.length) push(new TextNode(doc, decode(html.slice(i)))); break; }
    if (lt > i) push(new TextNode(doc, decode(html.slice(i, lt))));
    if (html.substr(lt, 4) === "<!--") {
      var end = html.indexOf("-->", lt);
      i = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html[lt + 1] === "/") {
      var gt = html.indexOf(">", lt);
      stack.pop();
      i = gt === -1 ? html.length : gt + 1;
      continue;
    }
    var m = /^<([A-Za-z][A-Za-z0-9-]*)/.exec(html.slice(lt));
    if (!m) { push(new TextNode(doc, "<")); i = lt + 1; continue; }
    var tag = m[1].toLowerCase();
    var el = new Element(doc, tag);
    var p = lt + m[0].length;
    // attributes, quote-aware so a `>` inside a quoted value cannot end the tag early
    while (p < html.length) {
      while (p < html.length && /\s/.test(html[p])) p++;
      if (html[p] === ">" || html[p] === "/" || p >= html.length) break;
      var nm = /^[^\s=>\/]+/.exec(html.slice(p));
      if (!nm) { p++; continue; }
      var aname = nm[0];
      p += aname.length;
      var q = p;
      while (q < html.length && /\s/.test(html[q])) q++;
      if (html[q] === "=") {
        q++;
        while (q < html.length && /\s/.test(html[q])) q++;
        var quote = html[q];
        if (quote === '"' || quote === "'") {
          var close = html.indexOf(quote, q + 1);
          if (close === -1) close = html.length;
          el.attributes[aname] = decode(html.slice(q + 1, close));
          p = close + 1;
        } else {
          var uv = /^[^\s>]*/.exec(html.slice(q))[0];
          el.attributes[aname] = decode(uv);
          p = q + uv.length;
        }
      } else {
        el.attributes[aname] = "";      // a valueless attribute (data-drawer-close)
      }
    }
    while (p < html.length && html[p] !== ">") p++;
    push(el);
    i = p + 1;
    if (VOID[tag]) continue;
    if (RAW_TEXT[tag]) {
      var closeTag = "</" + tag;
      var ci = html.toLowerCase().indexOf(closeTag, i);
      if (ci === -1) ci = html.length;
      if (ci > i) el.appendChild(new TextNode(doc, decode(html.slice(i, ci))));
      var cgt = html.indexOf(">", ci);
      i = cgt === -1 ? html.length : cgt + 1;
      continue;
    }
    stack.push(el);
  }
  return out;
}

// ---- a small selector engine: comma groups of one compound each (tag, #id, .class, [attr=v]) ----
function compile(sel) {
  return String(sel).split(",").map(function (part) {
    part = part.trim();
    var spec = { tag: null, id: null, classes: [], attrs: [] };
    var re = /(^[A-Za-z][A-Za-z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=("?)([^\]"]*)\5)?\]/g, m;
    while ((m = re.exec(part))) {
      if (m[1]) spec.tag = m[1].toUpperCase();
      else if (m[2]) spec.id = m[2];
      else if (m[3]) spec.classes.push(m[3]);
      else if (m[4]) spec.attrs.push([m[4], m[6] === undefined ? null : m[6]]);
    }
    return spec;
  });
}

function matches(el, specs) {
  return specs.some(function (s) {
    if (s.tag && el.tagName !== s.tag) return false;
    if (s.id && el.id !== s.id) return false;
    for (var i = 0; i < s.classes.length; i++) if (!el.classList.contains(s.classes[i])) return false;
    for (var j = 0; j < s.attrs.length; j++) {
      var a = s.attrs[j];
      if (!el.hasAttribute(a[0])) return false;
      if (a[1] !== null && el.getAttribute(a[0]) !== a[1]) return false;
    }
    return true;
  });
}

function walk(node, fn) {
  node.childNodes.forEach(function (c) {
    if (c.nodeType !== 1) return;
    fn(c);
    walk(c, fn);
  });
}

// A NodeList-alike, deliberately WITHOUT Array's extras: a real NodeList has `length`, indices,
// `item()` and `forEach` and nothing else, so code that wrongly assumes `.filter`/`.map` on a query
// result fails here exactly as it would in the browser rather than being quietly indulged.
function nodeList(hits) {
  var list = { length: hits.length };
  hits.forEach(function (el, i) { list[i] = el; });
  list.item = function (i) { return hits[i] === undefined ? null : hits[i]; };
  list.forEach = function (fn, thisArg) { hits.forEach(fn, thisArg); };
  return list;
}

Element.prototype.querySelectorAll = function (sel) {
  var specs = compile(sel), hits = [];
  walk(this, function (el) { if (matches(el, specs)) hits.push(el); });
  return nodeList(hits);
};
Element.prototype.querySelector = function (sel) { return this.querySelectorAll(sel).item(0); };
Element.prototype.closest = function (sel) {
  var specs = compile(sel), n = this;
  while (n && n.nodeType === 1) { if (matches(n, specs)) return n; n = n.parentNode; }
  return null;
};

// ---- document / window ----
function makeWindow() {
  var doc = new Element(null, "#document");
  doc.ownerDocument = doc;
  doc.nodeType = 9;
  doc.activeElement = null;
  doc.createElement = function (tag) { return new Element(doc, tag); };
  doc.getElementById = function (id) { return doc.querySelector("#" + id); };
  doc.addEventListener = function () {};
  doc.body = new Element(doc, "body");
  doc.appendChild(doc.body);
  var win = { document: doc, setTimeout: setTimeout, clearTimeout: clearTimeout };
  win.window = win;
  return win;
}

module.exports = { makeWindow: makeWindow, NON_TEXT_INPUT: NON_TEXT_INPUT };
