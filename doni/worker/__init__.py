import abc
from typing import TYPE_CHECKING

from doni.common import args

if TYPE_CHECKING:
    from oslo_config.cfg import Opt

    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware


class WorkerState(object):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    ERROR = "ERROR"
    STEADY = "STEADY"


class WorkerResult:
    class Base(abc.ABC):
        """Abstract base class for worker result types.

        Attributes:
            payload (dict): Some detailed information about the result. This
                payload will be saved in the worker "state_details" field.
        """

        payload: dict = {}

        def __init__(self, payload: dict = None):
            self.payload = payload or {}

    class Defer(Base):
        """Indicates that the worker should defer execution of this task.

        Use this class when your worker can't proceed but could reasonably
        proceed in the near future once some state in the system has become
        eventually consistent to expectations.
        """

        DEFER_REASON_DETAIL = "defer_reason"

        def __init__(self, payload: dict = None, reason: str = None):
            self.reason = reason
            if payload is None:
                payload = {}
            if self.reason is not None:
                payload[self.DEFER_REASON_DETAIL] = self.reason
            super().__init__(payload)

    class Success(Base):
        """Indicates that the worker completed successfully."""


class WorkerField(object):
    """A Hardware field supported by a worker.

    Each worker defines which fields it uses for its functionality. Worker fields
    are ultimately stored on the Hardware as properties, but must pass validation
    at the API layer when fields are being added/updated by the end user.

    Fields persisted in a Hardware's properties that are no longer in use or
    supported by any worker are hidden from API responses and are not visible
    except to admins.

    .. note::

       Two workers cannot currently share the same field. If a worker depends
       on some field managed by another worker, that is technically possible,
       but such a field should not be declared by the dependant worker.

    Attributes:
        name (str): The name of the field.
        schema (dict): A JSON schema to validate the field against. If not
            defined, the field is assumed to be a string.
        default (any): The default value for this field, if none is provided.
        required (bool): Whether the field is required if the worker is in use.
        private (bool): Whether the field should be hidden (not serialized) when
            requested by an unauthorized user.
        sensitive (bool): Whether the field should be masked when serialized.
        description (str): A user-friendly description of the field's purpose
            and contents.
    """

    def __init__(
        self,
        name,
        schema=None,
        default=None,
        required=False,
        private=False,
        sensitive=False,
        description=None,
    ):
        self.name = name
        self.schema = schema or args.STRING
        self.default = default
        self.required = required
        self.private = private
        self.sensitive = sensitive
        self.description = description
