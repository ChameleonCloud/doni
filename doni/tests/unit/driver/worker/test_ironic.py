import time
from operator import itemgetter
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from keystoneauth1 import loading as ks_loading
from oslo_utils import uuidutils

from doni.driver.worker.ironic import (
    PROVISION_STATE_TIMEOUT,
    IronicNodeProvisionStateTimeout,
    IronicWorker,
)
from doni.objects.hardware import Hardware
from doni.tests.unit import utils
from doni.worker import WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext


TEST_HARDWARE_UUID = uuidutils.generate_uuid()


@pytest.fixture
def ironic_worker(test_config):
    """Generate a test IronicWorker and ensure the environment is configured for it.

    Much of this is black magic to appease the gods of oslo_config.
    """
    # Configure the app to use a hardware type valid for this worker.
    test_config.config(
        enabled_hardware_types=["baremetal"], enabled_worker_types=["ironic"]
    )

    worker = IronicWorker()
    worker.register_opts(test_config)
    # NOTE(jason):
    # At application runtime, Keystone auth plugins are registered dynamically
    # depending on what auth_type is provided in the config. I'm not sure how
    # it's possible to even express that here, as there's a chicken-or-egg
    # question of how you set the auth_type while it's registering all the
    # auth options. So we register the manually here.
    plugin = ks_loading.get_plugin_loader("v3password")
    opts = ks_loading.get_auth_plugin_conf_options(plugin)
    test_config.register_opts(opts, group=worker.opt_group)

    test_config.config(
        group="ironic",
        auth_type="v3password",
        auth_url="http://localhost:5000",
        username="fake-username",
        user_domain_name="fake-user-domain-name",
        password="fake-password",
        project_name="fake-project-name",
        project_domain_name="fake-project-domain-name",
    )
    return worker


def get_fake_ironic(mocker, request_fn):
    mock_adapter = mock.MagicMock()
    mock_request = mock_adapter.request
    mock_request.side_effect = request_fn
    mocker.patch(
        "doni.driver.worker.ironic._get_ironic_adapter"
    ).return_value = mock_adapter
    return mock_request


def _apply_overrides(defaults: "dict", overrides: "dict") -> "dict":
    updated = defaults.copy()
    for key, value in overrides.items():
        if isinstance(value, dict):
            updated[key].update(value)
        else:
            updated[key] = value
    return updated


def get_fake_hardware(database: "utils.DBFixtures", prop_overrides={}):
    properties = {
        "baremetal_driver": "fake-driver",
        "baremetal_resource_class": "fake-resource_class",
        "baremetal_deploy_kernel_image": "fake-deploy_kernel_image",
        "baremetal_deploy_ramdisk_image": "fake-deploy_ramdisk_image",
        "baremetal_capabilities": {"boot_mode": "bios"},
        "cpu_arch": "fake-cpu_arch",
        "management_address": "fake-management_address",
        "ipmi_username": "fake-ipmi_username",
        "ipmi_password": "fake-ipmi_password",
        "ipmi_port": 123,
        "ipmi_terminal_port": 50123,
        "interfaces": [
            {
                "name": "fake-iface1_name",
                "mac_address": "00:00:00:00:00:00",
                "switch_id": "fake-switch_id",
                "switch_port_id": "fake-switch_port_id",
                "switch_info": "fake-switch_info",
                "pxe_enabled": True,
                "enabled": True,
            },
            {
                "name": "fake-iface2_name",
                "mac_address": "00:00:00:00:00:01",
                "pxe_enabled": False,
                "enabled": False,
            },
        ],
    }
    properties = _apply_overrides(properties, prop_overrides)

    db_hw = database.add_hardware(
        uuid=TEST_HARDWARE_UUID,
        name="fake-name",
        hardware_type="baremetal",
        properties=properties,
    )
    return Hardware(**db_hw)


