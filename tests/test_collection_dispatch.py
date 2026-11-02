"""采集队列在账号占用和后台维护阻塞时仍能继续调度。"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import app.db as db
from app.main import _collection_job_dict
from app.engine.monitor import MonitorEngine
from app.models import AccountRiskState, DouyinAccount, KeywordCollectionJob
from app.risk import OperationKind
from test_project_optimizations import local_project, store


def collection(account_id, platform="xhs"):
    return store(KeywordCollectionJob(
        platform=platform, account_id=account_id, keywords='["fixture"]'))


def account(platform="xhs"):
    return store(DouyinAccount(platform=platform, status="active", storage_state="{}"))


def test_busy_account_does_not_take_the_collection_slot(local_project, monkeypatch):
    busy, ready = account(), account()
    held_job, ready_job = collection(busy), collection(ready)
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    run = AsyncMock(return_value={})
    monkeypatch.setattr(engine.keyword_collector, "run", run)

    async def scenario():
        async with local_project.browser.lock_for(f"acc:{busy}"):
            result = await asyncio.wait_for(engine.run_collection_job(held_job), 1)
            assert result["deferred"] and result["signal"] == "account_busy"
            await engine._process_collection_jobs()
            await asyncio.wait_for(asyncio.gather(*engine._collection_tasks.values()), 1)
        run.assert_awaited_once()
        assert run.call_args.args[0] == ready_job
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, held_job)
            assert job.status == "pending"
            assert job.blocked_signal == "account_busy"
            assert "占用" in job.current_step
            assert session.get(KeywordCollectionJob, ready_job).status == "done"
            assert session.get(AccountRiskState, busy).risk_level == 0
        result = await engine.run_collection_job(held_job)
        assert result["status"] == "done"

    asyncio.run(scenario())


def test_scheduler_records_the_actual_pacing_hold(local_project):
    aid = account()
    job_id = collection(aid)
    deadline = datetime.utcnow() + timedelta(seconds=30)
    with db.get_session() as session:
        session.add(AccountRiskState(account_id=aid, operation_not_before=deadline))
        session.commit()
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    asyncio.run(engine._process_collection_jobs())
    with db.get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        assert job.status == "pending"
        assert job.blocked_signal == "operation_pacing"
        assert job.next_allowed_at == deadline
        assert "节奏" in job.current_step
        payload = _collection_job_dict(job)
        assert payload["blocked_signal"] == "operation_pacing"
        assert payload["blocked_reason"] == job.blocked_reason
        assert payload["next_allowed_at"] == deadline.isoformat() + "Z"


@pytest.mark.parametrize("maintenance_fails", [False, True])
def test_collection_dispatch_does_not_depend_on_monitor_loop(
        local_project, monkeypatch, maintenance_fails):
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    aid = account()
    job_id = collection(aid)
    run = AsyncMock(return_value={})
    monkeypatch.setattr(engine.keyword_collector, "run", run)

    async def scenario():
        blocked = asyncio.Event()

        async def scan():
            if maintenance_fails:
                raise RuntimeError("unrelated monitor failure")
            await blocked.wait()

        monkeypatch.setattr(engine, "_scan_once", scan)
        # A previously deferred job must wake at its own deadline, even while
        # the monitor loop is blocked or has aborted the current tick.
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            job.next_allowed_at = datetime.utcnow() + timedelta(milliseconds=100)
            session.add(job)
            session.commit()
        engine.start()
        try:
            async def completed():
                while True:
                    with db.get_session() as session:
                        if session.get(KeywordCollectionJob, job_id).status == "done":
                            return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(completed(), 2)
            run.assert_awaited_once()
        finally:
            await engine.stop()

    asyncio.run(scenario())


def test_finished_collection_wakes_the_next_job_without_waiting_for_a_tick(
        local_project, monkeypatch):
    first, second = collection(account()), collection(account())
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    async def scenario():
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(job_id, _account):
            if job_id == first:
                started.set()
                await release.wait()
            else:
                assert job_id == second
                completed.set()
            return {}

        monkeypatch.setattr(engine.keyword_collector, "run", run)
        monkeypatch.setattr(engine, "_loop", AsyncMock())
        engine.start()
        try:
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.gather(*engine._collection_tasks.values())
            with db.get_session() as session:
                assert session.get(KeywordCollectionJob, first).status == "done"
                assert session.get(KeywordCollectionJob, second).status == "done"
        finally:
            await engine.stop()
        assert not engine._collection_tasks
        assert engine._collection_dispatch_task is None

    asyncio.run(scenario())


def test_waiting_account_does_not_hold_the_shared_network_slot(local_project):
    busy, ready = account(), account()
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    async def scenario():
        async with local_project.browser.lock_for(f"acc:{busy}"):
            async def operation(aid):
                async with engine.operation_guard(aid, OperationKind.READ_HEAVY):
                    return aid
            waiting = asyncio.create_task(operation(busy))
            await asyncio.sleep(0)
            try:
                assert await asyncio.wait_for(operation(ready), 1) == ready
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", ["execution", "network"])
def test_collection_defers_when_shared_capacity_is_busy(local_project, monkeypatch, capacity):
    local_project.cfg.engine.active_accounts = 1 if capacity == "execution" else 2
    local_project.cfg.engine.douyin_read_mode = "browser"
    holder, aid = account(), account("douyin")
    job_id = collection(aid, "douyin")
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    run = AsyncMock(return_value={})
    monkeypatch.setattr(engine.keyword_collector, "run", run)

    async def scenario():
        async with engine.operation_guard(holder, OperationKind.READ_LIGHT):
            result = await asyncio.wait_for(engine.run_collection_job(job_id), 1)
            assert result["deferred"]
            assert result["signal"] == f"{capacity}_busy"
            assert not local_project.browser.lock_for(f"acc:{aid}").locked()
            run.assert_not_awaited()
            with db.get_session() as session:
                job = session.get(KeywordCollectionJob, job_id)
                assert job.status == "pending"
                assert job.blocked_signal == f"{capacity}_busy"
                assert job.next_allowed_at > datetime.utcnow()
                state = session.get(AccountRiskState, aid)
                assert state.risk_level == 0
                assert state.consecutive_network_failures == 0

    asyncio.run(scenario())


def test_busy_network_does_not_block_collection_on_another_exit(local_project, monkeypatch):
    local_project.cfg.engine.active_accounts = 3
    local_project.cfg.engine.douyin_read_mode = "browser"
    holder, busy, ready = account(), account("douyin"), account("douyin")
    with db.get_session() as session:
        row = session.get(DouyinAccount, ready)
        row.proxy = "http://127.0.0.1:19080"
        session.add(row)
        session.commit()
    held_job, ready_job = collection(busy, "douyin"), collection(ready, "douyin")
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    run = AsyncMock(return_value={})
    monkeypatch.setattr(engine.keyword_collector, "run", run)

    async def scenario():
        async with engine.operation_guard(holder, OperationKind.READ_LIGHT):
            await engine._process_collection_jobs()
            await asyncio.wait_for(asyncio.gather(*engine._collection_tasks.values()), 1)
            await engine._process_collection_jobs()
            await asyncio.wait_for(asyncio.gather(*engine._collection_tasks.values()), 1)
        run.assert_awaited_once()
        assert run.call_args.args[0] == ready_job
        with db.get_session() as session:
            assert session.get(KeywordCollectionJob, held_job).status == "pending"
            assert session.get(KeywordCollectionJob, ready_job).status == "done"

    asyncio.run(scenario())
