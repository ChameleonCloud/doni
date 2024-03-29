from datetime import datetime
from typing import TYPE_CHECKING

from dateutil.parser import parse
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import uuidutils
from pytz import UTC

from doni.common import args, exception, keystone
from doni.conf import auth as auth_conf
from doni.driver.util import ks_service_requestor, KeystoneServiceAPIError
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

LOG = logging.getLogger(__name__)

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
BLAZAR_DATE_FORMAT = "%Y-%m-%d %H:%M"
_BLAZAR_ADAPTER = None
_KEYSTONE_ADAPTER = None

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


def _get_keystone_adapter():
    """Adapter for calling the Keystone service itself (e.g., to look up projects)"""
    global _KEYSTONE_ADAPTER
    if not _KEYSTONE_ADAPTER:
        _KEYSTONE_ADAPTER = keystone.get_adapter(
            "keystone_authtoken",
            session=keystone.get_session("keystone_authtoken"),
            auth=keystone.get_auth("keystone_authtoken"),
            version=3,
        )
    return _KEYSTONE_ADAPTER


def call_keystone(*args, **kwargs):
    return ks_service_requestor("Keystone", _get_keystone_adapter)(*args, **kwargs)


class BaseBlazarWorker(BaseWorker):
    """A base Blazar worker that syncs a Hardware to some Blazar resource.

    The base worker also handles managing availability windows for the resource.
    """

    opts = []
    opt_group = "blazar"

    # How will the resource be looked up?
    resource_pk = "name"

    fields = [
        WorkerField(
            "authorized_projects",
            schema=args.array(args.STRING),
            description=(
                "Only users in projects specified in this list will be able to reserve "
                "the resource. Specify project names or IDs."
            ),
        ),
        WorkerField(
            "authorized_projects_reason",
            schema=args.STRING,
            description=(
                "An optional display reason to explain why the resource is restricted "
                "to only certain projects."
            ),
        ),
    ]

    def register_opts(self, conf):
        super().register_opts(conf)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="reservation")
        # Also register the keystone_authtoken group explicitly. The worker does not
        # initialize keystonemiddleware (b/c it doesn't use it), which normally would
        # be registering these options for us.
        try:
            auth_conf.register_auth_opts(
                conf, "keystone_authtoken", service_type="identity"
            )
        except cfg.DuplicateOptError:
            # Options already registered (happens when the API loads this worker itself)
            pass

    def list_opts(self):
        # We don't need to add keystone opts here; `add_auth_opts` just pulls common
        # auth options out to the flattened option list. This is only used by
        # oslo-config-generator.
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
    def to_resource_pk(cls, hardware: "Hardware") -> str:
        return hardware.uuid

    @classmethod
    def to_reservation_values(cls, hardware_uuid: str) -> dict:
        raise NotImplementedError()

    @classmethod
    def expected_state(cls, hardware: "Hardware", state: "dict") -> dict:
        """Compute the desired state of Blazar resource properties.

        Args:
            hardware (Hardware): the hardware item.
            state (dict): the state object. This will contain some base/generic state,
                which concrete classes can extend or override as needed.

        Returns:
            a dict of the desired state in Blazar.
        """
        raise NotImplementedError()

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        resource_id = state_details.get("blazar_resource_id")

        # For deletions, we need to go in reverse order; ensure we clean up the
        # availability window leases before deleting the resource in Blazar.
        if hardware.deleted:
            result = self.process_availability_windows(
                context, hardware, availability_windows, WorkerResult.Success()
            )
            if not isinstance(result, WorkerResult.Success):
                return result
            if not resource_id:
                raise BlazarIsWrongError(
                    (
                        f"Tried to delete resource for {hardware.uuid}, but no record of "
                        "matching Blazar resource"
                    )
                )
            # TODO: Are there any other leases? Try to detect this (is there a specific
            # error message in Blazar?) and return a deferred result if so.
            self._resource_delete(context, resource_id)
            return WorkerResult.Success(
                {
                    "blazar_resource_id": None,
                    "resource_created_at": None,
                    "resource_deleted_at": datetime.utcnow(),
                }
            )

        expected_state = {}
        # Populate base fields
        hw_props = hardware.properties
        if hw_props.get("authorized_projects") is not None:
            deref_projects = []
            for project_ref in hw_props["authorized_projects"]:
                if uuidutils.is_uuid_like(project_ref):
                    deref_projects.append(project_ref)
                else:
                    matching = call_keystone(context, f"/projects?name={project_ref}")[
                        "projects"
                    ]
                    if matching:
                        deref_projects.append(matching[0]["id"])
                    else:
                        LOG.warning(
                            f"Failed to look up authorized_project '{project_ref}' by name"
                        )

            expected_state["authorized_projects"] = ",".join(deref_projects)
        if hw_props.get("authorized_projects_reason") is not None:
            expected_state["restricted_reason"] = hw_props["authorized_projects_reason"]

        # Allow concrete classes to extend state
        expected_state = self.expected_state(hardware, expected_state)

        if resource_id:
            result = self._resource_update(context, resource_id, expected_state)
        else:
            # Without a cached resource_id, try to create a host. If the host exists,
            # blazar will match the uuid, and the request will fail.
            result = self._resource_create(
                context, self.to_resource_pk(hardware), expected_state
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

    def _resource_create(self, context, name, expected_state) -> WorkerResult.Base:
        """Attempt to create new host in blazar."""
        result = {}
        try:
            body = expected_state.copy()
            body[self.resource_pk] = name
            resource = call_blazar(
                context,
                self.resource_path,
                method="post",
                json=body,
            ).get(self.resource_type)
        except KeystoneServiceAPIError as exc:
            if exc.code == 404:
                return WorkerResult.Defer(
                    reason=(
                        "Can not make resource reservable, as the underlying entity "
                        "could not be found."
                    )
                )
            elif exc.code == 409:
                resource = self._find_resource(context, name)
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
                return WorkerResult.Defer(result, reason="Resource not found")
            elif exc.code == 409:
                # Host cannot be updated, referenced by current lease
                return WorkerResult.Defer(
                    result, reason="Active leases exist for resource"
                )

            raise  # Unhandled exception
        else:
            # On success, cache resource_id and updated time
            result["blazar_resource_id"] = expected_state.get("id")
            result["resource_updated_at"] = expected_state.get("updated_at")
            return WorkerResult.Success(result)

    def _resource_delete(self, context: "RequestContext", resource_id: "str"):
        call_blazar(context, f"{self.resource_path}/{resource_id}", method="delete")

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

    def _find_resource(self, context: "RequestContext", name: "str") -> dict:
        """Look up resource in blazar by name.

        If the blazar resource id is unknown or otherwise incorrect, the only option
        is to get the list of all resources from blazar, then search for matching
        name.

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
            (host for host in host_list if host.get(self.resource_pk) == name),
            None,
        )
        return matching_host
