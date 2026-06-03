import re
import time
from collections import abc
from functools import wraps
from typing import TYPE_CHECKING

import jsonpatch
from oslo_log import log

from doni.common import args, exception, keystone
from doni.conf import auth as auth_conf
from doni.driver.util import KeystoneServiceAPIError, ks_service_requestor
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerField, WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

LOG = log.getLogger(__name__)

IRONIC_API_VERSION = "1"
# 1.51 provides a "description" field for the node, which we may want to use.
# It is supported in Stein and later[1].
# https://docs.openstack.org/ironic/latest/contributor/webapi-version-history.html#id13
IRONIC_API_MICROVERSION = "1.51"
PROVISION_STATE_TIMEOUT = 60  # Seconds to wait for provision_state changes
_IRONIC_ADAPTER = None

# Ironic's provision state API takes target arguments that
# are annoyingly different than the state the node ultimately winds up in.
IRONIC_STATE_TARGETS = {
    "manageable": "manage",
    "available": "provide",
}

IRONIC_NODE_CAPABILITIES_SCHEMA = {
    "type": "object",
    "properties": {
        "boot_option": {
            "type": "string",
            "enum": ["local", "netboot", "ramdisk", "kickstart"],
        },
        "boot_mode": {"type": "string", "enum": ["bios", "uefi"]},
        "secure_boot": {"type": "string", "enum": ["true", "false"]},
        "trusted_boot": {"type": "string", "enum": ["true", "false"]},
        "disk_label": {"type": "string", "enum": ["msdos", "gpt"]},
    },
    "additionalProperties": False,
}

MASKED_VALUE_REGEX = re.compile("^\*+$")


def _get_ironic_adapter():
    global _IRONIC_ADAPTER
    if not _IRONIC_ADAPTER:
        _IRONIC_ADAPTER = keystone.get_adapter(
            "ironic",
            session=keystone.get_session("ironic"),
            auth=keystone.get_auth("ironic"),
            version=IRONIC_API_VERSION,
        )
    return _IRONIC_ADAPTER


