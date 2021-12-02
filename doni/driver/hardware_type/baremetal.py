from doni.common import args
from doni.driver.hardware_type.base import BaseHardwareType
from doni.worker import WorkerField


INTERFACES_SCHEMA = args.array(
    {
        "type": "object",
        "properties": {
            "name": args.STRING,
            "enabled": args.BOOLEAN,
            # There is no mac_address format in jsonschema yet[1]
            # [1]: https://github.com/json-schema-org/json-schema-spec/issues/540
            "mac_address": args.STRING,
            "vendor": args.STRING,
            "model": args.STRING,
            "switch_id": args.STRING,
            "switch_port_id": args.STRING,
            "switch_info": args.STRING,
            "pxe_enabled": args.BOOLEAN,
        },
        "required": ["name", "mac_address"],
        "additionalProperties": False,
    },
    min_items=1,
)


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
            schema=INTERFACES_SCHEMA,
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
