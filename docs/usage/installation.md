# Installation

You can install *Alchemical Queues* using pip:

```bash
pip install alchemical-queues
```

You can also install from source:
```bash
git clone https://github.com/thijsmie/alchemical_queues
cd alchemical_queues
pip install .
```

You can also use [uv](https://docs.astral.sh/uv/) to set up your development environment and run the tests:
```bash
git clone https://github.com/thijsmie/alchemical_queues
cd alchemical_queues
uv sync --all-groups
uv run pytest
```