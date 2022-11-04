import os
import io
import re
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
        return anybadge.Badge(label='mypy', value='checked', default_color='royalblue', **badge_common)

    m = re.search(r"Found (\d+) error", stdout)
    if m:
        errors = f"{m.group(1)} errors"
    else:
        errors = "fail"

    return anybadge.Badge(label='mypy', value=errors, default_color='red', **badge_common)


def run_pylint():
    out = io.StringIO()
    reporter = TextReporter(out)
    pylint_api.Run([f"{repo_dir / 'src' / 'alchemical_queues'}", "--score=y", "--reports=n"], reporter=reporter, exit=False)

    m = re.search(r"rated at ([\d\.]+)/", out.getvalue())

    try:
        if m:
            score = float(m.group(1))
        else:
            score = 0.0
    except ValueError:
        score = 0.0

    thresholds = {
        8: 'red',
        9: 'orange',
        9.5: 'yellow',
        10.0: 'green'
    }

    return anybadge.Badge('pylint %', score, thresholds=thresholds, **badge_common)


def run_black():
    p = subprocess.call(['black', '--check', str(repo_dir / "src")])

    if p == 0:
        return anybadge.Badge('formatting', 'black', default_color="black", **badge_common)
    else:
        return anybadge.Badge('formatting', 'fail', default_color="red", **badge_common)


def run_tests():
    p = subprocess.call(["pytest", "-q", "--cov", "src", "."])

    if p == 0:
        return anybadge.Badge('tests', 'passing', default_color="green", **badge_common)
    else:
        return anybadge.Badge('tests', 'failing', default_color="red", **badge_common)


def run_coverage():
    cov = coverage.Coverage()
    cov.load()

    thresholds = {
        85: 'red',
        90: 'orange',
        95: 'yellow',
        100: 'green'
    }

    return anybadge.Badge('coverage', int(cov.report()), value_suffix="%", thresholds=thresholds, **badge_common)

my_version = importlib.metadata.version("alchemical_queues")

write_badge("pypi", anybadge.Badge('pypi', 'alchemical_queues', default_color="royalblue", **badge_common))
write_badge("python", anybadge.Badge('python', '3.7|3.8|3.9|3.10|3.11', default_color="royalblue", **badge_common))
write_badge("version", anybadge.Badge('version', my_version, semver=True, default_color="green", **badge_common))
write_badge("mypy", run_mypy())
write_badge("pylint", run_pylint())
write_badge("formatting", run_black())
write_badge("tests", run_tests())
write_badge("coverage", run_coverage())