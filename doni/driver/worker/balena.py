from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField


class BalenaWorker(BaseWorker):
    fields = [
        WorkerField(
            "application_credential_id",
            schema="",
            private=True,
            required=True,
            description="",
        ),
        WorkerField(
            "application_credential_secret",
            schema="",
            private=True,
            sensitive=True,
            required=True,
            description="",
        ),
    ]

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        # Ensure the device is pre-registered in Balena.

        # Put the application credential information as device environment variables
        # on the coordinator container.

        # Create a Balena device API key and put it on worker state.

        return super().process(context, hardware, availability_windows, state_details)
