from doni.common import args
from doni.driver.hardware_type.base import BaseHardwareType
from doni.worker import WorkerField


# Full list of names:
# https://www.balena.io/docs/reference/base-images/devicetypes/
SUPPORTED_MACHINE_NAMES = [
    "jetson-nano",
    "jetson-xavier-nx-emmc",
    "raspberrypi3-64",
    "raspberrypi4-64",
    "coral-dev",
]
MACHINE_METADATA = {
    "jetson-nano": {
        "full_name": "Nvidia Jetson Nano SD-CARD",
        "vendor": "Nvidia",
        "model": "Jetson Nano",
    },
    "jetson-xavier-nx-emmc": {
        "full_name": "Nvidia Jetson Xavier NX eMMC",
        "vendor": "Nvidia",
        "model": "Jetson Xavier NX",
    },
    "raspberrypi3-64": {
        "full_name": "Raspberry Pi 3 (using 64bit OS)",
        "vendor": "Raspberry Pi",
        "model": "3",
    },
    "raspberrypi4-64": {
        "full_name": "Raspberry Pi 4 (using 64bit OS)",
        "vendor": "Raspberry Pi",
        "model": "4",
    },
    "coral-dev": {
        "full_name": "Google Coral Dev Board",
        "vendor": "Coral",
        "model": "Dev Board",
    },
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

LOCAL_EGRESS_TYPES = [
    "allow",
    "deny"
]

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
        "k8s_bootstrap_token",
        schema=args.STRING,
        required=False,
        private=False,
        description=(
            "A token used to join the device to a k3s cluster during bootstrap."
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
    WorkerField(
        "device_profiles",
        schema=args.array(args.STRING),
        required=False,
        private=False,
        description=(
            "A set of device profiles (representing a set of Linux resources that make "
            "it possible to access an attached peripheral, such as a USB or GPU "
            "device) currently supported on this device. Ideally this field is set via "
            "an automated process that has verified the required devices for the "
            "profile are all available."
        ),
    ),
    WorkerField(
        "local_egress",
        schema=args.enum(LOCAL_EGRESS_TYPES),
        required=False,
        default="allow",
        description=(
            "Whether containers on the device can send traffic to local networks."
        ),
    ),
]


class BalenaDevice(BaseHardwareType):
    enabled_workers = (
        "k8s",
        "balena",
        "blazar.device",
        "tunelo",
    )

    default_fields = COMMON_FIELDS
