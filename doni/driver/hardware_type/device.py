from doni.common import args
from doni.driver.hardware_type.base import BaseHardwareType
from doni.worker import WorkerField


# Full list of names:
# https://www.balena.io/docs/reference/base-images/devicetypes/
SUPPORTED_MACHINE_NAMES = ["jetson-nano", "raspberrypi3-64", "raspberrypi4-64"]
DEVICE_NAME_MAP = {
    "jetson-nano": "Nvidia Jetson Nano SD-CARD",
    "raspberrypi3-64": "Raspberry Pi 3 (using 64bit OS)",
    "raspberrypi4-64": "Raspberry Pi 4 (using 64bit OS)",
}

SUPPORTED_CHANNEL_TYPES = ["wireguard"]

CHANNEL_SCHEMA = {
    "type": "object",
    "properties": {
        "channel_type": args.enum(SUPPORTED_CHANNEL_TYPES),
        "public_key": args.STRING,
    },
    "required": ["channel_type"],
    "additionalProperties": False,
}
CHANNELS_SCHEMA = {
    "type": "object",
    "properties": {
        "user": CHANNEL_SCHEMA,
        "mgmt": CHANNEL_SCHEMA,
    },
    "required": ["user"],
    "additionalProperties": False,
}

COMMON_FIELDS = [
    WorkerField(
        "machine_name",
        schema=args.enum(SUPPORTED_MACHINE_NAMES),
        required=True,
        description=(
            "The type of device -- this must be an explicitly supported device type"
        ),
    ),
    WorkerField(
        "contact_email",
        schema=args.EMAIL,
        required=True,
        private=True,
        description=(
            "A contact email to use for any communication about the device. In "
            "some cases secure messages containing enrollment credentials may be "
            "sent here, so ensure it is an active mailbox."
        ),
    ),
    WorkerField(
        "channels",
        schema=CHANNELS_SCHEMA,
        private=True,
        description=(
            "A set of communications channels this device will use. All devices "
            "should at minimum provide a 'user' channel, through which user "
            "workload traffic will pass. Often a 'mgmt' channel is also needed "
            "to enable the device to configure the device for the user's workload ."
        ),
    ),
]


class BalenaDevice(BaseHardwareType):
    enabled_workers = (
        "balena",
        "blazar.device",
        "tunelo",
    )

    default_fields = COMMON_FIELDS
