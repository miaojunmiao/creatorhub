"""采集预览的长按、拖选与复制回归，仅访问本地静态测试页。"""
import os
import shutil
import subprocess

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def prepare_preview(page, platform="douyin", open_preview=True):
    # 本地合成短视频，无平台账号或外部网络依赖。
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("preview playback acceptance requires local ffmpeg")
    video_data = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x407060:s=320x180:r=10", "-t", "1", "-c:v", "libvpx",
        "-f", "webm", "pipe:1"], capture_output=True, check=True).stdout
    page.route("**/fixture-preview.webm", lambda route: route.fulfill(
        content_type="video/webm", body=video_data))
    page.locator("html").evaluate("""(el, platform) => {
      el.dataset.fixturePreview = 'true'; el.dataset.fixturePreviewVideoUrl = '/fixture-preview.webm';
      el.dataset.fixturePreviewPlatform = platform;
    }""", platform)
    page.locator(f'.pswitch [data-pf="{platform}"]').click()
    navigate(page, "collections", mobile=page.viewport_size["width"] <= 860)
    page.locator("#collection-job-table").get_by_role("button", name="查看结果", exact=True).click()
    if not open_preview:
        return
    page.locator("#collection-content-list").get_by_role("button", name="预览", exact=True).click()
    expect(page.locator("#pv-cap")).to_contain_text("可复制的采集文案")
    expect(page.locator("#pv-media video")).to_be_visible()
    page.locator("#pv-media video").evaluate("""video => {
      video.dataset.originalPreviewVideo = 'true';
      video.pause(); video.currentTime = 0;
    }""")


@pytest.mark.parametrize("platform,width", [("douyin", 1440), ("xhs", 1440), ("douyin", 375), ("xhs", 375)])
def test_workbench_preview_caption_hold_and_drag_outside(ui, platform, width):
    page, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    prepare_preview(page, platform)
    caption = page.locator("#pv-cap")
    box = caption.bounding_box()
    page.mouse.move(box["x"] + 22, box["y"] + 24)
    page.mouse.down()
    page.wait_for_timeout(1000)
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "1")
    expect(page.locator('#pv-media video[data-original-preview-video="true"]')).to_have_count(1)
    # 拖出文案区域再松开：浏览器会把 click 的目标定为共同祖先（遮罩）。
    page.mouse.move(box["x"] + box["width"] - 24, box["y"] + 24, steps=12)
    assert page.evaluate("!!window.getSelection().toString()")
    page.mouse.move(12, 12, steps=12)
    page.mouse.up()
    expect(page.locator("#preview")).to_be_visible()
    expect(page.locator('#pv-media video[data-original-preview-video="true"]')).to_have_count(1)
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "1")
    page.locator("#preview").click(position={"x": 5, "y": 5})
    expect(page.locator("#preview")).to_be_hidden()
    expect(page.locator("#pv-media video")).to_have_count(0)
    assert errors == []


def test_workbench_preview_copy_keeps_video_and_clipboard(ui):
    page, errors = ui
    prepare_preview(page)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    caption = page.locator("#pv-cap").inner_text()
    button = page.locator("#pv-copy")
    box = button.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down(); page.wait_for_timeout(800); page.mouse.up()
    expect(page.locator("#pv-copy-msg")).to_contain_text("文案已复制")
    # Windows 剪贴板会把换行规范化为 CRLF，内容和 Emoji 应保持一致。
    assert page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n") == caption
    expect(page.locator('#pv-media video[data-original-preview-video="true"]')).to_have_count(1)
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "1")
    assert len(page.context.pages) == 1
    page.keyboard.press("Escape")
    expect(page.locator("#preview")).to_be_hidden()
    assert errors == []


def test_workbench_preview_repeated_open_does_not_reload(ui):
    page, errors = ui
    prepare_preview(page, open_preview=False)
    page.locator("html").evaluate("el => el.dataset.fixturePreviewSlow = 'true'")
    button = page.locator(".collection-preview-primary")
    # 覆盖正在加载期间的重复触发，不只是浏览器原生按钮的 disabled 效果。
    button.evaluate("el => { for (let i=0;i<5;i++) el.click(); }")
    expect(page.locator("#pv-media video")).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "1")
    page.locator("#pv-media video").evaluate("video => { video.dataset.originalPreviewVideo = 'true'; video.pause(); }")
    button.evaluate("el => { for (let i=0;i<5;i++) el.click(); }")
    page.wait_for_timeout(700)
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "1")
    expect(page.locator('#pv-media video[data-original-preview-video="true"]')).to_have_count(1)
    page.get_by_role("button", name="关闭预览", exact=True).click()
    button.click()
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "2")
    expect(page.locator("#pv-media video")).to_be_visible()
    assert errors == []


def test_workbench_preview_failure_can_retry(ui):
    page, errors = ui
    prepare_preview(page, open_preview=False)
    page.locator("html").evaluate("el => el.dataset.fixturePreviewFail = 'true'")
    button = page.locator(".collection-preview-primary")
    button.click()
    expect(page.locator("#pv-media")).to_contain_text("预览失败")
    page.locator("html").evaluate("el => delete el.dataset.fixturePreviewFail")
    button.evaluate("el => el.click()")
    expect(page.locator("#pv-media video")).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-fixture-preview-requests", "2")
    assert errors == []
