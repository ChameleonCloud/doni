from typing import TYPE_CHECKING

from doni.driver.hardware_type.device import DEVICE_NAME_MAP
from doni.driver.worker.blazar import BaseBlazarWorker
from doni.worker import WorkerField

if TYPE_CHECKING:
    from doni.objects.hardware import Hardware

UNKNOWN_DEVICE = "(unknown)"


class BlazarDeviceWorker(BaseBlazarWorker):
    resource_path = "/devices"
    resource_type = "device"

    fields = [
        WorkerField(
            "blazar_device_driver",
            default="k8s",
            required=True,
            description=(
                "Which Blazar device driver plugin to use to make the device "
                "reservable. Defaults to k8s."
            ),
        )
    ]

    @classmethod
    def expected_state(cls, hardware: "Hardware") -> dict:
        hw_props = hardware.properties
        device_dict = {
            "uid": hardware.uuid,
            "device_driver": hw_props.get("blazar_device_driver"),
            "device_type": "container",
            "machine_name": hw_props.get("machine_name"),
            "device_name": DEVICE_NAME_MAP.get(hw_props.get("device_name"))
            or UNKNOWN_DEVICE,
        }
        return device_dict

    @classmethod
    def to_reservation_values(cls, hardware_uuid: str) -> dict:
        return {
            "resource_type": "device",
            "min": 1,
            "max": 1,
            "resource_properties": f'["==","$uid","{hardware_uuid}"]',
        }
