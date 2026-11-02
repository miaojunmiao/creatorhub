"""模板生成工具不依赖真实大模型或 API Key。"""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.engine import compose
from app.main import AiCopyIn, generate_ai_copy


def configure(monkeypatch, **overrides):
    settings = {"ai_base_url": "https://model.test/v1", "ai_api_key": "fixture-secret",
                "ai_model": "fixture-model", "ai_temperature": "0.9", "ai_enabled": "0"}
    settings.update(overrides)
    monkeypatch.setattr("app.main.get_setting", lambda key, default="": settings.get(key, default))


def test_manual_generation_works_with_automatic_comments_disabled(monkeypatch):
    configure(monkeypatch)
    generate = AsyncMock(return_value="新的文案 ✨\n第二行 🌿")
    monkeypatch.setattr(compose, "generate_copy", generate)
    result = asyncio.run(generate_ai_copy(AiCopyIn(template="分享 {主题} ✨", instructions="露营")))
    assert result["text"] == "新的文案 ✨\n第二行 🌿"
    assert generate.await_args.args[:4] == ("分享 {主题} ✨", "露营", True, "")


@pytest.mark.parametrize("template", ["", " \n "])
def test_empty_template_is_rejected(template, monkeypatch):
    configure(monkeypatch)
    from pydantic import ValidationError
    with pytest.raises((HTTPException, ValidationError)):
        asyncio.run(generate_ai_copy(AiCopyIn(template=template)))


def test_missing_configuration_does_not_call_provider(monkeypatch):
    configure(monkeypatch, ai_api_key="")
    generate = AsyncMock()
    monkeypatch.setattr(compose, "generate_copy", generate)
    with pytest.raises(HTTPException) as error:
        asyncio.run(generate_ai_copy(AiCopyIn(template="模板")))
    assert error.value.status_code == 400
    generate.assert_not_awaited()


def test_provider_error_does_not_expose_key(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(compose, "generate_copy", AsyncMock(side_effect=RuntimeError("fixture-secret")))
    with pytest.raises(HTTPException) as error:
        asyncio.run(generate_ai_copy(AiCopyIn(template="模板")))
    assert error.value.status_code == 502
    assert "fixture-secret" not in error.value.detail


@pytest.mark.parametrize("emoji", [True, False])
def test_copy_request_preserves_template_refresh_and_long_output(monkeypatch, emoji):
    captured = []
    output = "这是一篇完整文案🌿\n" * 40
    def handle(request):
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": output}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handle), **kwargs))
    template = "分享 {主题}\n① 好物 ✨\n② 使用体验"
    ai = {"base_url": "https://model.test/v1/", "api_key": "fixture-secret", "model": "fixture-model"}
    text = asyncio.run(compose.generate_copy(template, "写秋日露营", emoji, "上一版 🍂", ai))
    assert text == output.strip()
    assert len(text) > 200
    messages = captured[0]["messages"]
    assert template in messages[1]["content"]
    assert "写秋日露营" in messages[1]["content"]
    assert messages[2] == {"role": "assistant", "content": "上一版 🍂"}
    assert "重新生成" in messages[3]["content"]
    assert ("少量 Emoji" if emoji else "不要使用 Emoji") in messages[0]["content"]


def test_empty_model_output_is_error(monkeypatch):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []})), **kwargs))
    with pytest.raises(RuntimeError, match="空文案"):
        asyncio.run(compose.generate_copy("模板", "", True, "", {
            "base_url": "https://model.test/v1", "api_key": "fixture", "model": "fixture"}))
