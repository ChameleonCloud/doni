from doni.common import args
from doni.driver.hardware_type.base import BaseHardwareType
from doni.worker import WorkerField


class Baremetal(BaseHardwareType):
    """A bare metal node, provisionable via e.g., Ironic"""

    enabled_workers = (
        "blazar.physical_host",
        "ironic",
    )

    default_fields = [
        WorkerField(
            "management_address",
            schema=args.HOST_OR_IP,
            required=True,
            private=True,
            description="The out-of-band address, e.g. IPMI.",
        ),
        WorkerField(
            "interfaces",
            schema=args.array(args.NETWORK_DEVICE, min_items=1),
            required=True,
            description=("A list of network interfaces installed on the node."),
        ),
        WorkerField(
            "cpu_arch",
            schema=args.CPU_ARCH,
            required=True,
            default="x86_64",
            description=("The CPU architecture."),
        ),
    ]
