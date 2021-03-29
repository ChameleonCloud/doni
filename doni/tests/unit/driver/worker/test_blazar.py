"""Unit tests for blazar sync worker."""
import json
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from keystoneauth1 import loading as ks_loading
from oslo_utils import uuidutils

from doni.driver.worker.blazar import BlazarPhysicalHostWorker
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.tests.unit import utils
from doni.worker import WorkerResult

TEST_STATE_DETAILS = {
    "blazar_host_id": "1",
}
TEST_BLAZAR_HOST_ID = "1"
TEST_HARDWARE_UUID = uuidutils.generate_uuid()

if TYPE_CHECKING:
    from doni.common.context import RequestContext


@pytest.fixture
def blazar_worker(test_config):
    """Generate a test blazarWorker and ensure the environment is configured for it.

    Much of this is black magic to appease the gods of oslo_config.
    """
    # Configure the app to use a hardware type valid for this worker.
    test_config.config(
        enabled_hardware_types=["baremetal"],
        enabled_worker_types=["blazar.physical_host"],
    )

    worker = BlazarPhysicalHostWorker()
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
        group="blazar",
        auth_type="v3password",
        auth_url="http://localhost:5000",
        username="fake-username",
        user_domain_name="fake-user-domain-name",
        password="fake-password",
        project_name="fake-project-name",
        project_domain_name="fake-project-domain-name",
    )
    return worker


def get_fake_hardware(database: "utils.DBFixtures"):
    """Add a dummy hw device to the DB for testing."""
    db_hw = database.add_hardware(
        uuid=TEST_HARDWARE_UUID,
        hardware_type="baremetal",
        properties={
            "baremetal_driver": "fake-driver",
            "management_address": "fake-management_address",
            "ipmi_username": "fake-ipmi_username",
            "ipmi_password": "fake-ipmi_password",
        },
    )
    return Hardware(**db_hw)


def get_mocked_blazar(mocker, request_fn):
    """Patch method to mock blazar client."""
    mock_adapter = mock.MagicMock()
    mock_request = mock_adapter.request
    mock_request.side_effect = request_fn
    mocker.patch(
        "doni.driver.worker.blazar._get_blazar_adapter"
    ).return_value = mock_adapter
    return mock_request


def _stub_blazar_host_new(path, method, json):
    """Blazar stub for case where host where matching UUID does not exist."""
    if method == "get" and path == f"/os-hosts/{TEST_BLAZAR_HOST_ID}":
        return utils.MockResponse(404)
    elif method == "put" and path == f"/os-hosts/{TEST_BLAZAR_HOST_ID}":
        return utils.MockResponse(404)
    elif method == "post" and path == f"/os-hosts":
        # assume that creation succeeds, return created time
        assert json["node_name"] == "fake_name_1"
        return utils.MockResponse(201, {"created_at": "fake-created_at"})
    else:
        return None


def _stub_blazar_host_exist(path, method, json):
    """Blazar stub for case where host where matching UUID exists."""
    if method == "get" and path == f"/os-hosts/{TEST_BLAZAR_HOST_ID}":
        return utils.MockResponse(200)
    elif method == "put" and path == f"/os-hosts/{TEST_BLAZAR_HOST_ID}":
        assert json["node_name"] == "fake_name_1"
        return utils.MockResponse(201, {"updated_at": "fake-updated_at"})
    elif method == "post" and path == f"/os-hosts":
        # TODO Is this true?
        return utils.MockResponse(
            409, {"created_at": "fake-created_at", "name": "{TEST_BLAZAR_HOST_ID}"}
        )
    else:
        return None


def test_create_new_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test creation of a new physical host in blazar.

    This case assumes:
    1. The host's hw UUID is unique.
    2. The host has already been added to ironic, and therefore nova
    3. The task's state_details has no cached blazar host ID

    We therefore assume that blazar will accept the new host.
    """

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_new(path, method, json)
        if host_response:
            return host_response
        raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={},
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload.get("host_created_at") == "fake-created_at"
    assert blazar_request.call_count == 1


def test_create_duplicate_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test creation of a duplicate physical host in blazar.

    This case assumes:
    1. The host's hw UUID is already in blazar.
    2. The host has already been added to ironic, and therefore nova
    3. The task's state_details has no cached blazar host ID

    We therefore assume that blazar will detect the duplicate.
    """

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response
        raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={},
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload.get("host_created_at") == "fake-created_at"
    assert blazar_request.call_count == 1


