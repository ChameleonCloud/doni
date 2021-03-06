import pytest
from pytest_mock import MockerFixture

from doni.objects.worker_task import WorkerTask
from doni.tests.unit import utils
from doni.worker import BaseWorker, WorkerResult, WorkerState
from doni.worker.manager import WorkerManager

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doni.common.context import RequestContext


@pytest.fixture
def manager():
    _manager = WorkerManager("fake-host")
    _manager.start()
    return _manager


def worker_that_returns(ret):
    class WorkerThatReturns(BaseWorker):
        def process(self):
            return ret

    return WorkerThatReturns


def test_process_pending(
    mocker: "MockerFixture",
    manager: "WorkerManager",
    admin_context: "RequestContext",
    database: "utils.DBFixtures",
):
    process_task = mocker.patch.object(manager, "_process_task")
    num_hardwares = 10
    for _ in range(num_hardwares):
        database.add_hardware()
    manager.process_pending(admin_context)
    assert process_task.call_count == num_hardwares


def test_process_pending_success(
    mocker: "MockerFixture",
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


def test_process_with_windows(
    mocker: "MockerFixture",
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