def ironic_expected_node_body(hardware: "Hardware", overrides={}):
    props = hardware.properties

    driver_info = {
        "ipmi_address": props["management_address"],
        "ipmi_username": props["ipmi_username"],
        "ipmi_password": props["ipmi_password"],
        "ipmi_port": props["ipmi_port"],
        "ipmi_terminal_port": props["ipmi_terminal_port"],
    }
    if props["baremetal_deploy_kernel_image"]:
        driver_info["deploy_kernel"] = props["baremetal_deploy_kernel_image"]
    if props["baremetal_deploy_ramdisk_image"]:
        driver_info["deploy_ramdisk"] = props["baremetal_deploy_ramdisk_image"]

    node_properties = {
        "cpu_arch": props["cpu_arch"],
    }
    if props["baremetal_capabilities"]:
        node_properties["capabilities"] = ",".join(
            [f"{key}:{value}" for key, value in props["baremetal_capabilities"].items()]
        )

    node_body = {
        "uuid": hardware.uuid,
        "name": hardware.name,
        "created_at": "fake-created_at",  # Will be generated by Ironic
        "maintenance": False,
        "provision_state": "available",
        "driver": props["baremetal_driver"],
        "driver_info": driver_info,
        "resource_class": props["baremetal_resource_class"],
        "properties": node_properties,
    }

    for field, value in overrides.items():
        if isinstance(value, dict):
            node_body[field].update(value)
        else:
            node_body[field] = value

    return node_body


def ironic_expected_port_body(hardware: "Hardware", iface_idx=None, overrides={}):
    hw_iface = hardware.properties["interfaces"][iface_idx]
    if "switch_id" in hw_iface:
        local_link_connection = {
            "switch_id": hw_iface["switch_id"],
            "port_id": hw_iface["switch_port_id"],
            "switch_info": hw_iface["switch_info"],
        }
    else:
        local_link_connection = {}
    port_body = {
        "uuid": "fake-port_uuid",
        "address": hw_iface["mac_address"],
        "extra": {"name": hw_iface["name"]},
        "local_link_connection": local_link_connection,
        "pxe_enabled": hw_iface.get("pxe_enabled", True),
    }
    return port_body