def _defer_on_node_locked(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeystoneServiceAPIError as exc:
            if exc.code == 409:
                return WorkerResult.Defer(reason="Node is locked.")
            raise

    return wrapper


class IronicNodeProvisionStateTimeout(exception.DoniException):
    _msg_fmt = (
        "Ironic node %(node)s timed out updating its provision state to %(state)s"
    )


class IronicWorker(BaseWorker):

    fields = [
        WorkerField(
            "baremetal_driver",
            schema=args.enum(["ipmi"]),
            default="ipmi",
            private=True,
            description=(
                "The Ironic hardware driver that will control this node. See "
                "https://docs.openstack.org/ironic/latest/admin/drivers.html "
                "for a list of all Ironic hardware types. "
                "Currently only the 'ipmi' driver is supported."
            ),
        ),
        WorkerField(
            "baremetal_resource_class",
            schema=args.STRING,
            default="baremetal",
            private=True,
            description=(
                "The Ironic node resource class, which is used to map instance "
                "launch requests onto specific nodes via different Nova flavors. "
                "See https://docs.openstack.org/ironic/latest/install/configure-nova-flavors.html "
                "for more information. Defaults to 'baremetal', a generic resource "
                "class."
            ),
        ),
        WorkerField(
            "baremetal_deploy_kernel_image",
            schema=args.UUID,
            private=True,
            description=(
                "The Glance UUID for the Ironic deployment kernel image to use "
                "when preparing the host for the user-provided OS image. If "
                "not set, a default value configured by Ironic will be used."
            ),
        ),
        WorkerField(
            "baremetal_deploy_ramdisk_image",
            schema=args.UUID,
            private=True,
            description=(
                "The Glance UUID for the Ironic deployment ramdisk image to "
                "use when preparing the host for the user-provided OS image. "
                "If not set, a default value configured by Ironic will be used."
            ),
        ),
        WorkerField(
            "baremetal_capabilities",
            schema=IRONIC_NODE_CAPABILITIES_SCHEMA,
            private=True,
            description=(
                "Additional Ironic capabilities to set on the node. See "
                "https://docs.openstack.org/ironic/latest/_modules/ironic/drivers/modules/deploy_utils.html "
                "for a list of supported capabilities in SUPPORTED_CAPABILITIES."
            ),
        ),
        WorkerField(
            "ipmi_username",
            schema=args.STRING,
            private=True,
            description=(
                "The IPMI username to use for IPMI authentication. Only used "
                "if the ``baremetal_driver`` is 'ipmi'."
            ),
        ),
        WorkerField(
            "ipmi_password",
            schema=args.STRING,
            private=True,
            sensitive=True,
            description=(
                "The IPMI password to use for IPMI authentication. Only used "
                "if the ``baremetal_driver`` is 'ipmi'."
            ),
        ),
        WorkerField(
            "ipmi_port",
            schema=args.PORT_RANGE,
            private=True,
            description=(
                "The remote IPMI RMCP port. If not provided, this defaults to "
                "the default IPMI port of 623."
            ),
        ),
        WorkerField(
            "ipmi_terminal_port",
            schema=args.PORT_RANGE,
            private=True,
            description=(
                "A local port to use to provide a remote console for provisioners "
                "of the Ironic node. Each node should have its own free unique "
                "port on the host running Ironic."
            ),
        ),
    ]

    opts = []
    opt_group = "ironic"

    def register_opts(self, conf):
        conf.register_opts(self.opts, group=self.opt_group)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="baremetal")

    def list_opts(self):
        return auth_conf.add_auth_opts(self.opts, service_type="baremetal")

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        hw_props = hardware.properties
        hw_capabilities = hw_props.get("baremetal_capabilities")

        if isinstance(hw_capabilities, abc.Mapping) and hw_capabilities:
            capabilities_string = ",".join(
                [f"{key}:{value}" for key, value in hw_capabilities.items()]
            )
        else:
            capabilities_string = None

        desired_state = {
            "uuid": hardware.uuid,
            "name": hardware.name,
            "driver": hw_props.get("baremetal_driver"),
            "driver_info": {
                "ipmi_address": hw_props.get("management_address"),
                "ipmi_username": hw_props.get("ipmi_username"),
                "ipmi_password": hw_props.get("ipmi_password"),
                "ipmi_port": hw_props.get("ipmi_port"),
                "ipmi_terminal_port": hw_props.get("ipmi_terminal_port"),
                "deploy_kernel": hw_props.get("baremetal_deploy_kernel_image"),
                "deploy_ramdisk": hw_props.get("baremetal_deploy_ramdisk_image"),
            },
            "resource_class": hw_props.get("baremetal_resource_class"),
            "properties": {
                "capabilities": capabilities_string,
                "cpu_arch": hw_props.get("cpu_arch"),
            },
        }
        desired_interfaces = hw_props.get("interfaces", [])

        existing = _call_ironic(
            context, f"/nodes/{hardware.uuid}", method="get", allowed_error_codes=[404]
        )

        # if allowed_error_codes are specified, existing may contain an `error_message` key
        # instead, test for a UUID, as if a baremetal node is found, it MUST have one
        if not existing.get("uuid"):
            payload = _do_node_create(context, desired_state)
            _do_port_updates(context, hardware.uuid, desired_interfaces)
            return WorkerResult.Success(payload)

        if existing.get("maintenance"):
            # For operator sanity, avoid mutating any details about the node
            # if it is maintenance mode. It is likely this will fail anyways
            # if the node is in maintenance.
            # NOTE: there may be a future case where the 'maintenance' flag
            # is managed in the inventory, in which case this will have to change.
            return WorkerResult.Defer(
                reason=(
                    "Node is in maintenance mode. Please take the node "
                    "out of maintenance to apply this update."
                )
            )

        payload = _do_node_update(context, existing, desired_state)
        _do_port_updates(context, hardware.uuid, desired_interfaces)
        return WorkerResult.Success(payload)

    def import_existing(self, context):
        existing_nodes = []
        for node in _call_ironic(context, "/nodes?detail=True")["nodes"]:
            uuid = node["uuid"]
            driver_info = node["driver_info"]
            properties = node["properties"]

            if MASKED_VALUE_REGEX.match(driver_info.get("ipmi_password", "")):
                LOG.warning(
                    (
                        f"Node {uuid} has masked IPMI password. Please "
                        "reconfigure Ironic to allow showing secrets for admin "
                        "requests: https://docs.openstack.org/ironic/latest/admin/security.html"
                    )
                )
                continue

            interfaces = []
            for port in _call_ironic(context, f"/ports?node={uuid}&detail=True")[
                "ports"
            ]:
                port_llc = port["local_link_connection"]
                interfaces.append(
                    {
                        "name": port["extra"].get("name", port["uuid"]),
                        "mac_address": port["address"],
                        "switch_id": port_llc.get("switch_id"),
                        "switch_port_id": port_llc.get("port_id"),
                        "switch_info": port_llc.get("switch_info"),
                    }
                )

            existing_nodes.append(
                {
                    "uuid": uuid,
                    "name": node["name"],
                    "properties": {
                        "baremetal_driver": node["driver"],
                        "baremetal_resource_class": node["resource_class"],
                        "baremetal_deploy_kernel_image": driver_info.get(
                            "deploy_kernel"
                        ),
                        "baremetal_deploy_ramdisk_image": driver_info.get(
                            "deploy_ramdisk"
                        ),
                        "baremetal_capabilities": properties.get("capabilities"),
                        "cpu_arch": properties.get("cpu_arch"),
                        "management_address": driver_info["ipmi_address"],
                        "interfaces": interfaces,
                        "ipmi_username": driver_info["ipmi_username"],
                        "ipmi_password": driver_info["ipmi_password"],
                        "ipmi_port": driver_info.get("ipmi_port"),
                        "ipmi_terminal_port": driver_info.get("ipmi_terminal_port"),
                    },
                }
            )
        return existing_nodes


