from datetime import datetime, timezone
from typing import TYPE_CHECKING

from oslo_config.cfg import DictOpt, StrOpt
from oslo_log import log

from doni.api import utils as api_utils
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
        if hardware.deleted:
            self._delete_device(hardware)
            return WorkerResult.Success(
                {
                    "device_id": None,
                    "device_api_key": None,
                    "fleet_id": None,
                    "last_seen": None,
                }
            )

        balena_device = self._register_device(hardware)
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

        balena = _get_balena_sdk()
        device_id = self._to_device_id(hardware.uuid)

        if "device_api_key" not in state_details:
            # Generate a device key and store on the state; the device owner (user)
            # will be querying Doni for this information and configuring their device
            # OS image with it.
            device_api_key = balena.models.device.generate_device_key(device_id)
            state_details["device_api_key"] = device_api_key
            LOG.info(f"Generated device API key for {hardware.uuid}")

        # Balena gives the device a UUID but also maintains an internal ID, which is
        # useful to track for some operations
        state_details["device_id"] = balena_device["id"]
        state_details["fleet_id"] = balena_device["belongs_to__application"].get("__id")
        state_details["last_seen"] = (
            api_utils.format_date(datetime.now(tz=timezone.utc))
            if balena_device["is_online"]
            else balena_device["last_connectivity_event"]
        )

        return WorkerResult.Success(state_details)

    def _register_device(self, hardware: "Hardware"):
        from balena.exceptions import DeviceNotFound

        balena = _get_balena_sdk()
        device_id = self._to_device_id(hardware.uuid)
        machine_name = hardware.properties.get("machine_name")

        device_types = {
            device_type["slug"]: device_type["id"]
            for device_type in balena.models.device_type.get_all()
        }

        def set_device_type(uuid, device_type):
            # This function isn't available in the SDK but we can implement
            # using some available primitives.
            return balena.models.device.__set(
                uuid_or_id_or_ids=uuid,
                body={"is_of__device_type": device_type}
            )

        try:
            device = balena.models.device.get(device_id)
            if device["device_name"] != hardware.name:
                balena.models.device.rename(device_id, hardware.name)
                LOG.info(f"Updated device name for {hardware.uuid}")
            if device["is_of__device_type"]["__id"] != device_types[machine_name]:
                set_device_type(hardware.uuid, device_types[machine_name])
                LOG.info(f"Updated device type for {hardware.uuid}")
        except DeviceNotFound:
            fleet_name = CONF.balena.device_fleet_mapping.get(machine_name)
            if not fleet_name:
                raise ValueError(
                    f"No fleet is configured for machine name {machine_name}"
                )
            fleet = balena.models.application.get_by_name(fleet_name)
            device = balena.models.device.register(fleet["id"], device_id, device_types[machine_name])
            # Balena will have auto-assigned a device name, change to user-specified
            balena.models.device.rename(device_id, hardware.name)
            LOG.info(f"Registered new device for {hardware.uuid}")
            # Perform one additional fetch; when the device is returned from the
            # register endpoint, it is missing the belongs_to__application field,
            # which we use later. Probably this is a bug in openBalena
            device = balena.models.device.get(device_id)

        return device

    def _delete_device(self, hardware: "Hardware"):
        balena = _get_balena_sdk()
        balena.models.device.remove(self._to_device_id(hardware.uuid))
        LOG.info(f"Deleted device for {hardware.uuid}")

    def _to_device_id(self, hardware_uuid: str):
        return hardware_uuid.replace("-", "")

    def _existing(self, env_var_list, key):
        existing = next(
            iter([var for var in env_var_list if var["name"] == key]),
            None,
        )
        return existing


    def _sync_device_var(self, hardware_uuid, key, value, service_name=None):
        balena = _get_balena_sdk()
        device_id = self._to_device_id(hardware_uuid)

        service_var_base = balena.models.device.service_var
        env_var_base = balena.models.device.env_var

        if service_name:
            env_var_list = service_var_base.get_all_by_device(device_id)
        else:
            env_var_list = env_var_base.get_all_by_device(device_id)

        existing = self._existing(env_var_list, key)

        if not existing:
            if service_name:
                service_var_base.set(device_id, service_name, key, value)
            else:
                env_var_base.set(device_id, key, value)
            LOG.info(f"Created new device env var {key} for {hardware_uuid}")
        elif existing["value"] != value:
            if service_name:
                service_var_base.set(device_id, service_name, key, value)
            else:
                env_var_base.set(device_id, key, value)
            LOG.info(f"Updated device env var {key} for {hardware_uuid}")
