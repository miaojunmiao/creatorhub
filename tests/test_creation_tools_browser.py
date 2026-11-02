"""本地离线演示页验收，不连接平台或真实大模型。"""
import json
import os

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate, assert_no_overflow, capture

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


@pytest.mark.parametrize("width", [1440, 375])
def test_workbench_ai_template_generate_refresh_and_error(ui, width):
    page, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    mobile = width <= 860
    navigate(page, "settings", mobile=mobile)
    expect(page.get_by_role("tab", name="AI 文案", exact=True)).to_have_count(0)
    navigate(page, "ai-copy", mobile=mobile)
    expect(page.locator("#ai-copy-template")).to_be_visible()
    expect(page.locator("#ai-copy-refresh")).to_be_disabled()
    page.locator("#ai-copy-generate").click()
    expect(page.locator("#ai-copy-template-error")).to_be_visible()
    # 使用真实的自定义下拉交互加载内置模板。
    page.locator("#ai-copy-preset").locator("..").locator(".cs-trg").click()
    page.get_by_role("option", name="好物分享", exact=True).click()
    assert "✨" in page.locator("#ai-copy-template").input_value()
    template = "分享 {主题} ✨\n① 好物\n② 体验"
    page.locator("#ai-copy-template").fill(template)
    page.locator("#ai-copy-instructions").fill("秋日露营，200 字")
    page.locator("#ai-copy-generate").click()
    expect(page.locator("#ai-copy-generate")).to_be_disabled()
    expect(page.locator("#ai-copy-result")).to_have_value("第 1 版新文案 ✨\n秋日露营 🌿\n<script>这只是文案文本</script>")
    first = page.locator("#ai-copy-result").input_value()
    body = json.loads(page.locator("html").get_attribute("data-fixture-ai-body"))
    assert body == {"template": template, "instructions": "秋日露营，200 字", "allow_emoji": True, "previous_text": ""}
    page.locator("#ai-copy-emoji").uncheck()
    page.locator("#ai-copy-refresh").click()
    expect(page.locator("#ai-copy-result")).to_have_value(first.replace("第 1 版", "第 2 版"))
    body = json.loads(page.locator("html").get_attribute("data-fixture-ai-body"))
    assert body["previous_text"] == first
    assert body["allow_emoji"] is False
    page.locator("#ai-copy-result").fill("编辑后的文案 💬")
    page.locator("html").evaluate("el => el.dataset.fixtureAiFail = 'true'")
    page.locator("#ai-copy-refresh").click()
    expect(page.locator("#ai-copy-msg")).to_contain_text("上一版文案已保留")
    expect(page.locator("#ai-copy-result")).to_have_value("编辑后的文案 💬")
    expect(page.locator("#ai-copy-refresh")).to_be_enabled()
    page.get_by_role("tab", name="模型配置", exact=True).click()
    expect(page.locator("#ai-key")).to_be_visible()
    page.get_by_role("tab", name="生成文案", exact=True).click()
    expect(page.locator("#ai-copy-template")).to_have_value(template)
    assert_no_overflow(page, f"ai-copy-{width}")
    capture(page, f"ai-copy-{width}")
    assert errors == []


@pytest.mark.parametrize("platform", ["douyin", "xhs"])
@pytest.mark.parametrize("width", [1440, 375])
def test_workbench_duration_range_creation(ui, platform, width):
    page, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator(f'.pswitch [data-pf="{platform}"]').click()
    navigate(page, "collections", mobile=width <= 860)
    page.locator("#wb-create").click()
    expect(page.locator("#col-duration-wrap")).to_be_visible()
    page.locator("#col-keywords").fill("露营")
    page.locator("#col-min-duration").fill("40")
    page.locator("#col-max-duration").fill("20")
    page.get_by_role("button", name="开始批量采集", exact=True).click()
    expect(page.locator("#col-min-duration-error")).to_contain_text("下限不能大于上限")
    assert page.locator("html").get_attribute("data-fixture-collection-body") is None
    page.locator("#col-min-duration").fill("20")
    page.locator("#col-max-duration").fill("40")
    page.locator("#col-duration-wrap").scroll_into_view_if_needed()
    assert_no_overflow(page, f"duration-form-{platform}-{width}")
    capture(page, f"duration-form-{platform}-{width}")
    page.get_by_role("button", name="开始批量采集", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-fixture-write-count", "1")
    body = json.loads(page.locator("html").get_attribute("data-fixture-collection-body"))
    assert body["platform"] == platform
    assert (body["min_duration"], body["max_duration"]) == (20, 40)
    expect(page.locator("#collection-job-table")).to_contain_text("视频 20～40 秒")
    assert errors == []


def test_workbench_duration_edit_keeps_range(ui):
    page, errors = ui
    page.locator("html").evaluate("el => el.dataset.fixtureEditors = 'true'")
    navigate(page, "collections")
    page.locator('#collection-job-table button').filter(has_text="编辑").first.click()
    expect(page.locator("#ecol-min-duration")).to_have_value("0")
    page.locator("#ecol-min-duration").fill("20")
    page.locator("#ecol-max-duration").fill("40")
    page.locator("#ui-ok").click()
    expect(page.locator("html")).to_have_attribute("data-fixture-edit-count", "1")
    body = json.loads(page.locator("html").get_attribute("data-fixture-edit-body"))
    assert (body["min_duration"], body["max_duration"]) == (20, 40)
    expect(page.locator("#uimodal")).to_be_hidden()
    page.locator('#collection-job-table button').filter(has_text="编辑").first.click()
    expect(page.locator("#ecol-min-duration")).to_have_value("20")
    expect(page.locator("#ecol-max-duration")).to_have_value("40")
    assert errors == []
