"""Sync worker to update Blazar from Doni."""
from functools import wraps
from textwrap import shorten
from typing import TYPE_CHECKING

from keystoneauth1 import exceptions as kaexception
from oslo_log import log

from doni.common import args, exception, keystone
from doni.conf import auth as auth_conf
from doni.objects.availability_window import AvailabilityWindow
from doni.worker import BaseWorker, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.hardware import Hardware


LOG = log.getLogger(__name__)

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
_BLAZAR_ADAPTER = None

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


def _blazar_host_requst_body(hw: "Hardware") -> dict:
    hw_props = hw.properties
    body_dict = {
        "name": hw.uuid,
        "uid": hw.uuid,
        "node_name": hw.name,
        "node_type": hw_props.get("node_type"),
        "placement": {
            "node": hw_props.get("node"),
            "rack": hw_props.get("rack"),
        },
    }
    return body_dict


def _blazar_lease_requst_body(aw: AvailabilityWindow) -> dict:
    body_dict = {
        "name": f"{AW_LEASE_PREFIX}{aw.uuid}",
        "start_date": aw.start.isoformat(),
        "end_date": aw.end.isoformat(),
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
        (host for host in host_list if host.get("name") == hw_uuid),
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

    opts = []
    opt_group = "blazar"

    def register_opts(self, conf):
        """TODO What does this do?"""
        conf.register_opts(self.opts, group=self.opt_group)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")

    def list_opts(self):
        """TODO What does this do?"""
        return auth_conf.add_auth_opts(self.opts, service_type="reservation")

    def _blazar_host_update(self, context, hardware, host_id) -> WorkerResult:
        """Attempt to update existing host in blazar."""
        result = {}
        try:
            blazar_host = _call_blazar(
                context,
                f"/os-hosts/{host_id}",
                method="put",
                json=_blazar_host_requst_body(hardware),
            )
        except BlazarAPIError as exc:
            # TODO what error code does blazar return if the host has a lease already?
            if exc.code == 404:
                blazar_host = _search_hosts_for_uuid(context, hardware.uuid)
                if blazar_host:
                    # update stored host_id with match, and retry after defer
                    result["blazar_host_id"] = blazar_host.get("id")
                else:
                    # remove invalid stored host_id and retry after defer
                    result["blazar_host_id"] = None
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                # Host cannot be updated, referenced by current lease
                return WorkerResult.Defer(result)
            else:
                raise  # Unhandled exception
        else:
            # On success, cache host_id and updated time
            result["blazar_host_id"] = blazar_host.get("id")
            result["host_updated_at"] = blazar_host.get("updated_at")
            return WorkerResult.Success(result)

    def _blazar_host_create(self, context, hardware) -> WorkerResult:
        """Attempt to create new host in blazar."""
        result = {}
        try:
            host = _call_blazar(
                context,
                f"/os-hosts",
                method="post",
                json=_blazar_host_requst_body(hardware),
            )
        except BlazarAPIError as exc:
            if exc.code == 404:
                # host isn't in ironic.
                result["message"] = "Host does not exist in Ironic yet"
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                host = _search_hosts_for_uuid(context, hardware.uuid)
                if host:
                    # update stored host_id with match, and retry after defer
                    result["blazar_host_id"] = host.get("id")
                else:
                    # got conflict despite no matching host,
                    raise BlazarIsWrongError()
                return WorkerResult.Defer(result)
            else:
                raise
        else:
            result["blazar_host_id"] = host.get("id")
            result["host_created_at"] = host.get("created_at")
            return WorkerResult.Success(result)

    def _blazar_lease_list(self, context: "RequestContext"):
        """Get list of all leases from blazar. Return dict of blazar response."""

        def _allowed_prefix(lease: "dict") -> "bool":
            lease_name = lease.get("name")
            return lease_name.startswith(AW_LEASE_PREFIX)

        # List of all leases from blazar.
        lease_list_response = _call_blazar(
            context,
            "/leases",
            method="get",
        )
        lease_list = lease_list_response.get("leases")
        filtered_list = filter(_allowed_prefix, lease_list)
        return list(filtered_list)

    def _blazar_lease_update(self, context: "RequestContext", new_lease: "dict"):
        """Update blazar lease if necessary. Return result dict."""
        result = {}
        try:
            response = _call_blazar(
                context,
                f"/leases/{new_lease.get('name')}",
                method="put",
                json=new_lease,
            )
        except BlazarAPIError as exc:
            if exc.code == 404:
                # TODO lease ID doesn't exist, how to handle this case?
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                # TODO Lease update conflicts with another, how to handle this case?
                return WorkerResult.Defer(result)
        else:
            result["lease_updated_at"] = response.get("updated_at")
            return WorkerResult.Success(result)

    def _blazar_lease_create(self, context: "RequestContext", new_lease: "dict"):
        """Create blazar lease. Return result dict."""
        result = {}
        try:
            lease = _call_blazar(
                context,
                f"/leases",
                method="post",
                json=new_lease,
            )
        except BlazarAPIError as exc:
            if exc.code == 404:
                # TODO Host id in lease doesn't exist, what do do?
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                # TODO Lease conflicts with another, how to handle this case?
                return WorkerResult.Defer(result)
        else:
            result["lease_created_at"] = lease.get("created_at")
            return WorkerResult.Success(result)

    def _blazar_lease_delete(self, context: "RequestContext", lease: "dict"):
        """Create blazar lease. Return result dict."""
        result = {}
        try:
            lease = _call_blazar(
                context,
                f"/leases/{lease.get('name')}",
                method="delete",
            )
        except BlazarAPIError as exc:
            if exc.code == 404:
                # TODO Host id in lease doesn't exist, what do do?
                return WorkerResult.Defer(result)
        else:
            return WorkerResult.Success(result)

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
        if host_id:
            # TODO: We always update the host. We should add a precondition of some kind.
            host_result = self._blazar_host_update(context, hardware, host_id)
        else:
            # Without a cached host_id, try to create a host. If the host exists,
            # blazar will match the uuid, and the request will fail.
            host_result = self._blazar_host_create(context, hardware)

        if isinstance(host_result, WorkerResult.Defer):
            return host_result  # Return early on defer case

        # Get all leases from blazar
        leases_to_check = self._blazar_lease_list(context)

        lease_results = []
        # Loop over all availability windows that Doni has for this hw item
        for aw in availability_windows or []:
            new_lease = _blazar_lease_requst_body(aw)
            # Check to see if lease name already exists in blazar
            matching_index, matching_lease = _search_leases_for_lease_id(
                leases_to_check, new_lease
            )
            if matching_lease:
                # Pop each existing lease from the list. Any remaining at the end will be removed.
                leases_to_check.pop(matching_index)
                # Update case.
                if not new_lease.items() <= matching_lease.items():
                    # If new lease is a subset of old_lease, we don't need to update
                    lease_results.append(self._blazar_lease_update(context, new_lease))
            else:
                lease_results.append(self._blazar_lease_create(context, new_lease))

        delete_results = []
        # Delete any leases that are in blazar, but not in the desired availability window.
        for lease in leases_to_check:
            delete_results.append(self._blazar_lease_delete(context, lease))

        for result in [lease_results, delete_results]:
            if isinstance(result, WorkerResult.Defer):
                return WorkerResult.Success(host_result)

        return host_result

    def import_existing(self, context: "RequestContext"):
        """Get all known external state managed by this worker.

        This is an optional capability of a worker and supports an 'import' flow where existing
        resources/state outside of the doni can be brought under doni's management.

        The expected return type is a list of objects with a "uuid" and a "properties" key,
        representing the UUID of the hardware the state corresponds to (or None if one
        could not be reasonably determined and should be auto-assigned) and a set of
        properties that should be imported for that hardware item.
        """
        existing_hosts = _call_blazar(context, "/os-hosts")["hosts"]
        for host in existing_hosts:
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
