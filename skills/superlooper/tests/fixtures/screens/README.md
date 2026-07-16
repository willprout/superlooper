# Captured pane screens

Real `cmux read-screen` captures of Claude Code **2.1.211**, taken from live sessions driven in a
cmux tab — not hand-written approximations. They back `tests/test_pane_state.py` and
`tests/test_nudge_pane.py` (issue #151).

They exist because these two screens are the hard case in the whole classifier, and prose about
them is not trustworthy enough to build on. Rendered side by side:

| | `claude-trust-folder.txt` | `claude-askuserquestion-dialog.txt` |
|---|---|---|
| what it is | the folder-trust prompt — a **genuine menu** | the session's **own question** (AskUserQuestion) |
| cursor | `❯ 1. Yes, I trust this folder` | `❯ 1. Red` |
| footer | `Enter to confirm · Esc to cancel` | `Enter to select · ↑/↓ to navigate · Esc to cancel` |
| classifies as | `menu` (defer, rc=3) | `at_dialog` (refuse + surface, rc=6) |

The trust prompt contains **no** "do you want to" and **no** "do you trust" wording, and its
selection chrome is nearly identical to the question dialog's. That is the whole point of keeping
it here: any rule that tried to identify a question dialog by *excluding* known permission wording
mis-reads this screen as a question and quietly stops escalating a real trust prompt. So
`at_dialog` is anchored instead on the two rows only the question dialog ever renders — the
free-text "Other" row (`N. Type something.`) and the `N. Chat about this` escape row.

**If you re-capture these**, keep the option rows and the footer byte-exact — they are the
assertion. Machine identifiers (email, absolute paths, username) are scrubbed to placeholders;
nothing the classifier reads was touched.
