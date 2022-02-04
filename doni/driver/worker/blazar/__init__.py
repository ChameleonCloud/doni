from datetime import datetime
from typing import TYPE_CHECKING

from dateutil.parser import parse
from pytz import UTC

from doni.common import exception, keystone
from doni.conf import auth as auth_conf
from doni.driver.util import ks_service_requestor, KeystoneServiceAPIError
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
BLAZAR_DATE_FORMAT = "%Y-%m-%d %H:%M"
_BLAZAR_ADAPTER = None

AW_LEASE_PREFIX = "availability_window_"


class BlazarIsWrongError(exception.DoniException):
    """Exception for when the Blazar service is in a bad state of some kind."""

    _msg_fmt = "Blazar is in a bad state. The precise error was: %(message)s"


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


def call_blazar(*args, **kwargs):
    return ks_service_requestor("Blazar", _get_blazar_adapter)(*args, **kwargs)


class BaseBlazarWorker(BaseWorker):
    """A base Blazar worker that syncs a Hardware to some Blazar resource.

    The base worker also handles managing availability windows for the resource.
    """

    opts = []
    opt_group = "blazar"

    def register_opts(self, conf):
        super().register_opts(conf)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")

    def list_opts(self):
        return auth_conf.add_auth_opts(super().list_opts(), service_type="reservation")

    @classmethod
    def to_lease(cls, aw: "AvailabilityWindow") -> dict:
        return {
            "name": f"{AW_LEASE_PREFIX}{aw.uuid}",
            "start_date": aw.start.strftime(BLAZAR_DATE_FORMAT),
            "end_date": aw.end.strftime(BLAZAR_DATE_FORMAT),
            "reservations": [
                cls.to_reservation_values(aw.hardware_uuid),
            ],
        }

    @classmethod
    def to_reservation_values(cls, hardware_uuid: str) -> dict:
        """Given an AvailabilityWindow, """
        raise NotImplementedError()

    @classmethod
    def expected_state(cls, hardware: "Hardware") -> dict:
        raise NotImplementedError()

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        # If we know the resource_id, then update that host. Otherwise, create it.
        resource_id = state_details.get("blazar_resource_id")
        if resource_id:
            result = self._resource_update(
                context, resource_id, self.expected_state(hardware)
            )
        else:
            # Without a cached resource_id, try to create a host. If the host exists,
            # blazar will match the uuid, and the request will fail.
            result = self._resource_create(
                context, hardware.uuid, self.expected_state(hardware)
            )

        if isinstance(result, WorkerResult.Defer):
            return result  # Return early on defer case

        return self.process_availability_windows(
            context, hardware, availability_windows, result
        )

    def process_availability_windows(
        self, context, hardware, availability_windows, resource_result
    ):
        # Get all leases from blazar
        leases_to_check = self._lease_list(context, hardware)

        lease_results = []
        # Loop over all availability windows that Doni has for this hw item
        for aw in availability_windows or []:
            new_lease = self.to_lease(aw)
            # Check to see if lease name already exists in blazar
            matching_index, matching_lease = next(
                (
                    (index, lease)
                    for index, lease in enumerate(leases_to_check)
                    if lease.get("name") == new_lease.get("name")
                ),
                (None, None),
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
                        self._lease_delete(context, matching_lease["id"])
                    )
                    lease_results.append(self._lease_create(context, new_lease))
                else:
                    lease_results.append(
                        self._lease_update(
                            context, matching_lease["id"], lease_for_update
                        )
                    )
            else:
                lease_results.append(self._lease_create(context, new_lease))

        delete_results = []
        # Delete any leases that are in blazar, but not in the desired availability window.
        for lease in leases_to_check:
            delete_results.append(self._lease_delete(context, lease["id"]))

        if any(
            isinstance(res, WorkerResult.Defer)
            for res in lease_results + delete_results
        ):
            return WorkerResult.Defer(
                resource_result.payload,
                reason="One or more availability window leases failed to update",
            )
        else:
            # Preserve the original host result
            return resource_result

    def _resource_create(self, context, uuid, expected_state) -> WorkerResult.Base:
        """Attempt to create new host in blazar."""
        result = {}
        try:
            body = expected_state.copy()
            body["name"] = uuid
            resource = call_blazar(
                context,
                self.resource_path,
                method="post",
                json=body,
            ).get(self.resource_type)
        except KeystoneServiceAPIError as exc:
            if exc.code == 404:
                # host isn't in ironic.
                result["message"] = "Host does not exist in Ironic yet"
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                resource = self._find_resource(context, uuid)
                if resource:
                    # update stored resource_id with match, and retry after defer
                    result["blazar_resource_id"] = resource.get("id")
                else:
                    # got conflict despite no matching resource,
                    raise BlazarIsWrongError(
                        message=(
                            "Couldn't find resource in Blazar, yet Blazar returned a "
                            "409 on host create. Check Blazar for errors."
                        )
                    )
                return WorkerResult.Defer(result)
            else:
                raise
        else:
            result["blazar_resource_id"] = resource.get("id")
            result["resource_created_at"] = resource.get("created_at")
            return WorkerResult.Success(result)

    def _resource_update(
        self, context, resource_id, expected_state
    ) -> WorkerResult.Base:
        """Attempt to update existing host in blazar."""
        result = {}
        try:
            existing_state = call_blazar(
                context, f"{self.resource_path}/{resource_id}"
            ).get(self.resource_type)
            # Do not make any changes if not needed
            if not any(
                existing_state.get(k) != expected_state[k]
                for k in expected_state.keys()
            ):
                return WorkerResult.Success()

            expected_state = call_blazar(
                context,
                f"{self.resource_path}/{resource_id}",
                method="put",
                json=expected_state,
            ).get(self.resource_type)
        except KeystoneServiceAPIError as exc:
            # TODO what error code does blazar return if the host has a lease already?
            if exc.code == 404:
                # remove invalid stored resource_id and retry after defer
                result["blazar_resource_id"] = None
                return WorkerResult.Defer(result)
            elif exc.code == 409:
                # Host cannot be updated, referenced by current lease
                return WorkerResult.Defer(result)

            raise  # Unhandled exception
        else:
            # On success, cache resource_id and updated time
            result["blazar_resource_id"] = expected_state.get("id")
            result["resource_updated_at"] = expected_state.get("updated_at")
            return WorkerResult.Success(result)

    def _lease_list(self, context: "RequestContext", hardware: "Hardware"):
        """Get list of all leases from blazar. Return dict of blazar response."""
        # List of all leases from blazar.
        lease_list_response = call_blazar(
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

    def _lease_create(
        self, context: "RequestContext", new_lease: "dict"
    ) -> WorkerResult.Base:
        """Create blazar lease. Return result dict."""
        result = {}
        try:
            lease = call_blazar(
                context,
                f"/leases",
                method="post",
                json=new_lease,
            ).get("lease")
        except KeystoneServiceAPIError as exc:
            if exc.code == 404:
                return WorkerResult.Defer(reason="Resource not found")
            elif exc.code == 409:
                return WorkerResult.Defer(reason="Conflicts with existing lease")
            raise
        else:
            result["lease_created_at"] = lease.get("created_at")
            return WorkerResult.Success(result)

    def _lease_update(
        self, context: "RequestContext", lease_id: "str", new_lease: "dict"
    ) -> WorkerResult.Base:
        """Update blazar lease if necessary. Return result dict."""
        result = {}
        try:
            response = call_blazar(
                context,
                f"/leases/{lease_id}",
                method="put",
                json=new_lease,
            ).get("lease")
        except KeystoneServiceAPIError as exc:
            if exc.code == 404:
                return WorkerResult.Defer(reason="Resource not found")
            elif exc.code == 409:
                return WorkerResult.Defer(reason="Conflicts with existing lease")
            raise
        else:
            result["updated_at"] = response.get("updated_at")
            return WorkerResult.Success(result)

    def _lease_delete(
        self, context: "RequestContext", lease_id: "str"
    ) -> WorkerResult.Base:
        """Delete Blazar lease."""
        call_blazar(
            context,
            f"/leases/{lease_id}",
            method="delete",
        )
        return WorkerResult.Success()

    def _find_resource(self, context: "RequestContext", hw_uuid: "str") -> dict:
        """Look up resource in blazar by hw_uuid.

        If the blazar resource id is uknown or otherwise incorrect, the only option
        is to get the list of all resources from blazar, then search for matching
        hw_uuid.

        Returns:
            The matching resource's properties, including blazar_resource_id, if found.
        """
        host_list_response = call_blazar(
            context,
            self.resource_path,
            method="get",
            json={},
        )
        host_list = host_list_response.get(f"{self.resource_type}s")
        matching_host = next(
            (host for host in host_list if host.get("uid") == hw_uuid),
            None,
        )
        return matching_host
