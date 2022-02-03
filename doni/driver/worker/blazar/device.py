from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField


class BlazarDeviceWorker(BaseWorker):
    worker_fields = [
        WorkerField(
            "blazar_device_driver",
            required=True,
            description=(
                "Which Blazar device driver plugin to use to make the device "
                "reservable."
            ),
        )
    ]
