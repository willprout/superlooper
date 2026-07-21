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

---

## `claude-idle-with-auth-warnings.txt` (issue #174)

A real **2.1.216** idle pane, captured live in a pty at 100×40 and rendered through a terminal
emulator (the raw capture is cursor-positioned, so a naive escape-strip collapses the columns).
Machine identifiers scrubbed width-preservingly; nothing the classifier reads was touched.

It is here for what it carries in its status area:

```
 ⚠ 4 MCP servers need authentication · run /mcp
 ⚠ Your login expires in 4 days · run /login to renew
```

Both are auth-adjacent, `·`-separated lines behind a `⚠` glyph, and the second one names `/login`
— the exact shape every pattern in the `logged_out` family matches on. **The session is perfectly
healthy.** It classifies as `idle`, and it must keep classifying as `idle`: any looser net over the
`/login` shape reads a working session as auth death, refuses to nudge it, and pages the owner
about a lane that is doing its job.

This screen was found by accident while trying to induce a real auth-death banner, which is
precisely why it is kept — a hand-written negative would never have included it.