def _do_node_create(context, desired_state) -> dict:
    node = _call_ironic(context, "/nodes", method="post", json=desired_state)
    # Move from enroll -> manageable (Ironic will perform verification)
    wait_for_provision_state(context, node["uuid"], target_state="manageable")
    # Move from manageable -> available
    wait_for_provision_state(context, node["uuid"], target_state="available")

    return _success_payload(node)


def _do_node_update(context, ironic_node, desired_state) -> dict:
    node_uuid = ironic_node["uuid"]

    existing_state = {key: ironic_node.get(key) for key in desired_state.keys()}
    _normalize_for_patch(existing_state["driver_info"], desired_state["driver_info"])
    _normalize_for_patch(
        existing_state["properties"] or {}, desired_state["properties"] or {}
    )
    patch = jsonpatch.make_patch(existing_state, desired_state)

    if not patch:
        return _success_payload(ironic_node)

    # Nodes must be in 'manageable' state to change driver
    # TODO: we can tell by what kind of diff we need whether this is
    # actually required.
    if ironic_node["provision_state"] != "manageable":
        wait_for_provision_state(context, node_uuid, target_state="manageable")

    updated = _call_ironic(
        context, f"/nodes/{node_uuid}", method="patch", json=list(patch)
    )

    # Put back into available state
    wait_for_provision_state(context, node_uuid, target_state="available")

    return _success_payload(updated)


