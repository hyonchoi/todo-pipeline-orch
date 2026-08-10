
import copy
import re
from pathlib import Path

import pytest
import yaml

from hermes_pipeline.contract import ContractSchemaError
from hermes_pipeline.phases import (
    PhaseProfile,
    PhasePromptRenderError,
    _render_phase_prompt,
    load_phase_profile,
    load_phases,
    load_profile_prerequisites,
    resolve_profile_phases_path,
)

FIXTURE = """
phases:
  - phase_key: "phase_2_autoplan"
    name: "Phase 2: Autoplan"
    prompt: "do autoplan"
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800
  - phase_key: "phase_8_finish"
    name: "Phase 8: Finish Branch"
    prompt: "finish branch"
    tools: "Read,Write,Bash"
    turns: 15
"""


def extract_bundled_skill_references(profile, phases):
    prompt_text = "\n".join(phase.prompt for phase in phases)
    if profile == "gstack":
        return {
            "ai-coding-agents",
            *set(
            re.findall(
                r"\{(?:skill_prefix|superpowers_skill_prefix)\}"
                r"([a-z][a-z0-9-]*)",
                prompt_text,
            )
            ),
        }
    if profile == "agent-skills":
        return set(re.findall(r"\bagent-skills:[a-z][a-z0-9-]*", prompt_text))
    if profile == "native-sdd":
        return {"ai-coding-agents"}
    raise AssertionError(f"missing test-owned extraction pattern for {profile}")


def test_prerequisite_metadata_covers_every_bundled_skill_reference():
    for profile in ("gstack", "agent-skills"):
        metadata = load_profile_prerequisites(profile)
        declared = {item.skill_id for item in metadata.skills}
        phases = load_phases(resolve_profile_phases_path(profile))
        prompt_text = "\n".join(phase.prompt for phase in phases)
        for skill_id in declared:
            if skill_id == "ai-coding-agents":
                continue
            assert skill_id in prompt_text
        assert extract_bundled_skill_references(profile, phases) == declared


def test_load_phases_rejects_empty_profile(tmp_path):
    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text("phases: []\n")

    with pytest.raises(ValueError, match=r"phases.*must contain at least one"):
        load_phases(phases_path)


def test_gstack_prerequisites_are_conditional_and_verified():
    metadata = load_profile_prerequisites("gstack")
    assert {
        item.skill_id: item.distribution_owner for item in metadata.skills
    } == {
        "ai-coding-agents": "hermes",
        "autoplan": "gstack",
        "writing-plans": "superpowers",
        "subagent-driven-development": "superpowers",
        "review": "gstack",
        "cso": "gstack",
        "qa": "gstack",
        "document-release": "gstack",
        "document-generate": "gstack",
        "ship": "gstack",
    }
    for item in metadata.skills:
        assert item.support == "Conditional"
        if item.distribution_owner == "hermes":
            assert item.clients["claude"].invocation == "claude -p"
            assert item.clients["codex"].invocation == "codex exec"
            assert item.clients["claude"].discovery_root == "Hermes skill registry"
            assert item.clients["codex"].discovery_root == "Hermes skill registry"
        elif item.distribution_owner == "gstack":
            assert item.clients["claude"].invocation == f"/{item.skill_id}"
            assert item.clients["codex"].invocation == f"${item.skill_id}"
            assert item.clients["claude"].discovery_root == ".claude/skills"
            assert item.clients["codex"].discovery_root == ".codex/skills"
        else:
            assert item.clients["claude"].invocation == f"/{item.skill_id}"
            assert item.clients["codex"].invocation == f"$superpowers:{item.skill_id}"
            assert "claude-plugins-official/superpowers" in (
                item.clients["claude"].discovery_root or ""
            )
            assert "openai-curated-remote/superpowers" in (
                item.clients["codex"].discovery_root or ""
            )


def test_agent_skills_prerequisites_do_not_guess_external_contracts():
    metadata = load_profile_prerequisites("agent-skills")
    for item in metadata.skills:
        assert item.distribution_owner == "agent-skills plugin"
        assert item.support == "Unverified"
        assert item.clients["claude"].discovery_root is None
        assert item.clients["claude"].invocation is None
        assert item.clients["codex"].discovery_root is None
        assert item.clients["codex"].invocation is None


