from agent_board.git_context import (
    append_description_git_context,
    direct_git_contract,
    prompt_git_rules,
)


def test_agent_board_direct_git_contract_routes_copy_first():
    project = "/opt/scripts/home/seoadvanced"

    assert direct_git_contract(project, "починить content?section=accounts", "") == "content-redactor"
    assert direct_git_contract(project, "починить content?section=copy", "") == "copy"
    assert direct_git_contract(project, "правка /direct/automation/accounts", "") == "content-redactor"
    assert direct_git_contract(project, "обычная задача", "") == ""

    mixed = "починить content?section=copy и /direct/automation/accounts"
    assert direct_git_contract(project, mixed, "") == "copy"


def test_agent_board_git_context_visible_in_description_and_prompt():
    content_desc = append_description_git_context("Описание", "content-redactor")
    copy_desc = append_description_git_context("Описание", "copy")

    assert "Git-контекст:" in content_desc
    assert "yandex_direct_content_redactor" in content_desc
    assert "Не трогать copy-git" in content_desc
    assert "ydirect_automation_copy_auto_ak_ak" in copy_desc
    assert "Не трогать content/accounts git" in copy_desc

    assert "content_redactor_git.py preflight" in prompt_git_rules("content-redactor")
    assert "direct_git_guard.py --branch ydirect_automation_copy_auto_ak_ak" in prompt_git_rules("copy")
