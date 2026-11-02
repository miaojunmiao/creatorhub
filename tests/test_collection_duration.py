"""视频时长解析、范围筛选、保存和旧库迁移，全部使用离线样本。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app import db
from app.browser.fetcher import _douyin_search_item_matches
from app.config import Config
from app.engine.collection import KeywordCollector, _collection_content_matches
from app.main import (KeywordCollectionIn, _collection_content_dict,
                      _validated_collection_input, create_keyword_collection,
                      update_keyword_collection)
from app.models import DouyinAccount, KeywordCollectionJob
from app.platforms.douyin.client import DouyinClient
from app.platforms.douyin.extract import Aweme, parse_aweme
from app.platforms.xhs.extract import parse_note_brief, parse_note_detail


def video_item(milliseconds, item_id="video"):
    return {"aweme_id": item_id, "video": {"duration": milliseconds,
            "play_addr": {"url_list": ["https://media.test/video.mp4"]}}}


@pytest.mark.parametrize("duration,kind,expected", [
    (19.999, "video", False), (20, "video", True), (30.5, "video", True),
    (40, "video", True), (40.001, "video", False), (0, "video", False),
    (30, "images", False),
])
def test_inclusive_duration_range(duration, kind, expected):
    job = KeywordCollectionJob(min_duration=20, max_duration=40)
    aweme = Aweme("video", "", 0, "", kind, duration=duration)
    assert _collection_content_matches(aweme, job) is expected
    item = video_item(duration * 1000)
    if kind == "images":
        item["images"] = [{"url_list": ["https://media.test/photo.jpg"]}]
    assert _douyin_search_item_matches(item, min_duration=20, max_duration=40) is expected


@pytest.mark.parametrize("minimum,maximum,duration,expected", [
    (0, 0, 0, True), (20, 0, 90, True), (0, 40, 10, True),
    (0, 40, 0, False), (20, 0, 10, False),
])
def test_optional_and_one_sided_range(minimum, maximum, duration, expected):
    job = KeywordCollectionJob(min_duration=minimum, max_duration=maximum)
    assert _collection_content_matches(Aweme("v", "", 0, "", "video", duration=duration), job) is expected


@pytest.mark.parametrize("changes", [
    {"min_duration": -1}, {"max_duration": 86401},
    {"min_duration": 40, "max_duration": 20},
    {"min_duration": 20, "content_type": "images"},
    {"min_duration": 20, "platform": "kuaishou"},
])
def test_invalid_range_rejected(changes):
    with pytest.raises(HTTPException) as error:
        _validated_collection_input(KeywordCollectionIn(account_id=1, keywords=["露营"], **changes))
    assert error.value.status_code == 400


@pytest.mark.parametrize("value", [1.5, True, "20"])
def test_range_requires_integer_seconds(value):
    with pytest.raises(ValidationError):
        KeywordCollectionIn(account_id=1, min_duration=value)


def test_douyin_keeps_millisecond_precision():
    assert parse_aweme(video_item(40001)).duration == 40.001


@pytest.mark.parametrize("video,seconds", [
    ({"media": {"basic": {"duration": 30.25}}}, 30.25),
    ({"capa": {"duration": 25}}, 25),
    ({"consumer": {"duration": "24.5"}}, 24.5),
    ({"media": {"stream": {"h264": [{"duration": 40001}]}}}, 40.001),
    ({"capa": {"duration": 40}, "media": {"stream": {"h264": [{"duration": 40001}]}}}, 40.001),
    ({"media": {"stream": {"EF4": {"duration": "30000"}}}}, 30),
    ({"media": {"basic": {"duration": "NaN"}, "stream": {"h264": [{"duration": 23000}]}}}, 23),
    ({"media": {"basic": {"duration": "bad"}}}, 0),
    ({}, 0),
])
def test_xhs_seconds_and_milliseconds(video, seconds):
    card = {"id": "note", "type": "video", "video": video}
    brief = parse_note_brief({"id": "note", "note_card": card})
    assert brief["duration"] == seconds
    assert parse_note_detail(card).duration == seconds
    # 搜索卡片有时长、详情没有时长时，不应丢失已知值。
    assert parse_note_detail({"id": "note", "type": "video"}, brief).duration == seconds


def test_douyin_filters_before_result_limit():
    client = DouyinClient.__new__(DouyinClient)
    client.search_awemes_page = AsyncMock(side_effect=[
        {"items": [video_item(10000, "short")], "has_more": True, "offset": 1},
        {"items": [video_item(30000, "keep")], "has_more": False, "offset": 2},
    ])
    rows, error = asyncio.run(client.search_awemes("露营", max_results=1,
        min_duration=20, max_duration=40, max_pages=2))
    assert [row["aweme_id"] for row in rows] == ["keep"]
    assert not error
    assert client.search_awemes_page.await_count == 2


def test_xhs_duration_filter_discovers_beyond_result_limit():
    client = SimpleNamespace(search_notes=AsyncMock(side_effect=[
        [{"id": "short", "type": "video"}], [{"id": "keep", "type": "video"}],
    ]))
    job = KeywordCollectionJob(account_id=1, platform="xhs", max_pages_per_keyword=2,
        max_contents_per_keyword=1, min_duration=20, max_duration=40)
    collector = KeywordCollector(Config(), SimpleNamespace(), SimpleNamespace())
    collector._xhs_gap = AsyncMock()
    rows, error = asyncio.run(collector._discover_xhs(client, "露营", job))
    assert [row["id"] for row in rows] == ["short", "keep"]
    assert not error


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    db.init_db(str(tmp_path / "duration.db"))
    yield
    db._engine.dispose()


@pytest.mark.parametrize("platform", ["douyin", "xhs"])
def test_create_edit_and_persist_duration(isolated_database, monkeypatch, platform):
    monkeypatch.setattr("app.main.engine", None)
    with db.get_session() as session:
        account = DouyinAccount(platform=platform, status="active", storage_state='{"cookies": []}')
        session.add(account); session.commit(); session.refresh(account)
        account_id = account.id
    body = KeywordCollectionIn(platform=platform, account_id=account_id,
        keywords=["露营"], min_duration=20, max_duration=40)
    created = asyncio.run(create_keyword_collection(body))
    assert (created["min_duration"], created["max_duration"]) == (20, 40)
    with db.get_session() as session:
        job = session.get(KeywordCollectionJob, created["id"])
        job.status = "done"
        session.add(job); session.commit(); session.refresh(job)
    body.min_duration, body.max_duration = 25, 35
    edited = asyncio.run(update_keyword_collection(job.id, body))
    assert (edited["min_duration"], edited["max_duration"]) == (25, 35)
    row = KeywordCollector._upsert_content(job, "露营",
        Aweme("video", "测试", 0, "作者", "video", duration=30.125), "", "", "", "")
    assert _collection_content_dict(row)["duration"] == 30.125


def test_existing_database_adds_zero_defaults(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE keywordcollectionjob (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO keywordcollectionjob (id) VALUES (1)"))
        connection.execute(text("CREATE TABLE keywordcollectioncontent (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO keywordcollectioncontent (id) VALUES (1)"))
    db._auto_migrate(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT min_duration, max_duration FROM keywordcollectionjob")).one() == (0, 0)
        assert connection.execute(text("SELECT duration FROM keywordcollectioncontent")).scalar() == 0
    engine.dispose()
