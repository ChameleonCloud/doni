"""Sync worker to update Blazar from Doni."""
from datetime import datetime
from textwrap import shorten
from typing import TYPE_CHECKING

from dateutil.parser import parse
from keystoneauth1 import exceptions as kaexception
from oslo_log import log
from pytz import UTC

from doni.common import args, exception, keystone
from doni.conf import auth as auth_conf
from doni.driver.worker.base import BaseWorker
from doni.objects.availability_window import AvailabilityWindow
from doni.worker import WorkerField, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.hardware import Hardware


LOG = log.getLogger(__name__)

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
BLAZAR_DATE_FORMAT = "%Y-%m-%d %H:%M"
_BLAZAR_ADAPTER = None

PLACEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "rack": args.STRING,
        "node": args.STRING,
    },
    "additionalProperties": False,
}

AW_LEASE_PREFIX = "availability_window_"


def _get_blazar_adapter():
    global _BLAZAR_ADAPTER
    if not _BLAZAR_ADAPTER:
        _BLAZAR_ADAPTER = keystone.get_adapter(
            "blazar",
            session=keystone.get_session("blazar"),
            auth=keystone.get_auth("blazar"),
            version=BLAZAR_API_VERSION,
        )
    return _BLAZAR_ADAPTER


class BlazarUnavailable(exception.DoniException):
    """Exception for when the Blazar service cannot be contacted."""

    _msg_fmt = (
        "Could not contact Blazar API. Please check the service "
        "configuration. The precise error was: %(message)s"
    )


class BlazarIsWrongError(exception.DoniException):
    """Exception for when the Blazar service is in a bad state of some kind."""

    _msg_fmt = "Blazar is in a bad state. " "The precise error was: %(message)s"


class BlazarAPIError(exception.DoniException):
    """Exception for an otherwise unhandled error passed from Blazar's API."""

    _msg_fmt = "Blazar responded with HTTP %(code)s: %(text)s"


class BlazarAPIMalformedResponse(exception.DoniException):
    """Exception for malformed response from Blazar's API."""

    _msg_fmt = "Blazar response malformed: %(text)s"


class BlazarNodeProvisionStateTimeout(exception.DoniException):
    """Exception for a timeout on updating a host's provisioning state."""

    _msg_fmt = (
        "Blazar node %(node)s timed out updating its provision state to %(state)s"
    )


class BlazarWorkerDefer(exception.DoniException):
    """Signal to defer worker result."""


def _blazar_host_state(hw: "Hardware") -> dict:
    hw_props = hw.properties
    placement_props = hw_props.get("placement", {})
    body_dict = {"uid": hw.uuid, "node_name": hw.name}

    # FIXME(jason): Currently Blazar does not allow deleting extra capabilities,
    # and setting to None triggers errors in Blazar during create/update.
    # Until Blazar supports this, ignore nulled out fields. This means that we
    # cannot unset extra capabilities in Blazar, but that is already a problem
    # with Blazar's API itself. When that supports deletes, this can be updated.
    if hw_props.get("node_type"):
        body_dict["node_type"] = hw_props.get("node_type")
    if hw_props.get("cpu_arch"):
        body_dict["cpu_arch"] = hw_props.get("cpu_arch")
    if hw_props.get("su_factor"):
        body_dict["su_factor"] = hw_props.get("su_factor")
    if placement_props.get("node"):
        body_dict["placement.node"] = placement_props.get("node")
    if placement_props.get("rack"):
        body_dict["placement.rack"] = placement_props.get("rack")

    return body_dict


def _blazar_lease_request_body(aw: AvailabilityWindow) -> dict:
    body_dict = {
        "name": f"{AW_LEASE_PREFIX}{aw.uuid}",
        "start_date": aw.start.strftime(BLAZAR_DATE_FORMAT),
        "end_date": aw.end.strftime(BLAZAR_DATE_FORMAT),
        "reservations": [
            {
                "resource_type": "physical:host",
                "min": 1,
                "max": 1,
                "hypervisor_properties": None,
                "resource_properties": f'["==","$uid","{aw.hardware_uuid}"]',
            },
        ],
    }
    return body_dict


def _search_hosts_for_uuid(context: "RequestContext", hw_uuid: "str") -> dict:
    """Look up host in blazar by hw_uuid.

    If the blazar host id is uknown or otherwise incorrect, the only option
    is to get the list of all hosts from blazar, then search for matching
    hw_uuid.

    Returns a dict with the matching host's properties, including blazar_host_id.
    Returns None if not found.
    """
    host_list_response = _call_blazar(
        context,
        f"/os-hosts",
        method="get",
        json={},
    )
    host_list = host_list_response.get("hosts")
    matching_host = next(
        (host for host in host_list if host.get("hypervisor_hostname") == hw_uuid),
        None,
    )
    return matching_host


def _search_leases_for_lease_id(existing_leases: dict, new_lease: dict) -> tuple:
    matching_lease = next(
        (
            (index, lease)
            for index, lease in enumerate(existing_leases)
            if lease.get("name") == new_lease.get("name")
        ),
        (None, None),
    )
    return matching_lease