def test_ironic_create_node(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that new nodes are created if not already existing."""
    get_node_count = 0
    patch_node_count = 0
    fake_hw = get_fake_hardware(database)

    def _fake_ironic_for_create(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            nonlocal get_node_count
            get_node_count += 1
            if get_node_count == 1:
                return utils.MockResponse(404)
            elif get_node_count == 2:
                return utils.MockResponse(200, {"provision_state": "manageable"})
            elif get_node_count == 3:
                return utils.MockResponse(200, {"provision_state": "available"})
        elif method == "post" and path == f"/nodes":
            assert json["uuid"] == TEST_HARDWARE_UUID
            assert json["name"] == "fake-name"
            assert json["driver"] == "fake-driver"
            assert json["driver_info"] == {
                "ipmi_address": "fake-management_address",
                "ipmi_username": "fake-ipmi_username",
                "ipmi_password": "fake-ipmi_password",
                "ipmi_terminal_port": 50123,
                "ipmi_port": 123,
                "deploy_kernel": "fake-deploy_kernel_image",
                "deploy_ramdisk": "fake-deploy_ramdisk_image",
            }
            assert json["resource_class"] == "fake-resource_class"
            return utils.MockResponse(
                201, {"uuid": TEST_HARDWARE_UUID, "created_at": "fake-created_at"}
            )
        elif (
            method == "put" and path == f"/nodes/{TEST_HARDWARE_UUID}/states/provision"
        ):
            nonlocal patch_node_count
            patch_node_count += 1
            if patch_node_count == 1:
                provision_state = "manage"
            elif patch_node_count == 2:
                provision_state = "provide"
            assert json == {"target": provision_state}
            return utils.MockResponse(200, {})
        elif (
            method == "get" and path == f"/ports?node={TEST_HARDWARE_UUID}&detail=True"
        ):
            return utils.MockResponse(200, {"ports": []})
        elif method == "post" and path == "/ports":
            assert json["extra"] == {"name": "fake-iface1_name"}
            assert json["address"] == "00:00:00:00:00:00"
            assert json["local_link_connection"] == {
                "switch_id": "fake-switch_id",
                "port_id": "fake-switch_port_id",
                "switch_info": "fake-switch_info",
            }
            return utils.MockResponse(200, {"uuid": "fake-port_uuid"})
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    # 'sleep' is used to wait for provision state changes
    mocker.patch("time.sleep")
    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_create)

    result = ironic_worker.process(admin_context, fake_hw)

    assert isinstance(result, WorkerResult.Success)
    assert result.payload == {"created_at": "fake-created_at"}
    # call 1 = check that node does not exist
    # call 2 = create the node
    # call 3 = patch the node to 'manageable' state
    # call 4 = get the node to see if state changed
    # call 5 = patch the node to 'available' state
    # call 6 = get the node to see if state changed
    # call 7 = list ports for update
    # call 8 = add port
    assert fake_ironic.call_count == 8


def test_ironic_update_node(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that existing nodes are patched from hardware properties."""
    get_node_count = 0
    fake_hw = get_fake_hardware(database)

    def _fake_ironic_for_update(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            nonlocal get_node_count
            get_node_count += 1
            if get_node_count == 1:
                provision_state = "manageable"
            else:
                provision_state = "available"
            return utils.MockResponse(
                200,
                ironic_expected_node_body(
                    fake_hw,
                    {
                        "provision_state": provision_state,
                        "driver_info": {"ipmi_address": "REPLACE-ipmi_address"},
                    },
                ),
            )
        elif method == "patch" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            # Validate patch for node properties
            assert json == [
                {
                    "op": "replace",
                    "path": "/driver_info/ipmi_address",
                    "value": "fake-management_address",
                }
            ]
            return utils.MockResponse(
                200,
                {
                    "uuid": TEST_HARDWARE_UUID,
                    "created_at": "fake-created_at",
                },
            )
        elif (
            method == "put" and path == f"/nodes/{TEST_HARDWARE_UUID}/states/provision"
        ):
            assert json == {"target": "provide"}
            return utils.MockResponse(200)
        elif (
            method == "get" and path == f"/ports?node={TEST_HARDWARE_UUID}&detail=True"
        ):
            return utils.MockResponse(
                200,
                {"ports": [ironic_expected_port_body(fake_hw, iface_idx=0)]},
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    # 'sleep' is used to wait for provision state changes
    mocker.patch("time.sleep")
    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_update)

    result = ironic_worker.process(admin_context, fake_hw)

    assert isinstance(result, WorkerResult.Success)
    assert result.payload == {"created_at": "fake-created_at"}
    # call 1 = get the node
    # call 2 = patch the node's properties
    # call 3 = patch the node back to 'available' state
    # call 4 = get the node to see if state changed
    # call 5 = list ports for update
    assert fake_ironic.call_count == 5


def test_ironic_update_defer_on_maintenance(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that nodes in maintenance mode are not updated."""

    def _fake_ironic_for_maintenance(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            return utils.MockResponse(
                200,
                {
                    "uuid": TEST_HARDWARE_UUID,
                    "maintenance": True,
                },
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_maintenance)

    result = ironic_worker.process(admin_context, get_fake_hardware(database))

    assert isinstance(result, WorkerResult.Defer)
    assert "in maintenance" in result.reason
    assert fake_ironic.call_count == 1


def test_ironic_provision_state_timeout(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that nodes in maintenance mode are not updated."""

    def _fake_ironic_for_timeout(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            return utils.MockResponse(
                200,
                {
                    "uuid": TEST_HARDWARE_UUID,
                    "maintenance": False,
                    "provision_state": "available",
                    "driver_info": {},
                },
            )
        elif (
            method == "put" and path == f"/nodes/{TEST_HARDWARE_UUID}/states/provision"
        ):
            assert json == {"target": "manage"}
            return utils.MockResponse(200)
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    count = int(time.perf_counter())

    def _fake_perf_counter():
        nonlocal count
        count += 15
        return count

    mocker.patch("time.perf_counter").side_effect = _fake_perf_counter
    mocker.patch("time.sleep")

    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_timeout)

    with pytest.raises(IronicNodeProvisionStateTimeout):
        ironic_worker.process(admin_context, get_fake_hardware(database))
    # 1. call to get node
    # 2. call to update provision state
    # 3..n calls to poll state until timeout
    assert fake_ironic.call_count == 2 + (PROVISION_STATE_TIMEOUT / 15)


def test_ironic_update_defer_on_locked(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that nodes in locked state are deferred."""

    def _fake_ironic_for_locked(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            return utils.MockResponse(409)
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_locked)

    result = ironic_worker.process(admin_context, get_fake_hardware(database))

    assert isinstance(result, WorkerResult.Defer)
    assert "is locked" in result.reason
    assert fake_ironic.call_count == 1


def test_ironic_skips_update_on_empty_patch(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    """Test that nodes w/ no differences do not get updated."""
    fake_hw = get_fake_hardware(database)

    def _fake_ironic_for_noop_update(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            return utils.MockResponse(200, ironic_expected_node_body(fake_hw))
        elif (
            method == "get" and path == f"/ports?node={TEST_HARDWARE_UUID}&detail=True"
        ):
            return utils.MockResponse(
                200,
                {"ports": [ironic_expected_port_body(fake_hw, iface_idx=0)]},
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_noop_update)

    result = ironic_worker.process(admin_context, fake_hw)

    assert isinstance(result, WorkerResult.Success)
    assert result.payload == {"created_at": "fake-created_at"}
    assert fake_ironic.call_count == 2


def test_ironic_port_update_ignores_empty_switch_params(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    # Create a hardware w/o some switch interface information
    fake_hw = get_fake_hardware(
        database, {"interfaces": [{"name": "eno1", "mac_address": "00:00:00:00:00:00"}]}
    )

    def _fake_ironic_for_update(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            return utils.MockResponse(200, ironic_expected_node_body(fake_hw))
        elif (
            method == "get" and path == f"/ports?node={TEST_HARDWARE_UUID}&detail=True"
        ):
            return utils.MockResponse(
                200,
                {"ports": [ironic_expected_port_body(fake_hw, iface_idx=0)]},
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_update)

    result = ironic_worker.process(admin_context, fake_hw)

    assert isinstance(result, WorkerResult.Success)
    # call 1 = get the node
    # call 2 = list ports for update
    assert fake_ironic.call_count == 2


def test_ironic_update_remove_optional_fields(
    mocker,
    admin_context: "RequestContext",
    ironic_worker: "IronicWorker",
    database: "utils.DBFixtures",
):
    get_node_count = 0
    fake_hw = get_fake_hardware(
        database,
        {
            "baremetal_deploy_kernel_image": None,
            "baremetal_deploy_ramdisk_image": None,
        },
    )

    def _fake_ironic_for_update(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            nonlocal get_node_count
            get_node_count += 1
            if get_node_count == 1:
                provision_state = "manageable"
            else:
                provision_state = "available"
            return utils.MockResponse(
                200,
                ironic_expected_node_body(
                    fake_hw,
                    {
                        "provision_state": provision_state,
                        "driver_info": {
                            "deploy_kernel": "REMOVE-deploy_kernel",
                            "deploy_ramdisk": "REPLACE-deploy_ramdisk",
                        },
                    },
                ),
            )
        elif method == "patch" and path == f"/nodes/{TEST_HARDWARE_UUID}":
            # Validate patch for node properties
            assert sorted(json, key=itemgetter("path")) == sorted(
                [
                    {
                        "op": "remove",
                        "path": "/driver_info/deploy_kernel",
                    },
                    {
                        "op": "remove",
                        "path": "/driver_info/deploy_ramdisk",
                    },
                ],
                key=itemgetter("path"),
            )
            return utils.MockResponse(
                200,
                {
                    "uuid": TEST_HARDWARE_UUID,
                    "created_at": "fake-created_at",
                },
            )
        elif (
            method == "put" and path == f"/nodes/{TEST_HARDWARE_UUID}/states/provision"
        ):
            assert json == {"target": "provide"}
            return utils.MockResponse(200)
        elif (
            method == "get" and path == f"/ports?node={TEST_HARDWARE_UUID}&detail=True"
        ):
            return utils.MockResponse(
                200,
                {"ports": [ironic_expected_port_body(fake_hw, iface_idx=0)]},
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    # 'sleep' is used to wait for provision state changes
    mocker.patch("time.sleep")
    fake_ironic = get_fake_ironic(mocker, _fake_ironic_for_update)

    result = ironic_worker.process(admin_context, fake_hw)

    assert isinstance(result, WorkerResult.Success)
    # call 1 = get the node
    # call 2 = patch the node's properties
    # call 3 = patch the node back to 'available' state
    # call 4 = get the node to see if state changed
    # call 5 = list ports for update
    assert fake_ironic.call_count == 5
