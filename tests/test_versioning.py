import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"


def test_bump_counts_main_stage_code(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.1.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    src = tmp_path / "src" / "flowxer"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('__version__ = "0.1.0"\n')

    def run(part: str) -> str:
        return subprocess.check_output(
            [sys.executable, str(SCRIPT), part, "--root", str(tmp_path)],
            text=True,
        ).strip()

    assert run("patch") == "0.1.1"
    assert run("minor") == "0.2.1"
    assert run("major") == "1.2.1"
    assert (tmp_path / "VERSION").read_text() == "1.2.1\n"
    assert 'version = "1.2.1"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "1.2.1"' in (src / "__init__.py").read_text()
