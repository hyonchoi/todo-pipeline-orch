from pathlib import Path

from hermes_pipeline.config import Config
from hermes_pipeline.project_config import (
    _discover_projects,
    _is_enabled,
    _read_project_toml,
    _resolve_slack_channel,
)


def _mark_project(project_dir: Path) -> None:
    """A directory is a project iff <dir>/.hermes/pipeline.toml is a file."""
    (project_dir / ".hermes").mkdir(exist_ok=True)
    (project_dir / ".hermes" / "pipeline.toml").write_text('schema_version = 2\n')


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
    """project.toml slack_channel takes priority over global config."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"project__test\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "project__test"


def test_resolve_channel_env_fallback(tmp_path: Path):
    """Global config slack_channel is used when project.toml has none."""
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
    """Empty slack_channel in project.toml falls through to global config."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "env_channel"


def test_discover_projects_finds_active_projects(tmp_path: Path):
    """Should find projects with .hermes/pipeline.toml and enabled=true."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    _mark_project(p1)
    p2 = projects_dir / "project-b"
    p2.mkdir()
    _mark_project(p2)
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
    _mark_project(p1)
    p2 = projects_dir / "project-b"
    p2.mkdir()
    _mark_project(p2)
    p2_hermes = p2 / ".hermes"
    (p2_hermes / "project.toml").write_text("[active]\nenabled = false\n")
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    assert len(result) == 1
    paths = [p for p, _ in result]
    assert projects_dir / "project-a" in paths
    assert projects_dir / "project-b" not in paths


def test_discover_projects_skips_without_pipeline_contract(tmp_path: Path):
    """Directories without a .hermes/pipeline.toml file are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")  # legacy marker no longer counts
    p2 = projects_dir / "project-b"
    p2.mkdir()
    _mark_project(p2)
    p3 = projects_dir / "project-c"
    (p3 / ".hermes" / "pipeline.toml").mkdir(parents=True)  # a directory, not a file
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    paths = [p for p, _ in result]
    assert paths == [projects_dir / "project-b"]


def test_discover_projects_skips_invalid_slugs(tmp_path: Path):
    """Directories with invalid project slugs are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    p1 = projects_dir / "project-a"
    p1.mkdir()
    _mark_project(p1)
    p2 = projects_dir / "-invalid"
    p2.mkdir()
    _mark_project(p2)
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
    _mark_project(p1)
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
        _mark_project(p)
    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)
    names = [p.name for p, _ in result]
    assert names == ["alpha", "beta", "zebra"]


def test_discover_projects_warns_for_unmigrated_project_shaped_dirs(tmp_path: Path, caplog):
    """A dir with .hermes/ or TODOS.md but no pipeline.toml is a likely mistake."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    (projects_dir / "has-hermes" / ".hermes").mkdir(parents=True)
    (projects_dir / "has-todos").mkdir()
    (projects_dir / "has-todos" / "TODOS.md").write_text("# TODOS\n")
    (projects_dir / "plain").mkdir()
    config = Config(projects_dir=projects_dir)

    with caplog.at_level("WARNING", logger="hermes_pipeline.project_config"):
        assert _discover_projects(config) == []

    messages = [r.getMessage() for r in caplog.records]
    assert any("has-hermes" in m and "no .hermes/pipeline.toml" in m and "tpo init" in m for m in messages)
    assert any("has-todos" in m and "no .hermes/pipeline.toml" in m for m in messages)
    assert not any("plain" in m for m in messages)