def _documented_client_contract(client):
    if client.discovery_root is None:
        return "Unverified external plugin mechanism"
    return f"`{client.discovery_root}` / `{client.invocation}`"


def _documented_prerequisite_row(profile, item):
    return (
        f"| `{profile}` | `{item.skill_id}` | {item.distribution_owner} | "
        f"{_documented_client_contract(item.clients['claude'])} | "
        f"{_documented_client_contract(item.clients['codex'])} | "
        f"{item.support} |"
    )


def test_documented_prerequisite_rows_match_all_package_metadata_fields():
    readme = Path("README.md").read_text()
    reference = Path("docs/reference-cli.md").read_text()
    for profile in ("gstack", "agent-skills"):
        metadata = load_profile_prerequisites(profile)
        for item in metadata.skills:
            row = _documented_prerequisite_row(profile, item)
            assert row in readme
            assert row in reference


def test_readme_config_walkthrough_initializes_once_and_runs_sequentially():
    readme = Path("README.md").read_text()
    core_workflows = readme.split("## Core workflows", 1)[1].split(
        "## Subcommands", 1
    )[0]
    expected = """\
```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo config get prompt_client
tpo config set prompt_client codex
tpo config get prompt_client
tpo doctor <project>
```"""
    assert expected in core_workflows
    assert core_workflows.count("tpo config init") == 1


def test_profile_guide_lists_exact_metadata_skill_inventories():
    guide = Path("docs/howto-agent-skills-profile.md").read_text()
    for profile in ("gstack", "agent-skills"):
        marker = f"- **`{profile}`**"
        start = guide.index(marker)
        boundaries = (
            guide.find("\n- **", start + len(marker)),
            guide.find("\n\n", start + len(marker)),
        )
        end = min(boundary for boundary in boundaries if boundary != -1)
        inventory = guide[start:end].split("Skills:", 1)[1]
        documented = re.findall(r"`([^`]+)`", inventory)
        expected = [
            item.skill_id for item in load_profile_prerequisites(profile).skills
        ]
        assert documented == expected


def test_profile_guide_documents_complete_profile_data_and_doctor_error():
    guide = Path("docs/howto-agent-skills-profile.md").read_text()
    add_profile = guide.split("## Adding a new profile", 1)[1].split(
        "## Troubleshooting", 1
    )[0]
    assert "`phases.yaml`" in add_profile
    assert "`prerequisites.yaml`" in add_profile
    for field in (
        "`schema_version`",
        "`profile`",
        "`skills`",
        "`skill_id`",
        "`distribution_owner`",
        "`support`",
        "`clients`",
        "`discovery_root`",
        "`invocation`",
    ):
        assert field in add_profile

    troubleshooting = guide.split("## Troubleshooting", 1)[1]
    assert "INVALID: failed to load profile data for '<name>'" in troubleshooting
    assert "`prerequisites.yaml`" in troubleshooting


def test_release_qualification_covers_conditional_pairs():
    guide = Path("docs/release-qualification-agent-clients.md").read_text()
    for profile in ("gstack", "agent-skills"):
        for item in load_profile_prerequisites(profile).skills:
            if item.support != "Conditional":
                continue
            for client in ("claude", "codex"):
                assert f"`{profile}` / `{client}`" in guide
    assert "Normal CI does not run these checks" in guide


def test_candidate_evidence_inventory_matches_conditional_pairs():
    evidence_root = (
        Path("docs/release-evidence/agent-clients") / "candidate-source-snapshot"
    )
    conditional_pairs = {
        (profile, client)
        for profile in ("gstack", "agent-skills")
        for item in load_profile_prerequisites(profile).skills
        if item.support == "Conditional"
        for client in ("claude", "codex")
    }

    expected_names = {
        f"{profile}-{client}.md" for profile, client in conditional_pairs
    }
    assert {path.name for path in evidence_root.glob("*.md")} == expected_names


def _load_temporary_prerequisites(monkeypatch, tmp_path, metadata_text):
    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text("phases: []\n")
    if metadata_text is not None:
        (tmp_path / "prerequisites.yaml").write_text(metadata_text)
    monkeypatch.setattr(
        "hermes_pipeline.phases.resolve_profile_phases_path",
        lambda profile: phases_path,
    )
    return load_profile_prerequisites("gstack")


