import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize("arguments", [[], ["--user", "root"]])
def test_apply_requires_nonroot_report_owner_before_device_access(monkeypatch, capsys, arguments):
    spec = importlib.util.spec_from_file_location("nano_cleanup", "deploy/clean-nano-system.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(sys, "argv", ["clean-nano-system.py", "--apply", *arguments])

    def unexpected_read(_path):
        pytest.fail("Must reject an invalid owner before inspecting the device")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert "report owner" in capsys.readouterr().err or not arguments