def _do_port_updates(context, ironic_uuid, interfaces) -> dict:
    ports = _call_ironic(context, f"/ports?node={ironic_uuid}&detail=True")["ports"]
    ports_by_mac = {p["address"].lower(): p for p in ports}
    ifaces_by_mac = {i["mac_address"].lower(): i for i in interfaces}
    existing = set(ports_by_mac.keys())
    # Ignore interfaces not marked as enabled
    desired = set(
        mac for mac, iface in ifaces_by_mac.items() if iface.get("enabled", True)
    )

    def _desired_port_state(iface):
        body = {
            "extra": {
                "name": iface.get("name"),
            },
            "local_link_connection": {},
            "pxe_enabled": iface.get("pxe_enabled", True),
        }

        if iface.get("switch_id") and iface.get("switch_port_id"):
            body["local_link_connection"] = {
                "switch_id": iface.get("switch_id"),
                "port_id": iface.get("switch_port_id"),
                "switch_info": iface.get("switch_info", ""),
            }

        return body

    ifaces_to_add = desired - existing
    ifaces_to_update = desired & existing
    ifaces_to_remove = existing - desired
    needs_managable = False
    if ifaces_to_add or ifaces_to_update or ifaces_to_remove:
        needs_managable = True
        wait_for_provision_state(context, ironic_uuid, target_state="manageable")

    for iface_to_add in ifaces_to_add:
        iface = ifaces_by_mac[iface_to_add]
        body = {"node_uuid": ironic_uuid, "address": iface["mac_address"]}
        body.update(_desired_port_state(iface))
        port = _call_ironic(context, "/ports", method="post", json=body)
        LOG.info(f"Created port {port['uuid']} for node {ironic_uuid}")

    for iface_to_update in ifaces_to_update:
        port = ports_by_mac[iface_to_update]
        existing_state = {
            k: port[k] for k in ["extra", "local_link_connection", "pxe_enabled"]
        }
        desired_state = _desired_port_state(ifaces_by_mac[iface_to_update])
        _normalize_for_patch(existing_state["extra"], desired_state["extra"])
        _normalize_for_patch(
            existing_state["local_link_connection"],
            desired_state["local_link_connection"],
        )
        patch = jsonpatch.make_patch(existing_state, desired_state)
        if not patch:
            continue
        _call_ironic(
            context, f"/ports/{port['uuid']}", method="patch", json=list(patch)
        )
        LOG.info(f"Updated port {port['uuid']} for node {ironic_uuid}")

    for iface_to_remove in ifaces_to_remove:
        port_uuid = ports_by_mac[iface_to_remove]["uuid"]
        _call_ironic(context, f"/ports/{port_uuid}", method="delete")
        LOG.info(f"Deleted port {port_uuid} for node {ironic_uuid}")

    if needs_managable:
        wait_for_provision_state(context, ironic_uuid, target_state="available")

def _success_payload(node):
    # This 'created_at' isn't really used for anything but may provide comfort
    return {
        "created_at": node.get("created_at"),
    }


def _normalize_for_patch(existing, desired):
    # Copy unknown or empty keys from existing state to avoid overwriting w/ patch.
    for key in existing.keys():
        desired.setdefault(key, existing[key])

    for key in list(desired.keys()):
        # Treat "None" as explicitly removing
        if desired.get(key) is None:
            desired.pop(key, None)
            # Remove keys from each if they evaluate to None; this prevents a desired
            # 'None' from being sent to Ironic if it already has no value for that key.
            if existing.get(key) is None:
                existing.pop(key, None)



def wait_for_provision_state(context, node_uuid, target_state=None, timeout=PROVISION_STATE_TIMEOUT):
    try:
        _wait_for_provision_state(context, node_uuid, target_state, timeout)
    except IronicNodeProvisionStateTimeout as exc:
        LOG.debug(f"{exc}")
        return WorkerResult.Defer(reason="Failed to change provision state.")
    except KeystoneServiceAPIError as exc:
        if exc.code == 400:
            return WorkerResult.Defer(reason="Invalid State Transition.")
        elif exc.code == 409:
            return WorkerResult.Defer(reason="Node is locked.")
        else:
            raise exc


def _wait_for_provision_state(
    context, node_uuid, target_state=None, timeout=PROVISION_STATE_TIMEOUT
):
    #initialize current state
    node = _call_ironic(context, f"/nodes/{node_uuid}", method="get")
    provision_state = node.get("provision_state")

    #only try to set state if we're not there already
    if provision_state != target_state:
        _call_ironic(
            context,
            f"/nodes/{node_uuid}/states/provision",
            method="put",
            json={"target": IRONIC_STATE_TARGETS[target_state]},
        )
        start_time = time.perf_counter()
        provision_state = None
        while provision_state != target_state:
            if (time.perf_counter() - start_time) > timeout:
                raise IronicNodeProvisionStateTimeout(node=node_uuid, state=target_state)
            time.sleep(15)
            node = _call_ironic(context, f"/nodes/{node_uuid}", method="get")
            provision_state = node["provision_state"]


def _call_ironic(*args, **kwargs):
    microversion = kwargs.pop("microversion", IRONIC_API_MICROVERSION)
    return ks_service_requestor("Ironic", _get_ironic_adapter, microversion=microversion)(*args, **kwargs)