VALID_PREREQUISITE_METADATA = {
    "schema_version": 1,
    "profile": "gstack",
    "skills": [
        {
            "skill_id": "review",
            "distribution_owner": "gstack",
            "support": "Conditional",
            "clients": {
                "claude": {
                    "discovery_root": ".claude/skills",
                    "invocation": "/review",
                },
                "codex": {
                    "discovery_root": ".codex/skills",
                    "invocation": "$review",
                },
            },
        }
    ],
}
_DELETE = object()


def _metadata_with_updates(*updates):
    metadata = copy.deepcopy(VALID_PREREQUISITE_METADATA)
    for path, value in updates:
        target = metadata
        for key in path[:-1]:
            target = target[key]
        if value is _DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = value
    return yaml.safe_dump(metadata, sort_keys=False)


@pytest.mark.parametrize(
    ("updates", "field"),
    [
        (((("schema_version",), 2),), "schema_version"),
        (((("profile",), "other"),), "profile"),
        (((("skills",), {}),), "skills"),
        (((("skills",), ["review"]),), "skills[0]"),
        (((("skills", 0, "skill_id"), ""),), "skills[0].skill_id"),
        (
            (
                (
                    ("skills",),
                    [
                        copy.deepcopy(VALID_PREREQUISITE_METADATA["skills"][0]),
                        copy.deepcopy(VALID_PREREQUISITE_METADATA["skills"][0]),
                    ],
                ),
            ),
            "skills[1].skill_id",
        ),
        (
            ((("skills", 0, "distribution_owner"), ""),),
            "skills[0].distribution_owner",
        ),
        (((("skills", 0, "support"), "Maybe"),), "skills[0].support"),
        (
            ((("skills", 0, "clients", "codex"), _DELETE),),
            "skills[0].clients",
        ),
        (
            ((("skills", 0, "clients", "claude", "invocation"), _DELETE),),
            "skills[0].clients.claude",
        ),
        (
            ((("skills", 0, "clients", "claude", "discovery_root"), None),),
            "skills[0].clients.claude.discovery_root",
        ),
        (
            (
                (("skills", 0, "support"), "Unverified"),
                (("skills", 0, "clients", "claude", "discovery_root"), None),
                (("skills", 0, "clients", "codex", "discovery_root"), None),
                (("skills", 0, "clients", "codex", "invocation"), None),
            ),
            "skills[0].clients.claude.invocation",
        ),
    ],
)
def test_prerequisite_metadata_validation_names_path_and_field(
    monkeypatch, tmp_path, updates, field
):
    metadata_text = _metadata_with_updates(*updates)
    with pytest.raises(ValueError) as exc_info:
        _load_temporary_prerequisites(monkeypatch, tmp_path, metadata_text)
    message = str(exc_info.value)
    assert str(tmp_path / "prerequisites.yaml") in message
    assert field in message


@pytest.mark.parametrize("metadata_text", [None, "skills: [\n", "- review\n"])
def test_prerequisite_metadata_read_errors_name_path(
    monkeypatch, tmp_path, metadata_text
):
    with pytest.raises(ValueError) as exc_info:
        _load_temporary_prerequisites(monkeypatch, tmp_path, metadata_text)
    message = str(exc_info.value)
    assert str(tmp_path / "prerequisites.yaml") in message
    assert "metadata" in message

def test_load_phases_from_yaml(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert len(phases) == 2
    assert phases[0].phase_key == "phase_2_autoplan"
    assert phases[0].name == "Phase 2: Autoplan"
    assert phases[0].turns == 20
    assert phases[1].timeout == 1800  # default


def test_load_phase_profile_reads_requires_plan(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text("requires_plan: true\n" + FIXTURE)

    profile = load_phase_profile(p)

    assert profile == PhaseProfile(phases=tuple(load_phases(p)), requires_plan=True)


def test_load_phase_profile_defaults_requires_plan_false(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)

    assert load_phase_profile(p).requires_plan is False


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_load_phase_profile_rejects_non_boolean_requires_plan(tmp_path, value):
    p = tmp_path / "phases.yaml"
    data = yaml.safe_load(FIXTURE)
    data["requires_plan"] = value
    p.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(ValueError, match="requires_plan.*boolean"):
        load_phase_profile(p)


@pytest.mark.parametrize("timeout", ["2400", True, 0, -1])
def test_load_phases_rejects_invalid_timeout(tmp_path, timeout):
    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        yaml.safe_dump(
            {
                "phases": [
                    {
                        "phase_key": "phase_1",
                        "name": "One",
                        "timeout": timeout,
                    }
                ]
            }
        )
    )

    with pytest.raises(
        ValueError,
        match=r"phase_1.*timeout must be a positive integer",
    ):
        load_phases(phases_path)


def test_load_phases_defaults_timeout_to_1800(tmp_path):
    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
    )

    assert load_phases(phases_path)[0].timeout == 1800


