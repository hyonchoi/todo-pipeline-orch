"""Tests for contract.py — pipeline execution contract schema and validation."""
from __future__ import annotations

import pytest

from hermes_pipeline.contract import (
    CONTRACT_SCHEMA_VERSION,
    DEFAULT_PROFILE,
    LEGACY_IMPLICIT_PROFILE,
    ContractMissingError,
    ContractSchemaError,
    ContractVersionMismatchError,
    PipelineContract,
    contract_path,
    default_contract,
    load_contract,
    missing_capabilities,
    required_capabilities,
    write_default_contract,
)
from hermes_pipeline.phases import Phase, load_phases, resolve_profile_phases_path


def test_default_contract_has_expected_defaults():
    contract = default_contract()
    assert contract.schema_version == CONTRACT_SCHEMA_VERSION
    assert contract.assignee == "default"
    assert set(contract.capabilities) == {"Read", "Write", "Edit", "Bash"}
    assert contract.profile == DEFAULT_PROFILE
    assert contract.profile == "native-sdd"


def test_write_default_contract_creates_file(tmp_path):
    project_state = tmp_path / ".hermes"
    written = write_default_contract(project_state)
    assert written is True
    path = contract_path(project_state)
    assert path.is_file()
    assert "schema_version = 3" in path.read_text()
    assert 'review_assignee = "default"' in path.read_text()
    assert 'profile = "native-sdd"' in path.read_text()


def test_write_default_contract_grants_the_default_profiles_capabilities(tmp_path):
    """The written capabilities come from DEFAULT_PROFILE's phases.yaml."""
    import tomllib

    project_state = tmp_path / ".hermes"
    write_default_contract(project_state)

    data = tomllib.loads(contract_path(project_state).read_text())

    expected = sorted(
        required_capabilities(load_phases(resolve_profile_phases_path(DEFAULT_PROFILE)))
    )
    assert data["profile"] == DEFAULT_PROFILE
    assert data["capabilities"] == expected


def test_write_default_contract_idempotent(tmp_path):
    project_state = tmp_path / ".hermes"
    write_default_contract(project_state)
    path = contract_path(project_state)
    path.write_text('schema_version = 2\nassignee = "custom"\ncapabilities = ["Read"]\nprofile = "gstack"\n')

    written_again = write_default_contract(project_state)

    assert written_again is False
    assert 'assignee = "custom"' in path.read_text()


def test_load_contract_missing_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    with pytest.raises(ContractMissingError, match="tpo init"):
        load_contract(project_state)


def test_load_contract_valid(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "reviewer-bot"\ncapabilities = ["Read", "Bash"]\nprofile = "agent-skills"\n'
    )

    contract = load_contract(project_state)

    assert contract.schema_version == 2
    assert contract.assignee == "reviewer-bot"
    assert contract.capabilities == ("Read", "Bash")
    assert contract.profile == "agent-skills"
    assert contract.review_assignee is None


def test_load_contract_current_schema_requires_and_loads_review_assignee(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 3\nassignee = "implementer"\n'
        'review_assignee = "reviewer"\ncapabilities = ["Read"]\nprofile = "native-sdd"\n'
    )

    contract = load_contract(project_state)

    assert contract.review_assignee == "reviewer"


def test_profile_constants():
    assert DEFAULT_PROFILE == "native-sdd"
    assert LEGACY_IMPLICIT_PROFILE == "gstack"


def test_load_contract_profile_defaults_to_legacy_implicit_gstack_when_missing(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
    )

    contract = load_contract(project_state)

    assert contract.profile == "gstack"
    assert contract.profile == LEGACY_IMPLICIT_PROFILE


def test_load_contract_marks_missing_profile_as_undeclared(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
    )

    contract = load_contract(project_state)

    assert contract.profile == "gstack"
    assert contract.profile_declared is False


def test_load_contract_marks_explicit_profile_declared(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        'profile = "gstack"\n'
    )

    contract = load_contract(project_state)

    assert contract.profile == "gstack"
    assert contract.profile_declared is True


