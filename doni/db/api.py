import itertools
import threading
from typing import TYPE_CHECKING

import sqlalchemy as sa
from oslo_db import api as oslo_db_api
from oslo_db import exception as db_exc
from oslo_db.sqlalchemy import enginefacade
from oslo_db.sqlalchemy import utils as db_utils
from oslo_log import log
from oslo_utils import uuidutils
from osprofiler import sqlalchemy as osp_sqlalchemy
from sqlalchemy.orm.exc import NoResultFound

from doni.common import driver_factory, exception
from doni.conf import CONF
from doni.db import models
from doni.worker import WorkerState
from doni.driver.util import generate_k8s_bootstrap_token

if TYPE_CHECKING:
    from typing import ContextManager

    from sqlalchemy.orm.session import Session

LOG = log.getLogger(__name__)

_CONTEXT = threading.local()


def get_instance():
    return Connection()


def _session_for_read() -> "ContextManager[Session]":
    return _wrap_session(enginefacade.reader.using(_CONTEXT))


# Please add @oslo_db_api.retry_on_deadlock decorator to all methods using
# _session_for_write (as deadlocks happen on write), so that oslo_db is able
# to retry in case of deadlocks.
def _session_for_write() -> "ContextManager[Session]":
    return _wrap_session(enginefacade.writer.using(_CONTEXT))


def _wrap_session(session):
    if CONF.profiler.enabled and CONF.profiler.trace_sqlalchemy:
        session = osp_sqlalchemy.wrap_session(sa, session)
    return session


def _paginate_query(
    model, limit=None, marker=None, sort_key=None, sort_dir=None, query=None
):
    if not query:
        query = model_query(model)
    sort_keys = ["id"]
    if sort_key and sort_key not in sort_keys:
        sort_keys.insert(0, sort_key)
    try:
        query = db_utils.paginate_query(
            query, model, limit, sort_keys, marker=marker, sort_dir=sort_dir
        )
    except db_exc.InvalidSortKey:
        raise exception.InvalidParameterValue(
            ('The sort_key value "%(key)s" is an invalid field for sorting')
            % {"key": sort_key}
        )
    return query.all()


def model_query(model, *args, **kwargs):
    """Query helper for simpler session usage.

    Args:
        model (models.Base): The model to query over.
    """
    with _session_for_read() as session:
        query = session.query(model, *args)
        return query


