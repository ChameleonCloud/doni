from typing import TYPE_CHECKING

from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware


class FakeWorker(BaseWorker):

    fields = [
        WorkerField("private-field", private=True),
        WorkerField("private-and-sensitive-field", private=True, sensitive=True),
        WorkerField("public-field", private=False),
        WorkerField("public-and-sensitive-field", private=False, sensitive=True),
        WorkerField("default-field", private=True, default="default-field-value"),
    ]

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = [],
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        print(f"fake: processing hardware {hardware.uuid}")
        return WorkerResult.Success(
            {
                "fake-result": hardware.uuid,
                "fake-availability_windows": [aw.uuid for aw in availability_windows],
            }
        )
