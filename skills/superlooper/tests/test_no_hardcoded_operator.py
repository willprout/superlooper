"""Guard (issue #58 / DoD): no hardcoded operator name in runtime-emitted strings.

A stranger's loop must sign its OWN work — briefs, park memos, label descriptions and
notifications all render the configured operator name (``config.operator``), never a hardcoded
"William". This gate enforces that mechanically over the engine's runtime payload:
``skill/lib``, ``skill/bin`` and ``skill/templates``.

What it scans, and why comments are exempt:

* Python files (``.py`` or a ``#!...python`` shebang) are parsed with ``ast`` and only their
  string LITERALS are checked — never their comments, which are neither emitted nor a stranger's
  audit trail (they record this repo's OWN development history, "owner ruling 2026-07-06", etc.,
  which is genuinely about William and must stay). Module/class/function DOCSTRINGS are internal
  documentation too, so they are excluded alongside comments.
* Non-Python files (templates, shell, plists) are checked line-by-line: they carry emitted prose
  (the brief footer, the debugger briefs) plus a few install-time comments, all genericized.

The neutral machine label ids ``needs-owner`` / ``needs_william`` are NOT operator names — the
label was renamed to ``needs-owner`` but the legacy ``needs-william`` label id and the persisted
``needs_william`` status enum are still recognized for back-compat (see ``lib/janitor.py``,
``lib/watchdog.py``). Both are allow-listed here so a legitimate compat token never trips the gate.
"""
import ast
from pathlib import Path

_SKILL = Path(__file__).resolve().parent.parent / "skill"
_SCOPE = (_SKILL / "lib", _SKILL / "bin", _SKILL / "templates")

# Machine label/status ids that legitimately contain "william" (lowercased) — neutral coordination
# tokens the runner reads, kept for back-compat, never an operator display name.
_ALLOWED_TOKENS = ("needs-william", "needs_william")


def _residual_has_name(text):
    """True iff ``text`` names an operator once the allow-listed machine tokens are removed."""
    low = text.lower()
    for tok in _ALLOWED_TOKENS:
        low = low.replace(tok, "")
    return "william" in low


def _is_python(path):
    if path.suffix == ".py":
        return True
    try:
        first = path.read_text(encoding="utf-8").splitlines()[:1]
    except (OSError, UnicodeDecodeError):
        return False
    return bool(first) and first[0].startswith("#!") and "python" in first[0]


def _emitted_python_strings(source):
    """Every string-literal VALUE in ``source`` that is not a docstring — the strings that can be
    emitted at runtime. Comments live outside the AST (ignored); docstrings are internal docs."""
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
            rel = p.relative_to(_SKILL.parent)
        except ValueError:
            rel = p                                    # a file outside the scope root (planted test)
        if _is_python(p):
            try:
                strings = _emitted_python_strings(text)
            except SyntaxError:
                strings = [text]                      # unparseable -> scan whole file conservatively
            for s in strings:
                if _residual_has_name(s):
                    offenders.append("%s: emitted string names an operator: %r" % (rel, s[:70]))
        else:
            for i, line in enumerate(text.splitlines(), 1):
                if _residual_has_name(line):
                    offenders.append("%s:%d: %r" % (rel, i, line.strip()[:80]))
    return offenders


def test_allowed_machine_tokens_do_not_trip_the_gate():
    # The neutral label/status ids are NOT operator names.
    assert not _residual_has_name("needs-william")
    assert not _residual_has_name('status in ("needs_william", "bounced")')
    # But a real display name does trip it, tokens present or not.
    assert _residual_has_name("Approved by William via command-center")
    assert _residual_has_name("needs-william; memo to William")


def test_gate_flags_a_planted_operator_name(tmp_path):
    planted = tmp_path / "lib.py"
    planted.write_text('MEMO = "handed back to William"\n', encoding="utf-8")
    off = _offenders(files=[planted])
    assert off and "operator" in off[0]


def test_no_hardcoded_operator_name_in_engine_runtime_strings():
    offenders = _offenders()
    assert not offenders, (
        "Runtime-emitted strings must render config.operator, never a hardcoded name "
        "(issue #58):\n  " + "\n  ".join(offenders))
