from pathlib import Path

from hermes_pipeline.config import Config
from hermes_pipeline.project_config import (
    _discover_projects,
    _is_enabled,
    _read_project_toml,
    _resolve_slack_channel,
)


def test_is_enabled_default_true_when_no_file(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    assert _is_enabled(project_dir) is True


def test_is_enabled_default_true_when_no_active_section(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("# just a comment\n")
    assert _is_enabled(project_dir) is True


def test_is_enabled_false(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = false\n")
    assert _is_enabled(project_dir) is False


def test_is_enabled_explicit_true(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = true\n")
    assert _is_enabled(project_dir) is True


def test_read_project_toml_returns_none_when_missing(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _read_project_toml(project_dir)
    assert result is None


def test_read_project_toml_parses_sections(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = true\n\n[notifications]\nslack_channel = \"project__test\"\n")
    result = _read_project_toml(project_dir)
    assert result is not None
    assert result["active"]["enabled"] is True
    assert result["notifications"]["slack_channel"] == "project__test"


def test_is_enabled_returns_true_on_parse_error(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("this is not valid toml {{{")
    assert _is_enabled(project_dir) is True


def test_resolve_channel_project_toml_priority(tmp_path: Path):
    """project.toml slack_channel takes priority over env var."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"project__test\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "project__test"


def test_resolve_channel_env_fallback(tmp_path: Path):
    """PIPELINE_SLACK_CHANNEL env var is used when project.toml has none."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "env_channel"


def test_resolve_channel_default_fallback(tmp_path: Path):
    """#alert is the final fallback when no config source provides channel."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _resolve_slack_channel(project_dir, env_channel="")
    assert result == "#alert"


def test_resolve_channel_empty_project_toml_channel_uses_env(tmp_path: Path):
    """Empty slack_channel in project.toml falls through to env var."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "env_channel"


def test_discover_projects_finds_active_projects(tmp_path: Path):
    """Should find projects with TODOS.md and enabled=true."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")
    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 2
    paths = [p for p, _ in result]
    assert projects_dir / "project-a" in paths
    assert projects_dir / "project-b" in paths


def test_discover_projects_skips_disabled(tmp_path: Path):
    """Projects with enabled=false should be skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")
    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")
    p2_hermes = p2 / ".hermes"
    p2_hermes.mkdir()
    (p2_hermes / "project.toml").write_text("[active]\nenabled = false\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 1
    paths = [p for p, _ in result]
    assert projects_dir / "project-a" in paths
    assert projects_dir / "project-b" not in paths


def test_discover_projects_skips_no_todos(tmp_path: Path):
    """Directories without TODOS.md are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 1
    paths = [p for p, _ in result]
    assert projects_dir / "project-b" in paths


def test_discover_projects_skips_invalid_slugs(tmp_path: Path):
    """Directories with invalid project slugs are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")
    p2 = projects_dir / "-invalid"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 1
    paths = [p for p, _ in result]
    assert projects_dir / "project-a" in paths


def test_discover_projects_skips_files(tmp_path: Path):
    """Non-directory entries in projects_dir are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    (projects_dir / "README.md").write_text("readme\n")
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 1


def test_discover_projects_sorted(tmp_path: Path):
    """Projects are returned in sorted order by directory name."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    for name in ["zebra", "alpha", "beta"]:
        p = projects_dir / name
        p.mkdir()
        (p / "TODOS.md").write_text("# TODOS\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    names = [p.name for p, _ in result]
    assert names == ["alpha", "beta", "zebra"]
