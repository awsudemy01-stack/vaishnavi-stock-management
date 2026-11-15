"""``python -m vaishnavi doctor [--json] [--fix] [--checks a,b] [--lang hi]`` (stdlib; runs on a bare interpreter)."""

from __future__ import annotations

import argparse
import logging
import sys

from vaishnavi.commands import Env, register
from vaishnavi.bootstrap.doctor import DoctorContext, default_checks, render_json, render_text, run_doctor, select_checks
from vaishnavi.core.i18n import Texts

log = logging.getLogger("vaishnavi.commands.doctor")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.add_argument("--fix", action="store_true", help="apply the safe automatic fixes (create database, make backup, set WAL)")
    p.add_argument("--checks", default="", help="comma-separated subset of check keys")
    p.add_argument("--ascii", action="store_true", help="plain ASCII marks (legacy consoles)")


@register("doctor", "health check of this installation (exit 0 ok · 1 notes · 2 problems)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """Run the checks and print the report in the requested form."""
    ctx = DoctorContext(settings=env.settings, paths=env.paths, config_dir=env.config_dir, lang=env.lang, fix=ns.fix)
    checks = select_checks(default_checks(), ns.checks.split(",") if ns.checks else None)
    report = run_doctor(checks, ctx)
    if ns.json:
        sys.stdout.write(render_json(report) + "\n")
    else:
        unicode_ok = not ns.ascii and (sys.stdout.encoding or "").lower().replace("-", "") in ("utf8", "utf16")
        sys.stdout.write(render_text(report, Texts.load(env.lang), unicode=unicode_ok) + "\n")
    sys.stdout.flush()
    return report.exit_code
