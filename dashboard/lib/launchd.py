"""Render the dashboard's optional launchd keep-alive plist (Task 12).

command-center's default way to run is a visible ``bin/command-center`` you can watch. For an
always-on localhost dashboard, this module renders a macOS LaunchAgent from
``templates/launchd.command-center.plist`` — following the superlooper skill's
``templates/launchd.runner.plist`` pattern (literal ``{placeholder}`` substitution), but for ONE
process: command-center is a single localhost server watching every configured repo, so there is a
single label and a single keep-alive job, not the skill's one-runner-per-repo shape.

Pure and stdlib-only (semantics server-side, decision B.1): the substitution and its fail-loud
leftover-placeholder guard live here with tests; ``bin/install-launchd.sh`` is a thin shell that
resolves absolute paths and writes what the ``--bin/--config/--log`` CLI below prints.
"""
import argparse
import re
import sys
from pathlib import Path

# One localhost process watches every configured repo → one keep-alive job, one label. (No
# owner/name slug: that is the skill's per-repo runner, a different shape entirely.)
DEFAULT_LABEL = "com.command-center"

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "templates" / "launchd.command-center.plist"

# Anything shaped like ``{lowercase_token}``. Used to scan the TEMPLATE (not the rendered output)
# for unknown placeholders: judging the template means a substituted VALUE that happens to contain a
# ``{brace}`` run — a legal, if rare, path on disk — is never mistaken for a template typo.
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_KNOWN_PLACEHOLDERS = {"label", "command_center_bin", "config_path", "log_path"}


def render_plist(template, *, label, command_center_bin, config_path, log_path):
    """Substitute the four keep-alive placeholders into ``template`` and return the plist XML.

    ``{label}`` the LaunchAgent label · ``{command_center_bin}`` absolute path to
    ``bin/command-center`` · ``{config_path}`` absolute path to the dashboard's ``config.json`` ·
    ``{log_path}`` where stdout+stderr are written. An UNKNOWN placeholder in the template (a typo
    like ``{bogus}``) raises ``ValueError`` — fail loud at install time, never ship a plist launchd
    would choke on. The check reads the template, so a substituted value containing ``{...}`` is
    fine (it is a real path, not a typo).
    """
    unknown = sorted({m.group(1) for m in _PLACEHOLDER.finditer(template)} - _KNOWN_PLACEHOLDERS)
    if unknown:
        raise ValueError("unknown placeholder(s) in launchd template: "
                         + ", ".join("{%s}" % u for u in unknown))
    return (template
            .replace("{label}", label)
            .replace("{command_center_bin}", command_center_bin)
            .replace("{config_path}", config_path)
            .replace("{log_path}", log_path))


def _main(argv):
    ap = argparse.ArgumentParser(
        description="Render the command-center launchd keep-alive plist to stdout.")
    ap.add_argument("--bin", required=True, help="absolute path to bin/command-center")
    ap.add_argument("--config", required=True, help="absolute path to the dashboard config.json")
    ap.add_argument("--log", required=True, help="absolute path for the job's stdout+stderr log")
    ap.add_argument("--label", default=DEFAULT_LABEL, help="LaunchAgent label")
    args = ap.parse_args(argv)
    sys.stdout.write(render_plist(_TEMPLATE.read_text(), label=args.label,
                                  command_center_bin=args.bin, config_path=args.config,
                                  log_path=args.log))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
