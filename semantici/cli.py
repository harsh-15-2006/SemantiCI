"""Command-line release gate for CI/CD.

    python -m semantici.cli gate <app_dir> [--env KEY=VALUE ...]

Runs the approved suite in <app_dir>/business_checks/suite.yml and exits with
status 1 when the release must be blocked.
"""
import argparse
import json
import os
import sys

from .harness import execute_suite, load_suite, suite_path


def _markdown(report) -> str:
    icon = "✅ PASS" if report["decision"] == "PASS" else "❌ RELEASE BLOCKED"
    lines = [f"## SemantiCI business-correctness gate: {icon}", "",
             "| Invariant | Severity | Result |", "|---|---|---|"]
    for c in report["checks"]:
        result = "passed" if c["passed"] else ("error" if c.get("error") else f"VIOLATED ({len(c['violations'])} record(s))")
        lines.append(f"| {c['description']} | {c['severity']} | {result} |")
    for reason in report["reasons"]:
        lines.append(f"\n**{reason}**")
    return "\n".join(lines) + "\n"


def gate(app_dir, env) -> int:
    suite = load_suite(app_dir)
    if not suite["invariants"]:
        print(f"SemantiCI: no approved invariants in {suite_path(app_dir)}; nothing to verify.")
        return 0
    report = execute_suite(app_dir, suite["workflows"], suite["invariants"], env)

    print("=" * 70)
    print("SemantiCI business-correctness gate")
    print("=" * 70)
    for wf in report["workflows"]:
        print(f"scenario  {'ok  ' if wf['ok'] else 'FAIL'}  {wf['name']} ({len(wf['steps'])} steps)")
    for c in report["checks"]:
        state = "pass" if c["passed"] else ("ERR " if c.get("error") else "FAIL")
        print(f"invariant {state}  [{c['severity']}] {c['description']}")
        for row in c["violations"][:5]:
            print(f"            violating record: {row}")
        if c.get("error"):
            print(f"            error: {c['error']}")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    for r in report["reasons"]:
        print(f"BLOCKING: {r}")
    print("-" * 70)
    print(f"RELEASE STATUS: {'PASS' if report['decision'] == 'PASS' else 'BLOCKED'}")

    with open("semantici-report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(_markdown(report))
    return 0 if report["decision"] == "PASS" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="semantici")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("gate", help="run the approved business checks and block on critical violations")
    g.add_argument("app_dir")
    g.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    env = dict(e.split("=", 1) for e in args.env if "=" in e)
    return gate(args.app_dir, env)


if __name__ == "__main__":
    sys.exit(main())
