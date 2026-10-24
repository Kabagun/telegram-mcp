"""Keep pytest scratch files inside the configured, ignored workspace folder."""

from pathlib import Path


def pytest_configure(config):
    # pytest creates the basetemp leaf, but its parent is absent in a fresh clone.
    if config.option.basetemp is not None:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)
