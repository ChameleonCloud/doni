"""Sync worker to update Blazar from Doni."""
from typing import TYPE_CHECKING

from oslo_log import log

from doni.common import args
from doni.driver.worker.blazar import BaseBlazarWorker, call_blazar
from doni.objects.availability_window import AvailabilityWindow
from doni.worker import WorkerField

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.hardware import Hardware


LOG = log.getLogger(__name__)

PLACEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "rack": args.STRING,
        "node": args.STRING,
    },
    "additionalProperties": False,
}


class BlazarPhysicalHostWorker(BaseBlazarWorker):
    """This class handles the synchronization of physical hosts from Doni to Blazar."""

    resource_type = "host"
    resource_path = "/os-hosts"
    resource_pk = "hypervisor_hostname"

    fields = BaseBlazarWorker.fields + [
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

    def expected_state(self, hardware: "Hardware", host_dict: "dict") -> dict:
        hw_props = hardware.properties
        placement_props = hw_props.get("placement", {})
        host_dict.update({"uid": hardware.uuid, "node_name": hardware.name})

        # FIXME(jason): Currently Blazar does not allow deleting extra capabilities,
        # and setting to None triggers errors in Blazar during create/update.
        # Until Blazar supports this, ignore nulled out fields. This means that we
        # cannot unset extra capabilities in Blazar, but that is already a problem
        # with Blazar's API itself. When that supports deletes, this can be updated.
        if hw_props.get("node_type"):
            host_dict["node_type"] = hw_props.get("node_type")
        if hw_props.get("cpu_arch"):
            host_dict["cpu_arch"] = hw_props.get("cpu_arch")
        if hw_props.get("su_factor"):
            host_dict["su_factor"] = hw_props.get("su_factor")
        if placement_props.get("node"):
            host_dict["placement.node"] = placement_props.get("node")
        if placement_props.get("rack"):
            host_dict["placement.rack"] = placement_props.get("rack")

        return host_dict

    @classmethod
    def to_reservation_values(cls, hardware_uuid: str) -> dict:
        return {
            "resource_type": "physical:host",
            "min": 1,
            "max": 1,
            "hypervisor_properties": None,
            "resource_properties": f'["==","$uid","{hardware_uuid}"]',
        }

    def import_existing(self, context: "RequestContext"):
        existing_hosts = []
        for host in call_blazar(context, self.resource_path)[f"{self.resource_type}s"]:
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
