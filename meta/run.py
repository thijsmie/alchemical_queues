import importlib.metadata
import io
import json
import os
import re
import subprocess
from pathlib import Path

import anybadge
import coverage
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
    stdout, _, code = mypy_api.run([str(repo_dir)])

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


def run_ruff_lint():
    out = subprocess.run(
        ["ruff", "check", "--output-format=json", str(repo_dir / "src")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return len(json.loads(out.stdout or "[]"))


def badge_ruff_lint():
    issues = run_ruff_lint()
    value = "clean" if issues == 0 else f"{issues} issues"
    return anybadge.Badge(
        "ruff",
        value,
        default_color="green" if issues == 0 else "red",
        **badge_common,
    )


def run_ruff_format():
    return (
        subprocess.call(
            ["ruff", "format", "--check", str(repo_dir / "src")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def badge_ruff_format():
    if run_ruff_format():
        return anybadge.Badge(
            "formatting", "ruff", default_color="black", **badge_common
        )
    else:
        return anybadge.Badge("formatting", "fail", default_color="red", **badge_common)


def run_coverage():
    subprocess.call(
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
    return anybadge.Badge(
        "coverage",
        run_coverage()[0],
        value_suffix="%",
        thresholds=thresholds,
        **badge_common,
    )


def run_version():
    return importlib.metadata.version("alchemical_queues")


def badge_version():
    return anybadge.Badge(
        "version", run_version(), semver=True, default_color="green", **badge_common
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
            "python", "3.7|3.8|3.9|3.10|3.11", default_color="royalblue", **badge_common
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
    write_badge("ruff", badge_ruff_lint())
    write_badge("formatting", badge_ruff_format())
    write_badge("coverage", badge_coverage())


badges()
