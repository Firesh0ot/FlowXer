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


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def _conflicting_merge(tmp_path: Path, ours: str, theirs: str) -> None:
    """dev at `ours`, stage at `theirs`, then `git merge stage` on dev (conflicts in all version files)."""
    _git(tmp_path, "init", "-q", "-b", "dev")
    _seed(tmp_path, "7.15.30")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _git(tmp_path, "checkout", "-q", "-b", "stage")
    _run(tmp_path, "set", theirs)
    _git(tmp_path, "commit", "-q", "-am", theirs)
    _git(tmp_path, "checkout", "-q", "dev")
    _run(tmp_path, "set", ours)
    _git(tmp_path, "commit", "-q", "-am", ours)
    assert _git(tmp_path, "merge", "stage", "--no-edit").returncode != 0


def test_reconcile_refs_resolves_a_conflicting_version_merge(tmp_path: Path) -> None:
    # Merge-back main → stage → dev after a major bump: VERSION held conflict markers and
    # reconcile-refs failed ("VERSION must be MAJOR.MINOR.PATCH").
    _conflicting_merge(tmp_path, "7.15.31", "8.15.31")
    assert "<<<<<<< " in (tmp_path / "VERSION").read_text()
    assert _run(tmp_path, "reconcile-refs", "--refs", "dev", "stage") == "8.15.31"
    for name, line in [
        ("VERSION", "8.15.31"),
        ("pyproject.toml", 'version = "8.15.31"'),
        ("src/flowxer/__init__.py", '__version__ = "8.15.31"'),
    ]:
        text = (tmp_path / name).read_text()
        assert "<<<<<<< " not in text and "=======" not in text and ">>>>>>> " not in text
        assert line in text


def test_reconcile_refs_leaves_other_conflicts_to_a_person(tmp_path: Path) -> None:
    _conflicting_merge(tmp_path, "7.15.31", "8.15.31")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\n<<<<<<< HEAD\nversion = "7.15.31"\ndependencies = ["a"]\n=======\nversion = "8.15.31"\n>>>>>>> stage\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "reconcile-refs",
            "--refs",
            "dev",
            "stage",
            "--root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "outside the version line" in result.stderr
