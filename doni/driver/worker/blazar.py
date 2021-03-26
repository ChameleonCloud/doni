"""Sync worker to update Blazar from Doni."""
from functools import wraps
from textwrap import shorten
from typing import TYPE_CHECKING

from keystoneauth1 import exceptions as kaexception
from oslo_log import log

from doni.common import args, exception, keystone
from doni.conf import auth as auth_conf
from doni.worker import BaseWorker, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware


LOG = log.getLogger(__name__)

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
_BLAZAR_ADAPTER = None


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
    _msg_fmt = (
        "Could not contact Blazar API. Please check the service "
        "configuration. The precise error was: %(message)s"
    )


class BlazarAPIError(exception.DoniException):
    _msg_fmt = "Blazar responded with HTTP %(code)s: %(text)s"


class BlazarAPIMalformedResponse(exception.DoniException):
    _msg_fmt = "Blazar response malformed: %(text)s"


class BlazarNodeProvisionStateTimeout(exception.DoniException):
    _msg_fmt = (
        "Blazar node %(node)s timed out updating its provision state to %(state)s"
    )


def _defer_on_node_locked(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BlazarAPIError as exc:
            if exc.code == 409:
                return WorkerResult.Defer({"message": "Node is locked by ironic."})
            raise

    return wrapper


class BlazarPhysicalHostWorker(BaseWorker):
    opts = []
    opt_group = "blazar"

    def register_opts(self, conf):
        conf.register_opts(self.opts, group=self.opt_group)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")

    def list_opts(self):
        return auth_conf.add_auth_opts(self.opts, service_type="reservation")

    @_defer_on_node_locked
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
        hw_props = hardware.properties
        info_to_set = {
            "name": hardware.uuid,
            "uid": hardware.uuid,
            "node_name": hardware.name,
            "node_type": hw_props.get("node_type"),
            "placement": {
                "node": hw_props.get("node"),
                "rack": hw_props.get("rack"),
            },
        }

        # If we know the host_id, then update that host.
        # If we don't then attempt to create it
        # we'll always "touch" the host, because we can't tell if this was a host
        # or a lease update request yet.
        result = {}

        host_id = state_details.get("blazar_host_id")
        if host_id:
            # Always try to update the host in blazar. We could add a precondition
            # header based on e.g. timestamp if needed.
            update = _call_blazar(
                context,
                f"/os-hosts/{host_id}",
                method="put",
                json=info_to_set,
                allowed_status_codes=[200],
            )
            result["host_updated_at"] = update.get("updated_at")
        else:
            # We don't have a cached host_id, try to create a host. If the host exists,
            # blazar will match the uuid, and the request will fail.
            host = _call_blazar(
                context,
                f"/os-hosts",
                method="post",
                json=info_to_set,
                allowed_status_codes=[201, 409],
            )
            state_details["id"] = host.get("id")
            result["host_created_at"] = host.get("created_at")

        for aw in availability_windows or []:
            request_body = {
                "name": aw.uuid,
                "start_date": aw.start,
                "end_date": aw.end,
                "reservations": [
                    {
                        "resource_type": "physical:host",
                        "min": 1,
                        "max": 1,
                        "hypervisor_properties": None,
                        "resource_properties": '["=","$uid",{aw.hardware_uuid}]',
                    },
                ],
            }

            lease = _call_blazar(
                context,
                f"/leases/{aw.uuid}",
                method="get",
                allowed_status_codes=[200, 404],
            )
            if lease:
                if not (aw.fields.items() <= lease.items()):
                    update = _call_blazar(
                        context,
                        f"/leases/{aw.uuid}",
                        method="put",
                        json=request_body,
                        allowed_status_codes=[200],
                    )
                    result["lease_updated_at"] = update.get("updated_at")
            else:
                lease = _call_blazar(
                    context,
                    f"/leases",
                    method="post",
                    json=request_body,
                    allowed_status_codes=[201],
                )
                result["lease_created_at"] = lease.get("created_at")

        return WorkerResult.Success(result)


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
