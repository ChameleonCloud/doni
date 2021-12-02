from doni.common import args
from doni.driver.hardware_type.base import BaseHardwareType
from doni.worker import WorkerField


class Fake(BaseHardwareType):
    """A fake hardware type, useful for development and testing."""

    enabled_workers = ("fake-worker",)

    default_fields = [
        WorkerField("default_field", schema=args.STRING),
        WorkerField("default_required_field", schema=args.STRING, required=True),
    ]