class Connection(object):
    """SqlAlchemy connection."""

    def __init__(self):
        pass

    @oslo_db_api.retry_on_deadlock
    def create_hardware(
        self, values: dict, initial_worker_state=None
    ) -> "models.Hardware":
        if initial_worker_state is not None:
            assert getattr(WorkerState, initial_worker_state, None) is not None

        values.setdefault("uuid", uuidutils.generate_uuid())
        hardware_uuid = values["uuid"]

        # Create one worker for each worker type we have enabled.
        hardware_type = driver_factory.get_hardware_type(values["hardware_type"])
        enabled_worker_types = driver_factory.worker_types()
        worker_tasks = []
        for worker_type in hardware_type.enabled_workers:
            if worker_type not in enabled_worker_types:
                continue
            for field in enabled_worker_types[worker_type].fields:
                # Add in default fields
                if field.default and field.name not in values:
                    values["properties"].setdefault(field.name, field.default)

                # Generating Bootstrap token for K8S authentication
                values["properties"]["k8s_bootstrap_token"] = generate_k8s_bootstrap_token()

                # Add in overrides from hardware type
                if field.name in hardware_type.worker_overrides:
                    values["properties"][field.name] = hardware_type.worker_overrides[
                        field.name
                    ]
            task = models.WorkerTask()
            task.update(
                {
                    "uuid": uuidutils.generate_uuid(),
                    "hardware_uuid": hardware_uuid,
                    "worker_type": worker_type,
                    "state": initial_worker_state or WorkerState.PENDING,
                }
            )
            worker_tasks.append(task)

        hardware = models.Hardware()
        hardware.update(values)

        with _session_for_write() as session:
            try:
                session.add(hardware)
                # Flush the hardware INSERT so that the foreign key constraint
                # for the worker tasks (on hardware UUID) can be satisfied.
                session.flush()
                for task in worker_tasks:
                    session.add(task)
            except db_exc.DBDuplicateEntry as exc:
                if "name" in exc.columns:
                    raise exception.HardwareDuplicateName(name=values["name"])
                raise exception.HardwareAlreadyExists(uuid=values["uuid"])
        return hardware

    @oslo_db_api.retry_on_deadlock
    def backfill_worker_tasks(self) -> "list[models.WorkerTask]":
        hardwares = {hw.uuid: hw for hw in self.get_hardware_list()}
        worker_tasks = self.get_worker_tasks_for_hardware(hardwares.keys())

        enabled_worker_types = driver_factory.worker_types()

        missing_worker_tasks = []
        for hardware_uuid, hw in hardwares.items():
            hardware_type = driver_factory.get_hardware_type(hw.hardware_type)
            existing_worker_types = [
                wt.worker_type for wt in worker_tasks.get(hardware_uuid, [])
            ]

            for worker_type in hardware_type.enabled_workers:
                if worker_type not in enabled_worker_types:
                    continue
                if worker_type in existing_worker_types:
                    # Worker already exists
                    continue
                task = models.WorkerTask()
                task.update(
                    {
                        "uuid": uuidutils.generate_uuid(),
                        "hardware_uuid": hardware_uuid,
                        "worker_type": worker_type,
                        "state": WorkerState.PENDING,
                    }
                )
                missing_worker_tasks.append(task)

        with _session_for_write() as session:
            for task in missing_worker_tasks:
                session.add(task)

        return missing_worker_tasks

    @staticmethod
    def _hardware_by_uuid(session, hardware_uuid: str):
        return session.query(models.Hardware).filter_by(uuid=hardware_uuid, deleted=0)

    @oslo_db_api.retry_on_deadlock
    def update_hardware(self, hardware_uuid: str, values: dict) -> "models.Hardware":
        if "uuid" in values:
            msg = "Cannot overwrite UUID for existing Hardware."
            raise exception.InvalidParameterValue(msg=msg)

        with _session_for_write() as session:
            query = self._hardware_by_uuid(session, hardware_uuid)
            try:
                hardware = query.one()
                hardware_type = driver_factory.get_hardware_type(hardware.hardware_type)
                # Prevent updates to overridden fields
                for field in hardware_type.worker_overrides.keys():
                    del values[field]
                hardware.update(values)
            except NoResultFound:
                raise exception.HardwareNotFound(hardware=hardware_uuid)
            except db_exc.DBDuplicateEntry as exc:
                if "name" in exc.columns:
                    raise exception.HardwareDuplicateName(name=values["name"])
                raise exception.HardwareAlreadyExists(uuid=values["uuid"])
            return query.one()

    @oslo_db_api.retry_on_deadlock
    def destroy_hardware(self, hardware_uuid: str):
        with _session_for_write() as session:
            query = self._hardware_by_uuid(session, hardware_uuid)
            try:
                hardware = query.one()
                hardware.soft_delete(session)
            except NoResultFound:
                raise exception.HardwareNotFound(hardware=hardware_uuid)

    def get_hardware_by_uuid(self, hardware_uuid: str) -> "models.Hardware":
        with _session_for_read() as session:
            query = self._hardware_by_uuid(session, hardware_uuid)
            try:
                return query.one()
            except NoResultFound:
                raise exception.HardwareNotFound(hardware=hardware_uuid)

    def get_hardware_by_name(self, hardware_name: str) -> "models.Hardware":
        query = model_query(models.Hardware).filter_by(name=hardware_name)
        try:
            return query.one()
        except NoResultFound:
            raise exception.HardwareNotFound(hardware=hardware_name)

    def get_hardware_list(
        self,
        limit=None,
        marker=None,
        sort_key=None,
        sort_dir=None,
        project_id=None,
        deleted=False,
    ) -> "list[models.Hardware]":
        query = model_query(models.Hardware)
        if not deleted:
            query = query.filter_by(deleted=0)
        if project_id:
            query = query.filter_by(project_id=project_id)
        return _paginate_query(
            models.Hardware,
            limit,
            marker,
            sort_key,
            sort_dir,
            query=query,
        )

    def get_hardware_availability_window_list(
        self, hardware_uuid: str
    ) -> "list[models.AvailabilityWindow]":
        query = model_query(models.AvailabilityWindow).filter_by(
            hardware_uuid=hardware_uuid
        )
        # TODO: how to communicate that hardware doesn't exist?
        return query.all()

    @oslo_db_api.retry_on_deadlock
    def create_availability_window(self, values: dict) -> "models.AvailabilityWindow":
        if "uuid" not in values:
            values["uuid"] = uuidutils.generate_uuid()

        window = models.AvailabilityWindow()
        window.update(values)

        with _session_for_write() as session:
            try:
                session.add(window)
                session.flush()
            except db_exc.DBDuplicateEntry as exc:
                raise exception.HardwareAlreadyExists(uuid=values["uuid"])
        return window

    @oslo_db_api.retry_on_deadlock
    def update_availability_window(
        self, window_uuid: str, values: dict
    ) -> "models.AvailabilityWindow":
        if "uuid" in values:
            msg = "Cannot overwrite UUID for existing AvailabilityWindow."
            raise exception.InvalidParameterValue(msg=msg)

        with _session_for_write() as session:
            query = session.query(models.AvailabilityWindow).filter_by(uuid=window_uuid)
            count = query.update(values)
            if count != 1:
                raise exception.AvailabilityWindowNotFound(window=window_uuid)
            return query.one()

    @oslo_db_api.retry_on_deadlock
    def destroy_availability_window(self, window_uuid: str):
        with _session_for_write() as session:
            query = session.query(models.AvailabilityWindow).filter_by(uuid=window_uuid)
            try:
                _ = query.one()
            except NoResultFound:
                raise exception.AvailabilityWindowNotFound(window=window_uuid)
            query.delete()

    def get_availability_window_list(self) -> "list[models.AvailabilityWindow]":
        query = model_query(models.AvailabilityWindow)
        return query.all()

    def get_worker_tasks_in_state(
        self, state: "WorkerState"
    ) -> "list[models.WorkerTask]":
        enabled_worker_types = driver_factory.worker_types().keys()
        query = (
            model_query(models.WorkerTask)
            .filter(models.WorkerTask.state == state)
            .filter(models.WorkerTask.worker_type.in_(enabled_worker_types))
        )
        return query.all()

    def get_worker_tasks_for_hardware(
        self, hardware_uuids: "list[str]"
    ) -> "dict[str, list[models.WorkerTask]]":
        enabled_worker_types = driver_factory.worker_types().keys()
        query = (
            model_query(models.WorkerTask)
            .filter(models.WorkerTask.hardware_uuid.in_(hardware_uuids))
            .filter(models.WorkerTask.worker_type.in_(enabled_worker_types))
            .order_by(models.WorkerTask.hardware_uuid)
        )
        # TODO: how to communicate that hardware doesn't exist?
        return {
            hw_uuid: list(wt_iter)
            for hw_uuid, wt_iter in itertools.groupby(
                query.all(), lambda wt: wt.hardware_uuid
            )
        }

    @oslo_db_api.retry_on_deadlock
    def update_worker_task(
        self, worker_task_uuid: str, values: dict
    ) -> "models.WorkerTask":
        if "uuid" in values:
            msg = "Cannot overwrite UUID for existing WorkerTask."
            raise exception.InvalidParameterValue(msg=msg)

        with _session_for_write() as session:
            query = session.query(models.WorkerTask).filter_by(uuid=worker_task_uuid)
            try:
                count = query.update(values)
                if count != 1:
                    raise exception.WorkerTaskNotFound(worker=worker_task_uuid)
            except db_exc.DBDuplicateEntry as exc:
                raise exception.WorkerTaskAlreadyExists(uuid=values["uuid"])
            return query.one()