def test_load_contract_malformed_toml_raises_schema_error(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text("schema_version = ")

    with pytest.raises(ContractSchemaError):
        load_contract(project_state)


def test_load_contract_rejects_profile_with_path_separator(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        'profile = "../../etc/passwd"\n'
    )

    with pytest.raises(ContractSchemaError, match="profile"):
        load_contract(project_state)


def test_load_contract_rejects_empty_profile(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        'profile = ""\n'
    )

    with pytest.raises(ContractSchemaError, match="profile"):
        load_contract(project_state)


def test_load_contract_accepts_hyphenated_profile(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        'profile = "agent-skills"\n'
    )

    contract = load_contract(project_state)
    assert contract.profile == "agent-skills"


def test_load_contract_rejects_trailing_hyphen_profile(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        'profile = "gstack-"\n'
    )

    with pytest.raises(ContractSchemaError, match="profile"):
        load_contract(project_state)


def test_load_contract_rejects_profile_over_64_chars(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        f'profile = "{"a" * 65}"\n'
    )

    with pytest.raises(ContractSchemaError, match="profile"):
        load_contract(project_state)


def test_load_contract_accepts_profile_at_64_chars(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\ncapabilities = ["Read"]\n'
        f'profile = "{"a" * 64}"\n'
    )

    contract = load_contract(project_state)
    assert contract.profile == "a" * 64


def test_load_contract_missing_schema_version_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text('assignee = "default"\n')

    with pytest.raises(ContractSchemaError, match="schema_version"):
        load_contract(project_state)


def test_load_contract_version_mismatch_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text("schema_version = 99\n")

    with pytest.raises(ContractVersionMismatchError, match="99"):
        load_contract(project_state)


def test_load_contract_v1_without_profile_raises_version_mismatch(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 1\nassignee = "default"\ncapabilities = ["Read"]\n'
    )

    with pytest.raises(ContractVersionMismatchError, match="1"):
        load_contract(project_state)


def test_load_contract_invalid_capabilities_type_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 2\ncapabilities = "Read"\n'
    )

    with pytest.raises(ContractSchemaError, match="capabilities"):
        load_contract(project_state)


def test_required_capabilities_excludes_gate_phases():
    phases = [
        Phase(phase_key="p1", name="P1", tools="Read,Write"),
        Phase(phase_key="gate", name="Gate", gate=True, tools="Edit"),
        Phase(phase_key="p2", name="P2", tools="Bash"),
    ]
    assert required_capabilities(phases) == {"Read", "Write", "Bash"}


def test_missing_capabilities_detects_gap():
    contract = PipelineContract(schema_version=2, capabilities=("Read",))
    phases = [Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")]
    assert missing_capabilities(contract, phases) == {"Write", "Bash"}


def test_missing_capabilities_empty_when_satisfied():
    contract = PipelineContract(schema_version=2, capabilities=("Read", "Write", "Bash"))
    phases = [Phase(phase_key="p1", name="P1", tools="Read,Write")]
    assert missing_capabilities(contract, phases) == set()



class TestResolveBundledDir:
    def test_resolves_real_filesystem_path(self):
        """Normal (non-zip) install: returns a real, existing directory."""
        from hermes_pipeline.contract import _resolve_bundled_dir

        result = _resolve_bundled_dir("hermes-identity", "pipeline")
        assert result.is_dir()
        assert (result / "SOUL.md").is_file()

    def test_falls_back_to_tempdir_for_non_filesystem_traversable(self, mocker):
        """Zip-wheel install: Path(traversable) raises, falls back to a real temp copy."""
        import hermes_pipeline.contract as contract_mod
        from hermes_pipeline.contract import _resolve_bundled_dir

        class FakeTraversable:
            def __fspath__(self):
                raise NotImplementedError("not a real filesystem path")

            def joinpath(self, *parts):
                return self

            def iterdir(self):
                return iter([])

            def is_dir(self):
                return True

            name = "pipeline"

        mocker.patch.object(
            contract_mod, "_bundled_data_root",
            return_value=FakeTraversable(),
        )
        result = _resolve_bundled_dir("hermes-identity", "pipeline")
        assert result.is_dir()
