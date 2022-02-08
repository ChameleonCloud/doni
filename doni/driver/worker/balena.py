from typing import TYPE_CHECKING

from oslo_config.cfg import DictOpt, StrOpt
from oslo_log import log

from doni.common import args
from doni.conf import CONF
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField, WorkerResult


if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

LOG = log.getLogger(__name__)
BALENA_SDK = None


def _get_balena_sdk():
    global BALENA_SDK
    if not BALENA_SDK:
        from balena import Balena

        BALENA_SDK = Balena()
        if CONF.balena.api_endpoint:
            BALENA_SDK.settings.set("api_endpoint", CONF.balena.api_endpoint)
        BALENA_SDK.auth.login_with_token(CONF.balena.api_token)
    return BALENA_SDK


class BalenaWorker(BaseWorker):
    fields = [
        WorkerField(
            "application_credential_id",
            schema=args.STRING,
            private=True,
            required=True,
            description=(
                "The ID of a Keystone Application Credential that will be used to "
                "allow the Balena device to query OpenStack APIs. This credential "
                "should be scoped to the same project that enrolled the device into "
                "Doni. The credential does not need to be 'unrestricted', i.e., it "
                "does not need to be able to create other credentials/trusts."
            ),
        ),
        WorkerField(
            "application_credential_secret",
            schema=args.STRING,
            private=True,
            sensitive=True,
            required=True,
            description="The secret for the Keystone Application Credential",
        ),
    ]

    opts = [
        StrOpt(
            "api_endpoint",
            help=(
                "The Balena API endpoint. This can be used to point to an openBalena "
                "instance of the Balena API. Defaults to public balenaCloud."
            ),
        ),
        StrOpt(
            "api_token",
            required=True,
            help=("A Balena API token to use for authenticating to the Balena API."),
        ),
        DictOpt(
            "device_fleet_mapping",
            required=True,
            help=(
                "A mapping of Balena device types to the name of the fleet that "
                "those devices should be registered with."
            ),
        ),
        StrOpt(
            "credential_service_name",
            default="coordinator",
            help=(
                "The name of the Balena service in the fleet that should receive the "
                "Application Credential and secret as a device env var. All fleets "
                "defined in the ``device_fleet_mapping`` must implement this service."
            ),
        ),
    ]
    opt_group = "balena"

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        device_id = self._to_device_id(hardware.uuid)
        self._register_device(hardware)
        self._sync_device_var(
            hardware.uuid,
            "OS_APPLICATION_CREDENTIAL_ID",
            hardware.properties.get("application_credential_id"),
            service_name=CONF.balena.credential_service_name,
        )
        self._sync_device_var(
            hardware.uuid,
            "OS_APPLICATION_CREDENTIAL_SECRET",
            hardware.properties.get("application_credential_secret"),
            service_name=CONF.balena.credential_service_name,
        )

        if "device_api_key" not in state_details:
            # Generate a device key and store on the state; the device owner (user)
            # will be querying Doni for this information and configuring their device
            # OS image with it.
            device_api_key = _get_balena_sdk().models.device.generate_device_key(
                device_id
            )
            state_details["device_api_key"] = device_api_key
            LOG.info(f"Generated device API key for {hardware.uuid}")

        return WorkerResult.Success(state_details)

    def _register_device(self, hardware: "Hardware"):
        from balena.exceptions import DeviceNotFound

        balena = _get_balena_sdk()
        device_id = self._to_device_id(hardware.uuid)

        try:
            device = balena.models.device.get(device_id)
            if device.get("name") != hardware.name:
                balena.models.device.rename(device["id"], hardware.name)
                LOG.info(f"Updated device name for {hardware.uuid}")
        except DeviceNotFound:
            machine_name = hardware.properties.get("machine_name")
            fleet_name = CONF.balena.device_fleet_mapping.get(machine_name)
            if not fleet_name:
                raise ValueError(
                    f"No fleet is configured for machine name {machine_name}"
                )
            fleet = balena.models.application.get(fleet_name)
            device = balena.models.device.register(fleet["id"], device_id)
            balena.models.device.rename(device["id"], hardware.name)
            LOG.info(f"Registered new device for {hardware.uuid}")

    def _to_device_id(self, hardware_uuid: str):
        return hardware_uuid.replace("-", "")

    def _sync_device_var(self, hardware_uuid, key, value, service_name=None):
        balena = _get_balena_sdk()
        device_id = self._to_device_id(hardware_uuid)

        if service_name:
            device_vars = (
                balena.models.environment_variables.device_service_environment_variable
            )
        else:
            device_vars = balena.models.environment_variables.device

        existing = next(
            iter([var for var in device_vars.get_all(device_id) if var["name"] == key]),
            None,
        )
        if not existing:
            if service_name:
                device_vars.create(device_id, service_name, key, value)
            else:
                device_vars.create(device_id, key, value)
            LOG.info(f"Created new device env var {key} for {hardware_uuid}")
        elif existing["value"] != value:
            device_vars.update(existing["id"], value)
            LOG.info(f"Updated device env var {key} for {hardware_uuid}")
