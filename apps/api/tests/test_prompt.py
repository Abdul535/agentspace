from agentspace.agent.prompt import build_system_prompt
from agentspace.tools import TOOL_SETS, build_tools


def test_prompt_lists_the_tools_and_their_rules_once() -> None:
    prompt = build_system_prompt("  You help.\n", build_tools(TOOL_SETS["coding"], bash=True))
    assert prompt.startswith("You help.\n\n<tools>\n- read: Read file contents\n- bash: Execute bash")
    assert "- write: Create or overwrite files\n</tools>" in prompt
    rules = prompt.split("<rules>\n")[1].split("\n</rules>")[0].splitlines()
    assert rules[0] == "- Use bash for file operations like ls, rg, find"
    assert "- Use edit for targeted changes. Batch changes to one file in a single call." in rules
    assert rules[-2:] == ["- Be concise in your responses", "- Show file paths clearly when working with files"]
    assert len(rules) == len(set(rules))
    assert prompt.endswith("is refused.\n</workspace>")


def test_bash_hint_only_without_the_file_tools() -> None:
    with_search = build_system_prompt("p", build_tools(TOOL_SETS["all"], bash=True))
    assert "Use bash for file operations" not in with_search
    read_only = build_system_prompt("p", build_tools(TOOL_SETS["read_only"], bash=True))
    assert "bash" not in read_only and "- grep: Search file contents" in read_only


def test_no_tools() -> None:
    assert "<tools>\n(none)\n</tools>" in build_system_prompt("p", [])
