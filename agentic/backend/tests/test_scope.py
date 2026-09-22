"""The agent's scope rule: it plans Multistack deployments and refuses
everything else. Whether the model obeys is a property of the live model
and is checked against it, not here -- these pin the part code controls,
that every conversation actually carries the rule."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from orchestration.graph import _seed_messages
from orchestration.knowledge import BASE_INSTRUCTIONS, OUT_OF_SCOPE_REPLY, build_system_prompt, load_skills


def test_the_scope_rule_leads_the_base_instructions():
    """First, so the model reads it before any of the planning rules."""
    first_paragraph = BASE_INSTRUCTIONS.split("\n\n", 1)[0]
    assert first_paragraph.startswith("SCOPE:")
    assert OUT_OF_SCOPE_REPLY in first_paragraph


def test_every_new_conversation_carries_the_scope_rule():
    for name, _ in load_skills():
        assert build_system_prompt([name]).startswith(BASE_INSTRUCTIONS)
    assert build_system_prompt([]).startswith(BASE_INSTRUCTIONS)


def test_a_new_thread_is_seeded_with_the_scope_rule():
    system, human = _seed_messages("who is the president of india", [], [], new_thread=True)
    assert OUT_OF_SCOPE_REPLY in system.content
    assert human.content == "who is the president of india"


def test_a_question_naming_a_component_is_marked_in_scope():
    """The scope rule alone had the model refuse "what is rke2 cluster" as
    general knowledge. The note comes from the skill triggers, not the
    model's judgment, and rides in the user's own message: gpt-oss on vLLM
    largely ignored it as a separate system message."""
    human = _seed_messages("what is rke2 cluster", ["rke2-cluster"], [], new_thread=True)[-1]
    assert isinstance(human, HumanMessage)
    assert "rke2-cluster" in human.content and "in scope" in human.content
    assert human.content.endswith("\n\nwhat is rke2 cluster")


def test_the_note_is_added_on_later_turns_too():
    messages = _seed_messages("what does valkey do", ["valkey"], ["valkey"], new_thread=False)
    assert [type(m).__name__ for m in messages] == ["HumanMessage"]
    assert "(valkey)" in messages[0].content


def test_no_note_for_a_message_naming_no_component():
    for message in ("who is the president of india", "write python code for adding two numbers"):
        human = _seed_messages(message, [], [], new_thread=True)[-1]
        assert human.content == message


def test_a_skill_added_mid_conversation_does_not_resend_the_base_prompt():
    """The thread's first system message already has it. This used to
    split off only the first paragraph, re-sending most of the base
    prompt each time a new skill joined."""
    system = _seed_messages("now add a database", ["valkey", "cnpg"], ["valkey"], new_thread=False)[0]
    assert system.content.startswith("--- cnpg ---")
    assert "--- valkey ---" not in system.content
    assert BASE_INSTRUCTIONS.split("\n\n")[1] not in system.content


def test_the_target_cluster_is_restated_on_later_turns():
    """Named once, the kubeconfig must not depend on the model remembering it."""
    human = _seed_messages(
        "postgres org-db, password s3cret", ["cnpg"], ["cnpg"], new_thread=False,
        target="the existing cluster at kubeconfig_path='/tmp/kubeconfig'",
    )[-1]
    assert "kubeconfig_path='/tmp/kubeconfig'" in human.content
    assert human.content.endswith("postgres org-db, password s3cret")


def test_the_target_is_read_from_the_models_own_tool_calls():
    from orchestration.graph import _target_from

    assert _target_from("build_full_stack_plan", {"kubeconfig_path": "/tmp/kc", "tokenizer": {"kubeconfig_path": "wired"}}) \
        == "the existing cluster at kubeconfig_path='/tmp/kc'"
    assert _target_from("build_full_stack_plan", {"cluster": {"name": "demo"}}) == "a new RKE2 cluster named 'demo'"
    # Nested values in the whole-stack tool are placeholders, never a target.
    assert _target_from("build_full_stack_plan", {"tokenizer": {"kubeconfig_path": "wired"}}) is None
    assert _target_from("build_valkey_plan", {"valkey": {"kubeconfig_path": "/k"}}) \
        == "the existing cluster at kubeconfig_path='/k'"