def test_gate_phase_needs_no_llm_fields(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(
        """
phases:
  - phase_key: "phase_9_ship"
    name: "Phase 9: Ship Gate"
    gate: true
    terminal: true
"""
    )
    phases = load_phases(p)
    assert len(phases) == 1
    gate = phases[0]
    assert gate.gate is True
    assert gate.terminal is True
    assert gate.prompt == ""
    assert gate.tools == ""
    assert gate.turns == 0

def test_non_gate_phase_defaults_gate_false(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert phases[0].gate is False

def test_real_phases_yaml_ends_with_finish_branch():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert keys[-1] == "phase_8_finish_branch"
    finish = phases[-1]
    assert finish.gate is False
    assert finish.terminal is True


def test_real_phases_yaml_has_review_phase_between_dev_and_cso():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert "phase_5_review" in keys, keys
    dev_i = keys.index("phase_4_development")
    rev_i = keys.index("phase_5_review")
    cso_i = keys.index("phase_6_1_cso")
    assert dev_i < rev_i < cso_i, keys


def test_real_phases_yaml_review_phase_fields():
    phases = {p.phase_key: p for p in load_phases()}
    rev = phases["phase_5_review"]
    assert rev.tools == "Read,Edit,Bash"
    assert rev.turns == 30
    assert rev.timeout == 2400
    assert rev.terminal is False
    assert rev.gate is False
    # Prompt is instruction-only: it must NOT carry rollback control flow.
    assert "reset --hard" not in rev.prompt


def test_real_phases_yaml_development_does_not_block_for_review():
    phases = {p.phase_key: p for p in load_phases()}
    dev = phases["phase_4_development"]
    assert "Review is handled by the next pipeline phase" not in dev.prompt
    assert "request human review" not in dev.prompt
    assert "code review is still required" not in dev.prompt


def test_real_phases_yaml_development_defers_to_sdd_task_loop():
    phases = {p.phase_key: p for p in load_phases()}
    dev = phases["phase_4_development"]
    assert "Follow the subagent-driven-development task loop" in dev.prompt
    assert "short-circuit it with direct inline implementation" in dev.prompt
    assert "implementer subagent per plan task" in dev.prompt
    assert "test-driven-development" not in dev.prompt
    assert "TDD" not in dev.prompt


def test_gstack_phase_prompts_do_not_duplicate_hermes_wrapper():
    phases = load_phases(resolve_profile_phases_path("gstack"))

    for phase in phases:
        assert not phase.prompt.startswith("Hermes phase instructions:\n")
        assert "BEGIN EXTERNAL AGENT PROMPT" not in phase.prompt
        assert "END EXTERNAL AGENT PROMPT" not in phase.prompt
        assert "{agent_product}" not in phase.prompt


def test_real_phases_yaml_order_unchanged_for_existing_phases():
    keys = [p.phase_key for p in load_phases()]
    assert keys == [
        "phase_2_autoplan",
        "phase_3_writing_plan",
        "phase_4_development",
        "phase_5_review",
        "phase_6_1_cso",
        "phase_6_2_qa",
        "phase_7_document_release",
        "phase_8_finish_branch",
    ]


def test_resolve_profile_phases_path_gstack():
    path = resolve_profile_phases_path("gstack")
    assert path.name == "phases.yaml"
    assert "gstack" in str(path)
    assert path.is_file()


def test_resolve_profile_phases_path_unknown_raises_with_available_profiles():
    with pytest.raises(ContractSchemaError, match="gstack"):
        resolve_profile_phases_path("bogus-profile")


def test_load_phases_no_args_still_returns_gstack_phases():
    phases = load_phases()
    assert phases[0].phase_key == "phase_2_autoplan"


def test_real_phases_yaml_records_pipeline_branch_for_pr_handoff():
    phases = {p.phase_key: p for p in load_phases()}
    autoplan = phases["phase_2_autoplan"]
    finish = phases["phase_8_finish_branch"]
    assert ".hermes/pipeline_branch.txt" in autoplan.prompt
    assert ".hermes/pipeline_branch.txt" in finish.prompt
    assert "current branch name" in finish.prompt


def test_real_phases_yaml_finish_branch_uses_ship_skill():
    phases = {p.phase_key: p for p in load_phases()}
    finish = phases["phase_8_finish_branch"]
    assert "{skill_prefix}ship" in finish.prompt
    assert "finishing-a-development-branch" not in finish.prompt
    assert "complete\nthis task normally" in finish.prompt
    assert "Do NOT merge the PR" in finish.prompt
    assert "Do not block this task because a PR is ready" in finish.prompt
    assert "human review" not in finish.prompt.lower()
    assert (
        "Do NOT finish the remaining {skill_prefix}ship steps manually"
        in finish.prompt
    )
    claude_prompt = _render_phase_prompt(
        finish.prompt,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="claude",
    )
    codex_prompt = _render_phase_prompt(
        finish.prompt,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="codex",
    )
    assert "Use the gstack /ship skill." in claude_prompt
    assert "Do NOT finish the remaining /ship steps manually" in claude_prompt
    assert "Use the gstack $ship skill." in codex_prompt
    assert "Do NOT finish the remaining $ship steps manually" in codex_prompt
    assert finish.turns >= 100
    assert finish.timeout >= 7200


AGENT_SKILLS_PHASE_ORDER = [
    "phase_1_spec",
    "phase_1b_spec_gate",
    "phase_2_plan",
    "phase_3_implement",
    "phase_4_review",
    "phase_5_security",
    "phase_6_document_release",
    "phase_7_ship",
    "phase_8_ship",
]


def test_agent_skills_phases_yaml_order():
    phases = load_phases(resolve_profile_phases_path("agent-skills"))
    assert [p.phase_key for p in phases] == AGENT_SKILLS_PHASE_ORDER


def test_agent_skills_phases_yaml_gates():
    phases = {p.phase_key: p for p in load_phases(resolve_profile_phases_path("agent-skills"))}
    assert phases["phase_1b_spec_gate"].gate is True
    assert phases["phase_1b_spec_gate"].terminal is False
    assert phases["phase_8_ship"].gate is True
    assert phases["phase_8_ship"].terminal is True


def test_agent_skills_phases_yaml_non_gate_phases_reference_skills():
    phases = {p.phase_key: p for p in load_phases(resolve_profile_phases_path("agent-skills"))}
    assert "agent-skills:spec-driven-development" in phases["phase_1_spec"].prompt
    assert "agent-skills:planning-and-task-breakdown" in phases["phase_2_plan"].prompt
    assert "agent-skills:incremental-implementation" in phases["phase_3_implement"].prompt
    assert "agent-skills:test-driven-development" in phases["phase_3_implement"].prompt
    assert "agent-skills:code-review-and-quality" in phases["phase_4_review"].prompt
    assert "agent-skills:security-and-hardening" in phases["phase_5_security"].prompt
    assert "agent-skills:ship" in phases["phase_7_ship"].prompt


NATIVE_SDD_PHASE_ORDER = [
    "phase_4_development",
    "phase_5_review",
    "phase_8_finish_branch",
    "phase_9_human_review",
]


def test_native_sdd_profile_contract():
    profile = load_phase_profile(resolve_profile_phases_path("native-sdd"))
    phases = {phase.phase_key: phase for phase in profile.phases}

    assert profile.requires_plan is True
    assert list(phases) == NATIVE_SDD_PHASE_ORDER
    assert (phases["phase_4_development"].turns, phases["phase_4_development"].timeout) == (100, 7200)
    assert (phases["phase_5_review"].turns, phases["phase_5_review"].timeout) == (30, 2400)
    assert (phases["phase_8_finish_branch"].turns, phases["phase_8_finish_branch"].timeout) == (30, 2400)
    gate = phases["phase_9_human_review"]
    assert gate.gate is True
    assert gate.terminal is True


def test_native_sdd_profile_prompts_enforce_plan_tdd_sdd_and_pr_handoff():
    phases = {
        phase.phase_key: phase
        for phase in load_phases(resolve_profile_phases_path("native-sdd"))
    }
    development = phases["phase_4_development"].prompt
    development_lower = development.lower()
    review = phases["phase_5_review"].prompt
    finish = phases["phase_8_finish_branch"].prompt
    combined = "\n".join(phase.prompt for phase in phases.values()).lower()

    for phrase in (
        "{plan_path}",
        "fresh native implementer subagent",
        "red -> green -> refactor",
        "exactly one atomic commit per plan task",
        "do not implement inline",
        ".hermes/pipeline_branch.txt",
    ):
        assert phrase in development_lower
    assert "fresh independent review" in review.lower()
    assert "one review-fix commit" in review
    assert "main...HEAD" in review
    assert ".hermes/pipeline_branch.txt" in finish
    assert "create or update the pull request" in finish.lower()
    assert "do not merge" in finish.lower()
    assert "gstack" not in combined
    assert "superpowers" not in combined
    assert "agent-skills:" not in combined


def test_native_sdd_prerequisites_only_require_hermes_dispatcher_skill():
    metadata = load_profile_prerequisites("native-sdd")

    assert extract_bundled_skill_references(
        "native-sdd", load_phases(resolve_profile_phases_path("native-sdd"))
    ) == {"ai-coding-agents"}
    assert [item.skill_id for item in metadata.skills] == ["ai-coding-agents"]
    skill = metadata.skills[0]
    assert skill.distribution_owner == "hermes"
    assert skill.support == "Conditional"
    assert skill.clients["claude"].invocation == "claude -p"
    assert skill.clients["codex"].invocation == "codex exec"



def test_render_phase_prompt_no_spec_reference_unchanged():
    """Regression guard: omitting spec/reference kwargs must produce
    byte-identical output to pre-TODO-25 behavior."""
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-7", tick_id="01JT", project_slug="demo",
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material:" not in out
    assert out == (
        "Pipeline context:\n"
        "- todo_id: TODO-7\n"
        "- tick_id: 01JT\n"
        "- project_slug: demo\n"
        "Work on TODO-7 ONLY. Do not pick a different TODO.\n\n"
        "do thing"
    )


def test_render_phase_prompt_both_spec_and_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
        reference_paths=["docs/notes/a.md", "docs/notes/b.md"],
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material: docs/notes/a.md, docs/notes/b.md\n" in out


def test_render_phase_prompt_includes_plan_path_and_placeholder():
    out = _render_phase_prompt(
        "Implement {plan_path}",
        todo_id="TODO-25",
        tick_id="01JT",
        project_slug="demo",
        plan_path="docs/plans/TODO-25.md",
    )

    assert "Plan (execution authority): docs/plans/TODO-25.md\n" in out
    assert out.endswith("Implement docs/plans/TODO-25.md")


def test_render_phase_prompt_only_spec():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material:" not in out


def test_render_phase_prompt_only_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=["docs/notes/a.md"],
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material: docs/notes/a.md\n" in out


def test_render_phase_prompt_empty_reference_list_omitted():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=[],
    )
    assert "Reference material:" not in out


@pytest.mark.parametrize(
    ("client", "product", "prefix"),
    [("claude", "Claude Code", "/"), ("codex", "Codex", "$")],
)
def test_render_phase_prompt_all_allowed_fields(client, product, prefix):
    out = _render_phase_prompt(
        "{todo_id}|{tick_id}|{project_slug}|{agent_product}|"
        "{skill_prefix}review|{{literal}}",
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client=client,
        template_source="test-profile:phase_x",
    )
    assert out.endswith(
        f"TODO-41|01CLIENT|demo|{product}|{prefix}review|{{literal}}"
    )


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{unknown}", "unknown"),
        ("{}", "positional"),
        ("{0}", "positional"),
        ("{todo_id", "malformed"),
        ("{agent_product!r}", "conversion"),
        ("{todo_id:}", "format specification"),
        ("{todo_id:>10}", "format specification"),
        ("{todo_id.value}", "traversal"),
        ("{todo_id[0]}", "traversal"),
        ("{todo_id:{tick_id}}", "nested"),
    ],
)
def test_render_phase_prompt_rejects_advanced_formatting(template, message):
    with pytest.raises(
        PhasePromptRenderError,
        match=rf"test-profile:phase_x.*{message}",
    ):
        _render_phase_prompt(
            template,
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            template_source="test-profile:phase_x",
        )


