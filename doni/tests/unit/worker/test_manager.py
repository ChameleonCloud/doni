import time
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture

from doni.driver.worker.fake import FakeWorker
from doni.objects.worker_task import WorkerTask
from doni.tests.unit import utils
from doni.worker import WorkerResult, WorkerState
from doni.worker.manager import WorkerManager

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.tests.conftest import ConfigFixture


@pytest.fixture
def manager():
    _manager = WorkerManager("fake-host")
    _manager.start()
    return _manager


def test_process_pending(
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    num_hardwares = 10
    for _ in range(num_hardwares):
        database.add_hardware()
    manager.process_pending(admin_context)
    assert len(WorkerTask.list_pending(admin_context)) == 0
    for _, wt in WorkerTask.list_for_hardwares(
        admin_context, [hw["uuid"] for hw in database.hardwares]
    ).items():
        assert wt[0].state == WorkerState.STEADY


def test_process_pending_success(
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    fake_hw = database.add_hardware()
    # Add more items for processing
    for _ in range(9):
        database.add_hardware()
    assert len(WorkerTask.list_pending(admin_context)) == 10

    manager.process_pending(admin_context)

    assert len(WorkerTask.list_pending(admin_context)) == 0
    tasks = WorkerTask.list_for_hardware(admin_context, database.hardwares[0]["uuid"])
    assert len(tasks) == 1
    assert tasks[0].state == WorkerState.STEADY
    assert tasks[0].state_details == {
        "fake-result": fake_hw["uuid"],
        "fake-availability_windows": [],
    }


def test_process_pending_defer(
    mocker: "MockerFixture",
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    def process(context: "RequestContext", hardware, **kwargs):
        return WorkerResult.Defer(reason="fake reason")

    mocker.patch.object(FakeWorker, "process").side_effect = process

    fake_hw = database.add_hardware()

    manager.process_pending(admin_context)

    tasks = WorkerTask.list_for_hardware(admin_context, fake_hw["uuid"])
    assert len(tasks) == 1
    assert tasks[0].state == WorkerState.PENDING
    assert tasks[0].state_details == {"defer_count": 1, "defer_reason": "fake reason"}


def test_process_with_windows(
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    fake_hw = database.add_hardware()
    fake_window = database.add_availability_window(hardware_uuid=fake_hw["uuid"])
    manager.process_pending(admin_context)
    assert len(WorkerTask.list_pending(admin_context)) == 0
    tasks = WorkerTask.list_for_hardware(admin_context, database.hardwares[0]["uuid"])
    assert len(tasks) == 1
    assert tasks[0].state == WorkerState.STEADY
    assert tasks[0].state_details == {
        "fake-result": fake_hw["uuid"],
        "fake-availability_windows": [fake_window["uuid"]],
    }


def test_batching(
    mocker: "MockerFixture",
    test_config: "ConfigFixture",
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    for _ in range(10):
        database.add_hardware()
    test_config.config(group="worker", task_concurrency=1)

    worked_on = []

    def process(context: "RequestContext", hardware, **kwargs):
        counter = 0
        while counter < 10:
            worked_on.append(hardware.uuid)
            counter += 1
            # Cooperative yield
            time.sleep(0)
        return WorkerResult.Success()

    mocker.patch.object(FakeWorker, "process").side_effect = process

    manager.process_pending(admin_context)

    # Check that we only processed one item at a time. Because the
    # task pool size was 1, only one process() would have happened at a time,
    # so the worked_on log should be a series of consecutive entries for a
    # single hardware, e.g.:
    #
    #   AAAA-AAAA...
    #   AAAA-AAAA...
    #   AAAA-AAAA...
    #   BBBB-BBBB...
    #   BBBB-BBBB... (etc.)
    #
    # If batching was not working, we would expect some interleaving:
    #
    #  AAA-AAAA...
    #  CCC-CCCC...
    #  AAA-AAAA...
    #  BBB-BBBB...
    #  CCC-CCCC... (etc.)
    #
    # So, this checks that for each block of work, it's all work for the same
    # piece of hardware.
    for i in range(10):
        assert len(set(worked_on[10 * i : (10 * (i + 1))])) == 1

    assert len(WorkerTask.list_pending(admin_context)) == 0
