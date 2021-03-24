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


class BlazarPhysicalHostWorker(BaseWorker):
    opts = []
    opt_group = "blazar"

    def register_opts(self, conf):
        conf.register_opts(self.opts, group=self.opt_group)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")

    def list_opts(self):
        return auth_conf.add_auth_opts(self.opts, service_type="reservation")

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
        """
        desired_state = {
            # what data does blazar contain
            "name": "compute-1",
            "extra_capability_sample": "foo",
        }
        request_body = desired_state

        # If we know the host_id, then update that host.
        # If we don't then attempt to create it
        host_id = state_details.get("blazar_host_id")
        if host_id:
            host = _call_blazar(
                context,
                f"/os-hosts/{host_id}",
                method="put",
                json=request_body,
            )
            return WorkerResult.Success({"updated_at": host["updated_at"]})
        else:
            host = _call_blazar(
                context,
                f"/os-hosts",
                method="post",
                json=request_body,
            )
            return WorkerResult.Success({"created_at": host["created_at"]})


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
