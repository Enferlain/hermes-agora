"""Foundation smoke tests: import, version reporting, and entry point."""

import subprocess
import sys
from importlib.metadata import version

import pytest

import agora
from agora.cli import main


def test_package_version_matches_distribution_metadata() -> None:
    assert agora.__version__ == version("hermes-agora")


def test_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"agora {agora.__version__}"


def test_module_entry_point_runs() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agora", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == f"agora {agora.__version__}"
