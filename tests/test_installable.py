"""Install-path tests for ``musubi-harness``.

These tests intentionally spawn a fresh subprocess so the assertions run in a
clean interpreter with no working-tree contamination. They are the proof that
the published package can be installed on a fresh machine, and that the public
API is usable without any ``sys.path`` indirection.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]

# Console-script entry points declared in pyproject.toml under [project.scripts].
EXPECTED_ENTRY_POINTS = ("musubi-harness", "musubi-harness-conformance")


def _platform_scripts_dir() -> Path:
    """Return the platform-correct scripts directory a venv will use.

    The venv layout differs across platforms (``Scripts`` on Windows, ``bin``
    elsewhere). Rather than branching on ``sys.platform``, ask the stdlib:
    ``EnvBuilder.ensure_directories`` populates ``context.env_exe`` with the
    interpreter path that matches the OS layout, and its parent is the
    scripts directory.
    """
    with tempfile.TemporaryDirectory() as probe_dir:
        probe = Path(probe_dir) / "probe"
        context = venv.EnvBuilder(with_pip=False, clear=True).ensure_directories(probe)
        return Path(context.env_exe).parent


# Compute once at import time. The platform-correct layout does not change
# during a test run, and probing on every test would multiply venv creation
# cost without buying anything.
_PLATFORM_SCRIPTS_DIR = _platform_scripts_dir()


def _run_in_clean_interpreter(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh virtualenv with ``musubi-harness`` installed."""
    with tempfile.TemporaryDirectory() as scratch:
        venv_path = Path(scratch) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_path))
        scripts_dir = venv_path / _PLATFORM_SCRIPTS_DIR.parts[-1]
        # The interpreter may live at ``bin/python`` (POSIX) or
        # ``Scripts/python.exe`` (Windows); locate it on disk rather than
        # hardcoding the platform-specific filename.
        candidates = sorted(scripts_dir.glob("python*"))
        assert candidates, f"venv has no python interpreter in {scripts_dir}"
        python = candidates[0]
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
        # Restrict PATH to just the venv's scripts directory so ``shutil.which``
        # cannot accidentally resolve entry points from the outer environment
        # — that would defeat the "fresh install" contract.
        env = {
            "PATH": str(scripts_dir),
            # Keep HOME and TMPDIR so packages that consult them (pip cache,
            # tempfile, XDG dirs) do not error out on the test runner.
            "HOME": os.environ.get("HOME", ""),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        # Some POSIX build chains (and Windows without a default profile) need
        # a minimal SYSTEMROOT on Windows; nothing else is required.
        if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
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
    init_path = Path(lines[0])
    assert init_path.name == "__init__.py", init_path
    assert init_path.parent.name == "musubi_harness", init_path
    # The installed path lives inside the venv's site-packages, not in the
    # working tree. If this fails the harness was imported from the source
    # tree by mistake, which would mean the install is broken. Use
    # platform-aware segments so the assertion works on Windows (``\Lib\site-packages``)
    # and POSIX alike.
    parts = init_path.parts
    assert any(p == "site-packages" or p == "dist-packages" for p in parts), init_path
    # Sanity: confirm the package was not imported from the working tree by
    # accident. The installed path must not be a subpath of the source
    # checkout — pip's ``pip install <local dir>`` builds into site-packages,
    # so an ``init_path`` under PKG_DIR would mean the install silently fell
    # back to editable mode.
    try:
        init_path.relative_to(PKG_DIR)
    except ValueError:
        pass
    else:
        raise AssertionError(f"package was imported from the working tree: {init_path}")
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
    # Probe each entry point individually with ``shutil.which`` and resolve its
    # path back inside the venv's scripts directory, so the assertion cannot
    # be fooled by an outer-environment ``musubi-harness`` collision or by
    # substring matches (``musubi-harness`` is a prefix of
    # ``musubi-harness-conformance``).
    script = (
        "import shutil\n"
        f"names = {list(EXPECTED_ENTRY_POINTS)!r}\n"
        "rows = [(name, shutil.which(name) or '') for name in names]\n"
        "print('EPS:', rows)\n"
    )
    result = _run_in_clean_interpreter(script)
    assert result.returncode == 0, f"entry-point check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    stdout = result.stdout.strip()
    assert stdout.startswith("EPS: "), stdout
    parsed = ast.literal_eval(stdout.removeprefix("EPS: "))
    assert isinstance(parsed, list) and len(parsed) == len(EXPECTED_ENTRY_POINTS), stdout
    found = {name: path for name, path in parsed}
    # Every expected entry point must resolve to a real path. Compare names
    # exactly — substring matching would silently let ``musubi-harness`` slip
    # by as a prefix of ``musubi-harness-conformance``.
    for name in EXPECTED_ENTRY_POINTS:
        assert name in found, f"entry point {name!r} is not on PATH: {stdout}"
        path = found[name]
        assert path, f"entry point {name!r} resolved to an empty path: {stdout}"
        # The resolved executable must live inside the venv's scripts dir —
        # not somewhere pulled in from the test runner's environment.
        path_obj = Path(path)
        assert path_obj.is_absolute(), (name, path_obj)
        # Neither entry point should be sitting inside site-packages; both are
        # console scripts and live alongside pip in the venv's scripts dir.
        assert "site-packages" not in path_obj.parts, (name, path_obj)


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
    marker = Path(lines[1])
    assert marker.name == "py.typed", marker
    assert marker.parent.name == "musubi_harness", marker
