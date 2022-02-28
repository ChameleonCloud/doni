from typing import TYPE_CHECKING

from doni.db import api as db_api
from doni.objects import base
from doni.objects import fields as object_fields
from doni.worker import WorkerState

if TYPE_CHECKING:
    from doni.common.context import RequestContext


@base.DoniObjectRegistry.register
class WorkerTask(base.DoniObject):
    # Version 1.0: Initial version
    VERSION = "1.0"

    dbapi = db_api.get_instance()

    fields = {
        "id": object_fields.IntegerField(),
        "uuid": object_fields.UUIDField(),
        "hardware_uuid": object_fields.UUIDField(),
        "worker_type": object_fields.StringField(),
        "state": object_fields.WorkerStateField(),
        "state_details": object_fields.FlexibleDictField(),
    }

    @property
    def is_pending(self):
        return self.state == WorkerState.PENDING

    @property
    def is_in_progress(self):
        return self.state == WorkerState.IN_PROGRESS

    def save(self, context: "RequestContext" = None):
        """Save updates to this WorkerTask.

        Column-wise updates will be made based on the result of
        :func:`obj_what_changed`.

        Args:
            context (RequestContext): security context.
        """
        updates = self.obj_get_changes()
        db_worker_task = self.dbapi.update_worker_task(self.uuid, updates)
        # NOTE(jason): We take care to ignore the "state" field from the DB,
        # because the StateMachine oslo_versionedobject mixin does not well
        # handle the case of setting a state to the same value it was at--this
        # is considered invalid.
        self._from_db_object(
            self._context,
            self,
            db_worker_task,
            fields=[f for f in self.fields.keys() if f != "state"],
        )

    @classmethod
    def list_pending(cls, context: "RequestContext") -> "list[WorkerTask]":
        db_workers = cls.dbapi.get_worker_tasks_in_state(WorkerState.PENDING)
        return cls._from_db_object_list(context, db_workers)

    @classmethod
    def list_for_hardware(
        cls, context: "RequestContext", hardware_uuid: str
    ) -> "list[WorkerTask]":
        return cls.list_for_hardwares(context, [hardware_uuid]).get(hardware_uuid, [])

    @classmethod
    def list_for_hardwares(
        cls, context: "RequestContext", hardware_uuids: "list[str]"
    ) -> "dict[str, list[WorkerTask]]":
        return {
            hw_uuid: cls._from_db_object_list(context, db_workers)
            for hw_uuid, db_workers in cls.dbapi.get_worker_tasks_for_hardware(
                hardware_uuids
            ).items()
        }
