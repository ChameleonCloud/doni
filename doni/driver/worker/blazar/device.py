from typing import TYPE_CHECKING

from doni.driver.hardware_type.device import MACHINE_METADATA
from doni.driver.worker.blazar import BaseBlazarWorker
from doni.worker import WorkerField

if TYPE_CHECKING:
    from doni.objects.hardware import Hardware

UNKNOWN_DEVICE = "unknown"


class BlazarDeviceWorker(BaseBlazarWorker):
    resource_path = "/devices"
    resource_type = "device"

    fields = [
        WorkerField(
            "blazar_device_driver",
            default="k8s",
            description=(
                "Which Blazar device driver plugin to use to make the device "
                "reservable. Defaults to k8s."
            ),
        ),
    ]

    @classmethod
    def to_resource_pk(cls, hardware: "Hardware") -> str:
        # Devices are registered by name b/c Blazar will look up the kubelet in k8s
        # at resource create time, and kubelets are named after their hostname, which
        # is in turn set by the hardware name.
        return hardware.name

    @classmethod
    def expected_state(cls, hardware: "Hardware") -> dict:
        hw_props = hardware.properties
        machine_name = hw_props.get("machine_name")
        machine_meta = MACHINE_METADATA.get(machine_name, {})
        device_dict = {
            "uid": hardware.uuid,
            "device_driver": hw_props.get("blazar_device_driver"),
            "device_type": "container",
            "machine_name": machine_name,
            "device_name": machine_meta.get("full_name", UNKNOWN_DEVICE),
            "vendor": machine_meta.get("vendor", UNKNOWN_DEVICE),
            "model": machine_meta.get("model", UNKNOWN_DEVICE),
            # This differentiates v1 devices (enrolled as Zun compute nodes) and v2
            # devices (enrolled as k8s kubelets.)
            "platform_version": "2",
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