def test_update_existing_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test update of an existing physical host in blazar.

    This case assumes:
    1. The host's hw UUID is already in blazar.
    2. The host has already been added to ironic, and therefore nova
    3. The task's state_details has cached the blazar host ID

    We therefore assume that blazar will detect the duplicate.
    """

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response
        raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload.get("host_updated_at") == "fake-updated_at"
    assert blazar_request.call_count == 1


def _aw_lease_dict(aw: AvailabilityWindow) -> dict:
    aw_dict = {
        "name": f"availability_window_{aw.uuid}",
        "start_date": aw.start.isoformat(),
        "end_date": aw.end.isoformat(),
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
    return aw_dict


def _stub_blazar_lease_new(path, method, json, lease_uuid):
    """Stub for making a lease when none exist."""
    if method == "get":
        if path == f"/leases":
            return utils.MockResponse(200, {"leases": []})
        elif path == f"/leases/{lease_uuid}":
            return utils.MockResponse(404)
    elif method == "post" and path == f"/leases":
        return utils.MockResponse(201, {"created_at": "fake-created_at"})


def test_create_new_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test creation of new lease for part-time resources."""
    hw_obj = get_fake_hardware(database)
    fake_window = database.add_availability_window(hardware_uuid=hw_obj.uuid)
    aw_obj = AvailabilityWindow(**fake_window)

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response

        lease_response = _stub_blazar_lease_new(path, method, json, aw_obj.uuid)
        if lease_response:
            return lease_response

        raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=[aw_obj],
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)

    # 1 call to hosts path, 2 calls to leases
    assert blazar_request.call_count == 3


def test_update_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test update of existing lease for part-time resources."""
    hw_obj = get_fake_hardware(database)
    fake_window = database.add_availability_window(
        hardware_uuid=hw_obj.uuid,
    )
    aw_obj = AvailabilityWindow(**fake_window)

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response
        elif method == "get" and path == f"/leases/{aw_obj.uuid}":
            return utils.MockResponse(200, {"created_at": "fake-created_at"})
        elif method == "put" and path == f"/leases/{aw_obj.uuid}":
            return utils.MockResponse(200, {"updated_at": "fake-updated_at"})

        raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=[aw_obj],
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)

    # 1 call to hosts path, 2 calls to leases
    assert blazar_request.call_count == 3


def test_delete_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test deletion of an existing lease.

    This case assumes:
    1. A lease was created, matching the UUID of an availability window.
    2. That availability window was then removed.
    3. Any lease with a name not matching an availability window should be removed.
    4. TODO: how is this filtered so as not to remove all leases!
    """
    hw_obj = get_fake_hardware(database)

    aw_list = []
    aw_dict = {}

    for i in range(0, 2):
        fake_window = database.add_availability_window(hardware_uuid=hw_obj.uuid)
        aw_obj = AvailabilityWindow(**fake_window)
        # Create list to pass to blazar call
        aw_list.append(aw_obj)
        # add to dict, for use in mocked response
        aw_dict[f"/leases/{aw_obj.uuid}"] = _aw_lease_dict(aw_obj)

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response
        elif method == "get":
            if path == "/leases":
                return utils.MockResponse(200, list(aw_dict.values()))
            elif path in aw_dict.keys():
                return utils.MockResponse(200, aw_dict.get(path))
            else:
                return utils.MockResponse(404)
        elif method == "put" and path in aw_dict.keys():
            print(aw_dict.get(path))
            return utils.MockResponse(200, aw_dict.get(path))
        elif method == "delete":
            if path in aw_dict.keys():
                return utils.MockResponse(204)
            else:
                return utils.MockResponse(404)
        else:
            raise NotImplementedError("Unexpected request signature")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=aw_list,
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)

    assert blazar_request.call_count == 3
