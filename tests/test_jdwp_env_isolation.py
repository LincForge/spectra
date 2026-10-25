from pathlib import Path

from linc_spectra.core.jdwp_bridge import JDWPBridge, _home_search_paths


def test_home_search_paths_ignores_linc_home(monkeypatch: object, tmp_path: Path) -> None:
    """_home_search_paths no longer contains any $LINC_HOME path."""
    monkeypatch.setenv("LINC_HOME", str(tmp_path))  # type: ignore[attr-defined]
    home_paths = _home_search_paths()
    paths = JDWPBridge._get_search_paths()

    assert tmp_path / ".local" / "bin" not in home_paths
    assert tmp_path / "tools" / "kotlin-debug-adapter" / "adapter" / "bin" not in home_paths
    assert all(not p.is_relative_to(tmp_path) for p in home_paths)
    assert all(not p.is_relative_to(tmp_path) for p in paths)


def test_jdwp_search_paths_fall_back_to_real_home(monkeypatch: object, tmp_path: Path) -> None:
    """LINC_HOME redirects state, not tools — only real-home and system paths
    are searched even when LINC_HOME is set (reactor 3bee4d6c)."""
    monkeypatch.setenv("LINC_HOME", str(tmp_path))  # type: ignore[attr-defined]
    paths = JDWPBridge._get_search_paths()

    real_home = Path.home()
    assert paths == [
        real_home / "tools" / "kotlin-debug-adapter" / "adapter" / "bin",
        real_home / ".local" / "bin",
        Path("/usr/local/lib/kotlin-debug-adapter/adapter/bin"),
    ]


def test_jdwp_search_paths_without_linc_home(monkeypatch: object) -> None:
    """No LINC_HOME → real-home paths only, deduplicated, sdk fallback last."""
    monkeypatch.delenv("LINC_HOME", raising=False)  # type: ignore[attr-defined]
    paths = JDWPBridge._get_search_paths()

    real_home = Path.home()
    assert paths == [
        real_home / "tools" / "kotlin-debug-adapter" / "adapter" / "bin",
        real_home / ".local" / "bin",
        Path("/usr/local/lib/kotlin-debug-adapter/adapter/bin"),
    ]
