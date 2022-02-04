from doni.common import args
from doni.conf import auth as auth_conf
from doni.driver.worker.blazar import BaseBlazarWorker
from doni.worker import WorkerField


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
    def to_reservation_values(cls, hardware_uuid: str) -> dict:
        return {
            "resource_type": "device",
            "min": 1,
            "max": 1,
            "resource_properties": f'["==","$uid","{hardware_uuid}"]',
        }
