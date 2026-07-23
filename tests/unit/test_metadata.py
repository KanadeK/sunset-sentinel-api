from __future__ import annotations

import pytest

from sunset_sentinel_api import __version__
from sunset_sentinel_api.cli import main


def test_version_is_release_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
