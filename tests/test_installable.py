"""Install-path tests for ``musubi-harness``.

These tests intentionally spawn a fresh subprocess so the assertions run in a
clean interpreter with no working-tree contamination. They are the proof that
the published package can be installed on a fresh machine, and that the public
API is usable without any ``sys.path`` indirection.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import venv
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]


def _run_in_clean_interpreter(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh virtualenv with ``musubi-harness`` installed."""
    with tempfile.TemporaryDirectory() as scratch:
        venv_path = Path(scratch) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_path))
        python = venv_path / "bin" / "python"
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Install the package from the local checkout. This exercises the same
        # pyproject that PyPI builds from; if it fails here, a PyPI release
        # would fail too.
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "--quiet", str(PKG_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        return subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


def test_package_installs_into_clean_venv() -> None:
    """``pip install`` succeeds and ``import musubi_harness`` works in a fresh venv."""
    result = _run_in_clean_interpreter("import musubi_harness; print(musubi_harness.__file__); print(len(musubi_harness.__all__))")
    assert result.returncode == 0, f"install failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2, result.stdout
    init_path = lines[0]
    assert init_path.endswith("/musubi_harness/__init__.py"), init_path
    # The installed path lives inside the venv's site-packages, not in the
    # working tree. If this fails the harness was imported from the source
    # tree by mistake, which would mean the install is broken.
    assert "/site-packages/" in init_path or "/dist-packages/" in init_path, init_path
    assert int(lines[1]) >= 30, f"expected at least 30 public symbols, got {lines[1]}"


def test_public_api_is_usable_without_path_hacks() -> None:
    """Adapter authors can build a PluginRuntime from a clean venv with one import."""
    result = _run_in_clean_interpreter(
        "from musubi_harness import PluginRuntime, RuntimeConfig, PluginMcpFacade; "
        "from musubi_harness import TurnEnvelope, Outbox, CapturePolicy; "
        "from musubi_harness import Drainer, DeliveryStore, MemoryDataClient; "
        "from pathlib import Path; "
        "runtime = PluginRuntime('musubi-codex', default_data_root=Path('/tmp/none')); "
        "config = RuntimeConfig(actor='tama', presence='tama/command-chair', zone='home'); "
        "facade = PluginMcpFacade(runtime, source='codex', event_prefix='codex', "
        "owner_label='test', server_name='test'); "
        "print('OK', facade.runtime is runtime, config.episodic_namespace)"
    )
    assert result.returncode == 0, f"public-api smoke failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert result.stdout.startswith("OK True tama/command-chair/episodic"), result.stdout


def test_console_scripts_resolve_on_path() -> None:
    """Both ``musubi-harness`` and ``musubi-harness-conformance`` are installed on PATH."""
    result = _run_in_clean_interpreter(
        "import shutil; "
        "eps = ['musubi-harness', 'musubi-harness-conformance']; "
        "reachable = [name for name in eps if shutil.which(name)]; "
        "print('EPS:', ','.join(sorted(reachable)))"
    )
    assert result.returncode == 0, f"entry-point check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    eps_line = result.stdout.strip()
    assert "musubi-harness" in eps_line, eps_line
    assert "musubi-harness-conformance" in eps_line, eps_line


def test_py_typed_marker_ships_with_wheel() -> None:
    """The installed package carries PEP 561 ``py.typed`` so downstream mypy is happy."""
    result = _run_in_clean_interpreter(
        "import importlib.util, pathlib; "
        "spec = importlib.util.find_spec('musubi_harness'); "
        "root = pathlib.Path(spec.origin).parent; "
        "marker = root / 'py.typed'; "
        "print('TYPED' if marker.is_file() else 'UNTYPED'); "
        "print(marker)"
    )
    assert result.returncode == 0, f"py.typed check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "TYPED", f"installed package is missing py.typed marker:\n{result.stdout}\nstderr={result.stderr}"
    assert lines[1].endswith("/musubi_harness/py.typed"), lines[1]
