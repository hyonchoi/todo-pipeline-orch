from pathlib import Path

from hermes_pipeline.project_config import _is_enabled, _read_project_toml


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