def test_render_phase_prompt_rejects_unknown_client():
    with pytest.raises(
        PhasePromptRenderError,
        match=r"prompt_client.*claude.*codex",
    ):
        _render_phase_prompt(
            "{agent_product}",
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            prompt_client="Claude",
        )


def test_render_phase_prompt_does_not_rewrite_unrelated_text():
    template = "Read docs/a/b.md and https://example.test/a; literal $HOME."
    out = _render_phase_prompt(
        template,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="codex",
    )
    assert out.endswith(template)


@pytest.mark.parametrize("profile", ["gstack", "agent-skills"])
@pytest.mark.parametrize(
    ("client", "product", "forbidden_product"),
    [
        ("claude", "Claude Code", "Codex"),
        ("codex", "Codex", "Claude Code"),
    ],
)
def test_every_bundled_phase_renders_for_client(
    profile, client, product, forbidden_product
):
    prerequisites = load_profile_prerequisites(profile)
    opposite_client = "codex" if client == "claude" else "claude"
    opposite_invocations = {
        item.clients[opposite_client].invocation
        for item in prerequisites.skills
        if item.support == "Conditional"
    }
    assert None not in opposite_invocations
    phases = load_phases(resolve_profile_phases_path(profile))
    for phase in phases:
        rendered = _render_phase_prompt(
            phase.prompt,
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            prompt_client=client,
            template_source=f"{profile}:{phase.phase_key}",
        )
        assert "{agent_product}" not in rendered
        assert "{skill_prefix}" not in rendered
        if "agent_product" in phase.prompt:
            assert product in rendered
            assert forbidden_product not in rendered
        for opposite_invocation in opposite_invocations:
            assert opposite_invocation not in rendered
        for prerequisite in prerequisites.skills:
            invocation = prerequisite.clients[client].invocation
            opposite_invocation = prerequisite.clients[opposite_client].invocation
            if prerequisite.support == "Conditional":
                if prerequisite.distribution_owner == "hermes":
                    continue
                if f"{{skill_prefix}}{prerequisite.skill_id}" not in phase.prompt:
                    continue
                assert invocation is not None
                assert opposite_invocation is not None
                assert invocation in rendered
            else:
                if prerequisite.skill_id not in phase.prompt:
                    continue
                assert prerequisite.support == "Unverified"
                assert invocation is None
                assert opposite_invocation is None
                assert prerequisite.skill_id in rendered
                assert f"/{prerequisite.skill_id}" not in rendered
                assert f"${prerequisite.skill_id}" not in rendered


