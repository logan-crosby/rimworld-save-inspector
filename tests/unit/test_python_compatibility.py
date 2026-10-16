import os
import subprocess
import sys
from pathlib import Path


def test_parser_imports_and_frame_default_is_compatible_with_python_311():
    repo_root = Path(__file__).resolve().parents[2]
    python311 = repo_root / ".venv-3.11" / "bin" / "python"
    interpreter = str(python311 if python311.is_file() else Path(sys.executable))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    probe = """from collections.abc import Mapping
from rimworld_save_inspector.parser.core import _Frame
frame = _Frame('li', '/li[1]')
assert isinstance(frame.attrs, Mapping) and not frame.attrs
try:
    frame.attrs['forbidden'] = 'value'
except TypeError:
    pass
else:
    raise AssertionError('the empty attrs default must remain immutable')
"""
    result = subprocess.run(
        [interpreter, "-c", probe],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
