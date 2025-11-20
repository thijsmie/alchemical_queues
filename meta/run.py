import os
import io
import re
import sys
import anybadge
import coverage
import subprocess
import importlib.metadata
from pathlib import Path

from mypy import api as mypy_api


this_dir = Path(__file__).parent
output_dir = this_dir / "output"
output_dir.mkdir(exist_ok=True)
repo_dir = this_dir.parent
os.chdir(repo_dir)
badge_common = {"num_value_padding_chars": 0.5}


def write_badge(name: str, badge: anybadge.Badge):
    badge_file = (output_dir / name).with_suffix(".svg")
    badge_file.unlink(missing_ok=True)
    badge.write_badge(badge_file)


def run_mypy():
    stdout, _, code = mypy_api.run([str(repo_dir / "src")])

    if code == 0:
        return "royalblue", "checked"

    m = re.search(r"Found (\d+) error", stdout)
    if m:
        return "red", f"{m.group(1)} errors"

    return "red", "fail"


def badge_mypy():
    color, result = run_mypy()
    return anybadge.Badge(
        label="mypy", value=result, default_color=color, **badge_common
    )


def run_ruff():
    format_result = subprocess.run(
        ["ruff", "format", "--check", str(repo_dir / "src")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    check_result = subprocess.run(
        ["ruff", "check", str(repo_dir / "src")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return format_result.returncode == 0 and check_result.returncode == 0


def run_coverage():
    subprocess.run(
        ["pytest", "-q", "--cov", "src", "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cov = coverage.Coverage()
    cov.load()

    output = io.StringIO()
    num = cov.report(file=output)

    return int(num), output.getvalue()


def badge_coverage():
    thresholds = {85: "red", 90: "orange", 95: "yellow", 100: "green"}
    try:
        coverage_pct, _ = run_coverage()
        return anybadge.Badge(
            "coverage",
            coverage_pct,
            value_suffix="%",
            thresholds=thresholds,
            **badge_common,
        )
    except Exception as e:
        print(f"Warning: Failed to generate coverage badge: {e}", file=sys.stderr)
        return anybadge.Badge(
            "coverage", "unknown", default_color="lightgrey", **badge_common
        )


def run_version():
    try:
        return importlib.metadata.version("alchemical_queues")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def badge_version():
    version = run_version()
    return anybadge.Badge(
        "version", version, semver=True, default_color="green", **badge_common
    )


def badges():
    write_badge(
        "pypi",
        anybadge.Badge(
            "pypi", "alchemical_queues", default_color="royalblue", **badge_common
        ),
    )
    write_badge(
        "python",
        anybadge.Badge(
            "python", "3.10|3.11|3.12|3.13", default_color="royalblue", **badge_common
        ),
    )
    write_badge(
        "documentation",
        anybadge.Badge(
            "documentation", "mkdocs", default_color="royalblue", **badge_common
        ),
    )
    write_badge("version", badge_version())
    write_badge("mypy", badge_mypy())
    write_badge("coverage", badge_coverage())


def pr_commentary():
    mypy_status = run_mypy()[1]
    ruff_status = ":+1:" if run_ruff() else ":-1:"
    
    try:
        coverage_pct, coverage_report = run_coverage()
    except Exception as e:
        coverage_report = f"Failed to generate coverage: {e}"
    
    return f"""# PR metrics

 - *mypy*: {mypy_status}
 - *formatting*: {ruff_status}

## Code coverage
```
{coverage_report}
```
"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run.py [badges|pr_commentary]", file=sys.stderr)
        sys.exit(1)
    
    if sys.argv[1] == "badges":
        badges()
    elif sys.argv[1] == "pr_commentary":
        print(pr_commentary())
    else:
        print(f"Unknown command: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)