def test_codex_gstack_profile_uses_namespaced_superpowers_skills():
    phases = {
        phase.phase_key: phase
        for phase in load_phases(resolve_profile_phases_path("gstack"))
    }

    writing_plan = _render_phase_prompt(
        phases["phase_3_writing_plan"].prompt,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="codex",
    )
    development = _render_phase_prompt(
        phases["phase_4_development"].prompt,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="codex",
    )

    assert "$superpowers:writing-plans" in writing_plan
    assert "$superpowers:subagent-driven-development" in development


@pytest.mark.parametrize(
    ("client", "ordinary", "possessive"),
    [
        ("claude", "Use the /autoplan skill in Claude Code.", "Follow /review's fix loop."),
        ("codex", "Use the $autoplan skill in Codex.", "Follow $review's fix loop."),
    ],
)
def test_gstack_exact_client_grammar(client, ordinary, possessive):
    assert ordinary in _render_phase_prompt(
        "Use the {skill_prefix}autoplan skill in {agent_product}.",
        todo_id="TODO-41", tick_id="01CLIENT", project_slug="demo",
        prompt_client=client,
    )
    assert possessive in _render_phase_prompt(
        "Follow {skill_prefix}review's fix loop.",
        todo_id="TODO-41", tick_id="01CLIENT", project_slug="demo",
        prompt_client=client,
    )
