import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"


def _seed(tmp_path: Path, version: str) -> None:
    (tmp_path / "VERSION").write_text(f"{version}\n")
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    src = tmp_path / "src" / "flowxer"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(f'__version__ = "{version}"\n')


def _run(tmp_path: Path, *args: str) -> str:
    return subprocess.check_output(
        [sys.executable, str(SCRIPT), *args, "--root", str(tmp_path)],
        text=True,
    ).strip()


def test_bump_counts_main_stage_code(tmp_path: Path) -> None:
    _seed(tmp_path, "0.1.0")
    assert _run(tmp_path, "patch") == "0.1.1"
    assert _run(tmp_path, "minor") == "0.2.1"
    assert _run(tmp_path, "major") == "1.2.1"
    assert (tmp_path / "VERSION").read_text() == "1.2.1\n"
    assert 'version = "1.2.1"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "1.2.1"' in (tmp_path / "src/flowxer/__init__.py").read_text()


def test_reconcile_takes_component_wise_max(tmp_path: Path) -> None:
    _seed(tmp_path, "0.1.2")
    assert _run(tmp_path, "reconcile", "1.2.1", "0.3.1") == "1.3.2"
    assert (tmp_path / "VERSION").read_text() == "1.3.2\n"
    assert 'version = "1.3.2"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "1.3.2"' in (tmp_path / "src/flowxer/__init__.py").read_text()


def test_set_writes_all_version_files(tmp_path: Path) -> None:
    _seed(tmp_path, "0.0.0")
    assert _run(tmp_path, "set", "1.3.2") == "1.3.2"
    assert (tmp_path / "VERSION").read_text() == "1.3.2\n"
