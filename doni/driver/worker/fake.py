from typing import TYPE_CHECKING

from doni.worker import BaseWorker, WorkerField, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.hardware import Hardware


class FakeWorker(BaseWorker):

    fields = [
        WorkerField("private-field", private=True),
        WorkerField("private-and-sensitive-field", private=True, sensitive=True),
        WorkerField("public-field", private=False),
        WorkerField("public-and-sensitive-field", private=False, sensitive=True),
    ]

    def process(
        self, context: "RequestContext", hardware: "Hardware"
    ) -> "WorkerResult.Base":
        print(f"fake: processing hardware {hardware.uuid}")
        return WorkerResult.Success(
            {"fake-result": f"fake-worker-prefix-{hardware.uuid}"}
        )
