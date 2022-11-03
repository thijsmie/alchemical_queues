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
from pylint import lint as pylint_api
from pylint.reporters.text import TextReporter


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


def run_pylint():
    out = io.StringIO()
    reporter = TextReporter(out)
    pylint_api.Run(
        [f"{repo_dir / 'src' / 'alchemical_queues'}", "--score=y", "--reports=n"],
        reporter=reporter,
        exit=False,
    )

    m = re.search(r"rated at ([\d\.]+)/", out.getvalue())

    try:
        if m:
            score = float(m.group(1))
        else:
            score = 0.0
    except ValueError:
        score = 0.0

    return score


def badge_pylint():
    thresholds = {8: "red", 9: "orange", 9.5: "yellow", 10.0: "green"}
    return anybadge.Badge("pylint", run_pylint(), thresholds=thresholds, **badge_common)


def run_black():
    return (
        subprocess.call(
            ["black", "--check", str(repo_dir / "src")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def badge_black():
    if run_black():
        return anybadge.Badge(
            "formatting", "black", default_color="black", **badge_common
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
    write_badge("version", badge_version())
    write_badge("mypy", badge_mypy())
    write_badge("pylint", badge_pylint())
    write_badge("formatting", badge_black())
    write_badge("coverage", badge_coverage())


def pr_commentary():
    return f"""# PR metrics

 - *mypy*: {run_mypy()[1]}
 - *pylint*: {run_pylint()}
 - *formatting*: {'good' if run_black() else 'please run black!'}

## Code coverage

{run_coverage()[1]}
"""


if sys.argv[1] == "badges":
    badges()
else:
    print(pr_commentary())
