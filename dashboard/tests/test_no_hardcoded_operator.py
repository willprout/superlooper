"""Guard (issue #58 / DoD): no hardcoded operator name in the command center's runtime strings.

The command center signs its audit trail with the configured operator name (``config.operator``),
never a hardcoded "William": the six verbs' audit comments, the flag label description, the
needs-you cards, the digest, the tower log, and the approve toast. This gate enforces that over the
dashboard's runtime payload — ``lib/`` (pure semantics) and ``static/`` (the JS/CSS that binds them).

* Python files are parsed with ``ast``; only string LITERALS are checked (never comments — which
  record this repo's OWN development history and are genuinely about William — and never module/
  class/function docstrings, which are internal documentation).
* ``static/`` (JS/CSS/HTML) is checked line-by-line; its handful of comment mentions are genericized
  so any residual "William" is a real emitted string.

The neutral machine label ids ``needs-owner`` / ``needs_william`` are NOT operator names — the label
was renamed to ``needs-owner`` while the legacy ``needs-william`` id and the persisted
``needs_william`` status enum stay recognized for back-compat — so both are allow-listed.
"""
import ast
from pathlib import Path

_DASH = Path(__file__).resolve().parent.parent
_SCOPE = (_DASH / "lib", _DASH / "static")

_ALLOWED_TOKENS = ("needs-william", "needs_william")


def _residual_has_name(text):
    low = text.lower()
    for tok in _ALLOWED_TOKENS:
        low = low.replace(tok, "")
    return "william" in low


def _emitted_python_strings(source):
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append(node.value)
    return out


def _iter_scope_files():
    for base in _SCOPE:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            # Skip compiled/byte artifacts explicitly — a stray decodable one must never be scanned
            # as if it were source (only source and rendered templates are in scope).
            if "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            if p.is_file():
                yield p


def _offenders(files=None):
    offenders = []
    for p in (files if files is not None else _iter_scope_files()):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = p.relative_to(_DASH)
        except ValueError:
            rel = p
        if p.suffix == ".py":
            try:
                strings = _emitted_python_strings(text)
            except SyntaxError:
                strings = [text]
            for s in strings:
                if _residual_has_name(s):
                    offenders.append("%s: emitted string names an operator: %r" % (rel, s[:70]))
        else:
            for i, line in enumerate(text.splitlines(), 1):
                if _residual_has_name(line):
                    offenders.append("%s:%d: %r" % (rel, i, line.strip()[:80]))
    return offenders


def test_allowed_machine_tokens_do_not_trip_the_gate():
    assert not _residual_has_name("needs-william")
    assert not _residual_has_name('kind === "needs_william"')
    assert _residual_has_name("Approved by William via command-center")


def test_gate_flags_a_planted_operator_name(tmp_path):
    planted = tmp_path / "toast.js"
    planted.write_text('showToast("Approved by William");\n', encoding="utf-8")
    off = _offenders(files=[planted])
    assert off


def test_no_hardcoded_operator_name_in_dashboard_runtime_strings():
    offenders = _offenders()
    assert not offenders, (
        "Runtime-emitted strings must render config.operator, never a hardcoded name "
        "(issue #58):\n  " + "\n  ".join(offenders))
