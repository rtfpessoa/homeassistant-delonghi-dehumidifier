"""Enforce the Home Assistant line coverage thresholds without rounding."""

import json
import sys
from pathlib import Path


def main() -> int:
    """Check every integration module in a branch-enabled coverage.py report."""
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not report["meta"]["branch_coverage"]:
        raise SystemExit(
            "Generate the report with --cov-branch to check the config flow."
        )

    package = Path("custom_components/delonghi_dehumidifier_api")
    if not (package / "config_flow.py").is_file():
        raise SystemExit("Run the coverage check from the repository root.")
    failures = []
    for module in sorted(package.rglob("*.py")):
        record = report["files"].get(module.as_posix())
        if record is None:
            failures.append(f"{module} is missing from the coverage report")
            continue
        summary = record["summary"]
        covered, total = summary["covered_lines"], summary["num_statements"]
        if module == package / "config_flow.py":
            if covered < total or summary["missing_branches"]:
                failures.append(
                    f"{module} requires all statements and branches to be covered"
                )
        elif total and covered * 100 <= total * 95:
            failures.append(
                f"{module} covers {covered}/{total} statements (must exceed 95%)"
            )
    for failure in failures:
        print(failure, file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