class BlazarPhysicalHostWorker(BaseWorker):
    """This class handles the synchronization of physical hosts from Doni to Blazar."""

    fields = [
        WorkerField(
            "node_type",
            schema=args.STRING,
            description=("A high-level classification of the type of node."),
        ),
        WorkerField(
            "placement",
            schema=PLACEMENT_SCHEMA,
            description=("Information about the physical placement of the node."),
        ),
        WorkerField(
            "su_factor",
            schema=args.NUMBER,
            description="The service unit (SU) hourly cost of the resource.",
            default=1.0,
        ),
    ]

    opts = []
    opt_group = "blazar"

    def register_opts(self, conf):
        super().register_opts(conf)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")

    def list_opts(self):
        return auth_conf.add_auth_opts(super().list_opts(), service_type="reservation")

    def _blazar_host_update(
        self, context, host_id, expected_state
    ) -> WorkerResult.Base:
        """Attempt to update existing host in blazar."""
        result = {}
        try:
            existing_state = _call_blazar(context, f"/os-hosts/{host_id}").get("host")
            # Do not make any changes if not needed
            if not any(
                existing_state.get(k) != expected_state[k]
                for k in expected_state.keys()
            ):
                return WorkerResult.Success()

            expected_state = _call_blazar(
                context,
                f"/os-hosts/{host_id}",
                method="put",
                json=expected_state,
            ).get("host")
        except BlazarAPIError as exc:
            # TODO what error code does blazar return if the host has a lease already?
            if exc.code == 404:
                # remove invalid stored host_id and retry after defer
                result["blazar_host_id"] = None
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                # Host cannot be updated, referenced by current lease
                return WorkerResult.Defer(result)

            raise  # Unhandled exception
        else:
            # On success, cache host_id and updated time
            result["blazar_host_id"] = expected_state.get("id")
            result["host_updated_at"] = expected_state.get("updated_at")
            return WorkerResult.Success(result)

    def _blazar_host_create(
        self, context, host_name, expected_state
    ) -> WorkerResult.Base:
        """Attempt to create new host in blazar."""
        result = {}
        try:
            body = expected_state.copy()
            body["name"] = host_name
            host = _call_blazar(
                context,
                f"/os-hosts",
                method="post",
                json=body,
            ).get("host")
        except BlazarAPIError as exc:
            if exc.code == 404:
                # host isn't in ironic.
                result["message"] = "Host does not exist in Ironic yet"
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                host = _search_hosts_for_uuid(context, host_name)
                if host:
                    # update stored host_id with match, and retry after defer
                    result["blazar_host_id"] = host.get("id")
                else:
                    # got conflict despite no matching host,
                    raise BlazarIsWrongError(
                        message=(
                            "Couldn't find host in Blazar, yet Blazar returned a "
                            "409 on host create. Check Blazar for errors."
                        )
                    )
                return WorkerResult.Defer(result)
            else:
                raise
        else:
            result["blazar_host_id"] = host.get("id")
            result["host_created_at"] = host.get("created_at")
            return WorkerResult.Success(result)

    def _blazar_lease_list(self, context: "RequestContext", hardware: "Hardware"):
        """Get list of all leases from blazar. Return dict of blazar response."""
        # List of all leases from blazar.
        lease_list_response = _call_blazar(
            context,
            "/leases",
            method="get",
        )
        return [
            lease
            for lease in lease_list_response.get("leases")
            # Perform a bit of a kludgy check to see if the UUID appears at
            # all in the nested JSON string representing the reservation
            # contraints.
            if (
                lease["name"].startswith(AW_LEASE_PREFIX)
                and hardware.uuid in str(lease["reservations"])
            )
        ]

    def _blazar_lease_update(
        self, context: "RequestContext", lease_id: "str", new_lease: "dict"
    ) -> WorkerResult.Base:
        """Update blazar lease if necessary. Return result dict."""
        result = {}
        try:
            response = _call_blazar(
                context,
                f"/leases/{lease_id}",
                method="put",
                json=new_lease,
            ).get("lease")
        except BlazarAPIError as exc:
            if exc.code == 404:
                return WorkerResult.Defer(reason="Host not found")
            elif exc.code == 409:
                return WorkerResult.Defer(reason="Conflicts with existing lease")
            raise
        else:
            result["updated_at"] = response.get("updated_at")
            return WorkerResult.Success(result)

    def _blazar_lease_create(
        self, context: "RequestContext", new_lease: "dict"
    ) -> WorkerResult.Base:
        """Create blazar lease. Return result dict."""
        result = {}
        try:
            lease = _call_blazar(
                context,
                f"/leases",
                method="post",
                json=new_lease,
            ).get("lease")
        except BlazarAPIError as exc:
            if exc.code == 404:
                return WorkerResult.Defer(reason="Host not found")
            elif exc.code == 409:
                return WorkerResult.Defer(reason="Conflicts with existing lease")
            raise
        else:
            result["lease_created_at"] = lease.get("created_at")
            return WorkerResult.Success(result)

    def _blazar_lease_delete(
        self, context: "RequestContext", lease_id: "str"
    ) -> WorkerResult.Base:
        """Delete Blazar lease."""
        _call_blazar(
            context,
            f"/leases/{lease_id}",
            method="delete",
        )
        return WorkerResult.Success()

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = {},
    ) -> "WorkerResult.Base":
        """Main loop for Blazar sync worker.

        This method ensures that an up-to-date blazar host object exists for
        each physical host in Doni's DB.

        "name" must match the name used by nova to identify the node. In our case
        it is the hardware uuid, as that is what ironic is passing to nova.
        blazar uses nova.get_servers_per_host to check if there is an existing
        server with that name.
        """
        # If we know the host_id, then update that host. Otherwise, attempt to create it.
        host_id = state_details.get("blazar_host_id")
        expected_host_state = _blazar_host_state(hardware)
        if host_id:
            host_result = self._blazar_host_update(
                context, host_id, expected_host_state
            )
        else:
            # Without a cached host_id, try to create a host. If the host exists,
            # blazar will match the uuid, and the request will fail.
            host_result = self._blazar_host_create(
                context, hardware.uuid, expected_host_state
            )

        if isinstance(host_result, WorkerResult.Defer):
            return host_result  # Return early on defer case

        # Get all leases from blazar
        leases_to_check = self._blazar_lease_list(context, hardware)

        lease_results = []
        # Loop over all availability windows that Doni has for this hw item
        for aw in availability_windows or []:
            new_lease = _blazar_lease_request_body(aw)
            # Check to see if lease name already exists in blazar
            matching_index, matching_lease = _search_leases_for_lease_id(
                leases_to_check, new_lease
            )
            if matching_lease:
                # Pop each existing lease from the list. Any remaining at the end will be removed.
                leases_to_check.pop(matching_index)
                lease_for_update = new_lease.copy()
                # Do not attempt to update reservations; we only support updating
                # the start and end date.
                lease_for_update.pop("reservations", None)

                if lease_for_update.items() <= matching_lease.items():
                    # If new lease is a subset of old_lease, we don't need to update
                    continue

                # When comparing availability windows to leases, ensure we are
                # comparing w/ the same precision as Blazar allows (minutes)
                aw_start = aw.start.replace(second=0, microsecond=0)
                matching_lease_start = UTC.localize(parse(matching_lease["start_date"]))

                if (
                    matching_lease_start < datetime.now(tz=UTC)
                    and aw_start > matching_lease_start
                ):
                    # Special case, updating an availability window to start later,
                    # after it has already been entered in to Blazar. This is not
                    # strictly allowed by Blazar (updating start time after lease begins)
                    # but we can fake it with a delete/create.
                    lease_results.append(
                        self._blazar_lease_delete(context, matching_lease["id"])
                    )
                    lease_results.append(self._blazar_lease_create(context, new_lease))
                else:
                    lease_results.append(
                        self._blazar_lease_update(
                            context, matching_lease["id"], lease_for_update
                        )
                    )
            else:
                lease_results.append(self._blazar_lease_create(context, new_lease))

        delete_results = []
        # Delete any leases that are in blazar, but not in the desired availability window.
        for lease in leases_to_check:
            delete_results.append(self._blazar_lease_delete(context, lease["id"]))

        if any(
            isinstance(res, WorkerResult.Defer)
            for res in lease_results + delete_results
        ):
            host_result = WorkerResult.Defer(
                host_result.payload,
                reason="One or more availability window leases failed to update",
            )

        return host_result

    def import_existing(self, context: "RequestContext"):
        existing_hosts = []
        for host in _call_blazar(context, "/os-hosts")["hosts"]:
            existing_hosts.append(
                {
                    "uuid": host["hypervisor_hostname"],
                    "name": host.get("node_name"),
                    "properties": {
                        "node_type": host.get("node_type"),
                        "placement": {
                            "node": host.get("placement.node"),
                            "rack": host.get("placement.rack"),
                        },
                        "su_factor": host.get("su_factor"),
                    },
                }
            )
        return existing_hosts


def _call_blazar(context, path, method="get", json=None, allowed_status_codes=[]):
    try:
        blazar = _get_blazar_adapter()
        resp = blazar.request(
            path,
            method=method,
            json=json,
            microversion=BLAZAR_API_MICROVERSION,
            global_request_id=context.global_id,
            raise_exc=False,
        )
    except kaexception.ClientException as exc:
        raise BlazarUnavailable(message=str(exc))

    if resp.status_code >= 400 and resp.status_code not in allowed_status_codes:
        raise BlazarAPIError(code=resp.status_code, text=shorten(resp.text, width=50))

    try:
        # Treat empty response bodies as None
        return resp.json() if resp.text else None
    except Exception:
        raise BlazarAPIMalformedResponse(text=shorten(resp.text, width=50))
