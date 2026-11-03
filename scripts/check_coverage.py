"""Enforce the Home Assistant line coverage thresholds without rounding."""

import json
import sys
from pathlib import Path
from typing import Any

PACKAGE = Path("custom_components/delonghi_dehumidifier_api")
CONFIG_FLOW = PACKAGE / "config_flow.py"


def coverage_error(module: Path, records: dict[str, Any]) -> str | None:
    """Describe the unmet rule for one module, if any."""
    record = records.get(module.as_posix())
    if record is None:
        return f"{module} is missing from the coverage report"
    summary = record["summary"]
    covered, total = summary["covered_lines"], summary["num_statements"]
    if module == CONFIG_FLOW:
        if covered < total or summary["missing_branches"]:
            return f"{module} requires all statements and branches to be covered"
    elif total and covered * 100 <= total * 95:
        return f"{module} covers {covered}/{total} statements (must exceed 95%)"
    return None


def main() -> int:
    """Check every integration module in a branch-enabled coverage.py report."""
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not report["meta"]["branch_coverage"]:
        raise SystemExit(
            "Generate the report with --cov-branch to check the config flow."
        )
    if not CONFIG_FLOW.is_file():
        raise SystemExit("Run the coverage check from the repository root.")

    failed = False
    for module in sorted(PACKAGE.rglob("*.py")):
        if error := coverage_error(module, report["files"]):
            print(error, file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
