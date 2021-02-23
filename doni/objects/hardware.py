from oslo_versionedobjects import fields as ovo_fields

from doni.db import api as db_api
from doni.objects import base
from doni.objects import fields as object_fields

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from doni.common.context import RequestContext

@base.DoniObjectRegistry.register
class Hardware(base.DoniObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    dbapi = db_api.get_instance()

    fields = {
        'id': object_fields.IntegerField(),
        'uuid': object_fields.UUIDField(nullable=False),
        'hardware_type': object_fields.StringField(nullable=False),
        'project_id': object_fields.StringField(nullable=False),
        'name': object_fields.StringField(nullable=False),
        'workers': ovo_fields.ListOfObjectsField('Worker', nullable=False),
    }

    def create(self, context: "RequestContext"=None):
        """Create a Hardware record in the DB.

        Args:
            context (RequestContext): security context.

        Raises:
            HardwareDuplicateName: if a hardware with the same name exists.
            HardwareAlreadyExists: if a hardware with the same UUID exists.
        """
        values = self.obj_get_changes()
        db_hardware = self.dbapi.create_hardware(values)
        self._from_db_object(self._context, self, db_hardware)

    def save(self, context: "RequestContext"=None):
        """Save updates to this Hardware.

        Column-wise updates will be made based on the result of
        :func:`what_changed`.

        Args:
            context (RequestContext): security context.

        Raises:
            HardwareDuplicateName: if a hardware with the same name exists.
            HardwareNotFound: if the hardware does not exist.
        """
        updates = self.obj_get_changes()
        db_hardware = self.dbapi.update_hardware(self.uuid, updates)
        self._from_db_object(self._context, self, db_hardware)

    def destroy(self):
        """Delete the Hardware from the DB.

        Args:
            context (RequestContext): security context.

        Raises:
            HardwareNotFound: if the hardware no longer appears in the database.
        """
        self.dbapi.destroy_hardware(self.uuid)
        self.obj_reset_changes()

    @classmethod
    def get_by_id(cls, context: "RequestContext", hardware_id: int) -> "Hardware":
        """Find a hardware based on its integer ID.

        Args:
            context (RequestContext): security context.
            hardware_id (int): The ID of a hardware.

        Returns:
            A :class:`Hardware` object.

        Raises:
            HardwareNotFound: if the hardware no longer appears in the database.
        """
        db_hardware = cls.dbapi.get_hardware_by_id(hardware_id)
        hardware = cls._from_db_object(context, cls(), db_hardware)
        return hardware

    @classmethod
    def get_by_uuid(cls, context: "RequestContext", uuid: str) -> "Hardware":
        """Find a hardware based on its UUID.

        Args:
            context (RequestContext): security context.
            uuid (str): The UUID of a hardware.

        Returns:
            A :class:`Hardware` object.

        Raises:
            HardwareNotFound: if the hardware no longer appears in the database.
        """
        db_hardware = cls.dbapi.get_hardware_by_uuid(uuid)
        hardware = cls._from_db_object(context, cls(), db_hardware)
        return hardware

    @classmethod
    def get_by_name(cls, context: "RequestContext", name: str) -> "Hardware":
        """Find a hardware based on its name.

        Args:
            context (RequestContext): security context.
            name (str): The name of a hardware.

        Returns:
            A :class:`Hardware` object.

        Raises:
            HardwareNotFound: if the hardware no longer appears in the database.
        """
        db_hardware = cls.dbapi.get_hardware_by_name(name)
        hardware = cls._from_db_object(context, cls(), db_hardware)
        return hardware

    @classmethod
    def list(cls, context: "RequestContext", limit: int=None, marker: str=None,
             sort_key: str=None, sort_dir: str=None) -> "list[Hardware]":
        """Return a list of Hardware objects.

        Args:
            context (RequestContext): security context.
            limit (int): maximum number of resources to return in a single
                result.
            marker (str): pagination marker for large data sets.
            sort_key (str): column to sort results by.
            sort_dir (str): direction to sort. "asc" or "desc".

        Returns:
            A list of :class:`Hardware` objects.
        """
        db_templates = cls.dbapi.get_hardware_list(
            limit=limit, marker=marker, sort_key=sort_key, sort_dir=sort_dir)
        return cls._from_db_object_list(context, db_templates)
