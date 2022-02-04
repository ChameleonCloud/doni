"""Unit tests for blazar sync worker."""
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from keystoneauth1 import loading as ks_loading
from oslo_utils import uuidutils

from doni.driver.worker.blazar import AW_LEASE_PREFIX
from doni.driver.worker.blazar.physical_host import BlazarPhysicalHostWorker
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.tests.unit import utils
from doni.worker import WorkerResult

TEST_STATE_DETAILS = {
    "blazar_resource_id": "1",
}
TEST_BLAZAR_RESOURCE_ID = "1"
TEST_HARDWARE_UUID = uuidutils.generate_uuid()

if TYPE_CHECKING:
    from doni.common.context import RequestContext


def _fake_lease(aw_obj):
    lease = BlazarPhysicalHostWorker.to_lease(aw_obj)
    lease["id"] = uuidutils.generate_uuid()
    return lease


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


def _get_hosts_response(hw_list) -> dict:
    response_dict = {"hosts": []}
    for hw in hw_list or []:
        hw_dict = {
            "id": TEST_BLAZAR_RESOURCE_ID,
            "hypervisor_hostname": hw.uuid,
            "uid": hw.uuid,
            "node_name": hw.name,
        }
        response_dict["hosts"].append(hw_dict)
    return response_dict


def _stub_blazar_host_new(path, method, json):
    """Blazar stub for case where host where matching UUID does not exist."""
    if method == "get" and path == f"/os-hosts/{TEST_BLAZAR_RESOURCE_ID}":
        return utils.MockResponse(404)
    elif method == "get" and path == f"/os-hosts":
        return utils.MockResponse(200, {"hosts": []})
    elif method == "put" and path == f"/os-hosts/{TEST_BLAZAR_RESOURCE_ID}":
        return utils.MockResponse(404)
    elif method == "post" and path == f"/os-hosts":
        # assume that creation succeeds, return created time
        assert json["node_name"] == "fake_name_1"
        return utils.MockResponse(201, {"host": {"created_at": "fake-created_at"}})
    elif method == "get" and path == f"/leases":
        return utils.MockResponse(200, {"leases": []})
    else:
        return None


def _stub_blazar_host_exist(path, method, json, hw_list=None, host_details={}):
    """Blazar stub for case where host where matching UUID exists."""
    if method == "get" and path == f"/os-hosts/{TEST_BLAZAR_RESOURCE_ID}":
        return utils.MockResponse(200, {"host": host_details})
    elif method == "get" and path == f"/os-hosts":
        return utils.MockResponse(200, _get_hosts_response(hw_list))
    elif method == "put" and path == f"/os-hosts/{TEST_BLAZAR_RESOURCE_ID}":
        assert json["node_name"] == "fake_name_1"
        return utils.MockResponse(
            200,
            {
                "host": {
                    "updated_at": "fake-updated_at",
                    "id": TEST_BLAZAR_RESOURCE_ID,
                    "hypervisor_hostname": TEST_HARDWARE_UUID,
                },
            },
        )
    elif method == "post" and path == f"/os-hosts":
        return utils.MockResponse(
            409,
            {
                "error_code": 409,
                "error_message": "fake-error_message",
            },
        )
    elif method == "get" and path == f"/leases":
        return utils.MockResponse(200, {"leases": []})
    else:
        return None


@pytest.mark.parametrize(
    "state_details,result_type,resource_created_at",
    [
        ({}, WorkerResult.Success, "fake-created_at"),
        (TEST_STATE_DETAILS, WorkerResult.Defer, None),
    ],
)
def test_new_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
    state_details: "dict",
    result_type: "type",
    resource_created_at: "str",
):
    """Test creation of a new physical host in blazar.

    Case 1: Blazar Host ID is None, Host is being added for the first time (normal create)
    Case 2: Blazar Host ID is present, but doesn't match an existing host. (update and 404)

    This assumes:
    1. The host's hw UUID is unique.
    2. The host has already been added to ironic, and therefore nova
    """

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_new(path, method, json)
        if host_response:
            return host_response
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details=state_details,
    )

    assert isinstance(result, result_type)
    assert result.payload.get("resource_created_at") == resource_created_at


@pytest.mark.parametrize(
    "state_details,result_type,blazar_resource_id,call_count",
    [
        ({}, WorkerResult.Defer, TEST_BLAZAR_RESOURCE_ID, 2),
        (TEST_STATE_DETAILS, WorkerResult.Success, TEST_BLAZAR_RESOURCE_ID, 3),
    ],
)
def test_existing_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
    state_details: "dict",
    result_type: "type",
    blazar_resource_id: "str",
    call_count: "int",
):
    """Test creation of a duplicate physical host in blazar.

    Cases:
    1: cached Blazer Host ID is None, but host exists in blazar (create + cache miss)
    2: cached Blazer Host ID is Present, and host exists in blazar (normal update case)

    This case assumes:
    1. The host's hw UUID is already in blazar.
    2. The host has already been added to ironic, and therefore nova
    3. The task's state_details has no cached blazar host ID

    We therefore assume that blazar will detect the duplicate.
    """
    hw_to_add = get_fake_hardware(database)
    hw_list = [hw_to_add]

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_exist(path, method, json, hw_list)
        if host_response:
            return host_response
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_to_add,
        state_details=state_details,
    )

    assert isinstance(result, result_type)
    assert result.payload.get("blazar_resource_id") == blazar_resource_id
    assert blazar_request.call_count == call_count


def test_no_updates_to_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    hw_to_add = get_fake_hardware(database)
    hw_list = [hw_to_add]

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        host_response = _stub_blazar_host_exist(
            path,
            method,
            json,
            hw_list,
            host_details={
                "uid": TEST_HARDWARE_UUID,
                "node_name": "fake_name_1",
                "su_factor": 1.0,
            },
        )
        if host_response:
            return host_response
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_to_add,
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)
    # 1 call to check host, 1 call to check leases
    assert blazar_request.call_count == 2


def _stub_blazar_lease_new(path, method, json, lease_dict):
    """Stub for blazar when when no leases exist."""
    lease_id = lease_dict["id"]
    if method == "get":
        if path == "/leases":
            return utils.MockResponse(200, {"leases": []})
        elif path == f"/leases/{lease_id}":
            return utils.MockResponse(404)
    elif method == "post" and path == "/leases":
        return utils.MockResponse(201, {"lease": {"created_at": "fake-created_at"}})
    elif method == "delete" and path == f"/leases/{lease_id}":
        return utils.MockResponse(404)


def _stub_blazar_lease_existing(path, method, json, lease_dict):
    """Stub for blazar when when a lease with matching UUID exists."""
    lease_body = {"leases": [lease_dict]}
    lease_id = lease_dict["id"]
    if method == "get":
        if path == "/leases":
            return utils.MockResponse(200, lease_body)
        elif path == f"/leases/{lease_id}":
            return utils.MockResponse(200, {"lease": {"created_at": "fake-created_at"}})
    elif method == "put" and path == f"/leases/{lease_id}":
        return utils.MockResponse(200, {"lease": {"updated_at": "fake-updated_at"}})
    elif method == "post" and path == "/leases":
        return utils.MockResponse(409)
    elif method == "delete" and path == f"/leases/{lease_id}":
        return utils.MockResponse(204)


def test_create_new_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
):
    """Test creation of new availability window lease.

    This case assumes:
    Blazar Host API:
        1. The host's hw UUID is already in blazar.
        2. The host has already been added to ironic, and therefore nova
        3. The task's state_details has cached the blazar host ID
    Blazar Lease API:
        1. The availability window list has one item for the current hw item
        2. blazar has no leases stored
    """
    hw_obj = get_fake_hardware(database)
    fake_window = database.add_availability_window(hardware_uuid=hw_obj.uuid)
    aw_obj = AvailabilityWindow(**fake_window)

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")

        lease_response = _stub_blazar_lease_new(path, method, json, _fake_lease(aw_obj))
        if lease_response:
            return lease_response

        # If matched in leases, this will not execute
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response

        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=[aw_obj],
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, WorkerResult.Success)

    # 2 call to hosts path, 2 calls to leases
    assert blazar_request.call_count == 4


@pytest.mark.parametrize(
    "lease_changed,result_type,call_count",
    [
        (False, WorkerResult.Success, 3),
        (True, WorkerResult.Success, 4),
    ],
)
def test_update_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
    lease_changed: "bool",
    result_type: "type",
    call_count: "int",
):
    """Test update of an existing lease for an availability.

    This case assumes:
    Blazar Host API:
        1. The host's hw UUID is already in blazar.
        2. The host has already been added to ironic, and therefore nova
        3. The task's state_details has cached the blazar host ID
    Blazar Lease API:
        1. The availability window list has one item for the current hw item
        2. Blazar has 1 lease stored, that has a name matching the window uuid

    Cases:
        1. Lease exists, and all info matches
        2. Lease exists, and some info does not match
        3. Lease does not exist
    """
    hw_obj = get_fake_hardware(database)
    fake_window = database.add_availability_window(hardware_uuid=hw_obj.uuid)
    aw_obj = AvailabilityWindow(**fake_window)
    fake_lease = _fake_lease(aw_obj)

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")

        if lease_changed:
            # Change end time to force lease update
            timechange = timedelta(days=1)
            fake_lease["end_date"] = (aw_obj.end + timechange).isoformat()

        lease_response = _stub_blazar_lease_existing(path, method, json, fake_lease)
        if lease_response:
            return lease_response

        # If matched in leases, this will not execute
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response

        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=[aw_obj],
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, result_type)

    # 1 call to hosts path, 2 calls to leases
    assert blazar_request.call_count == call_count


@pytest.mark.parametrize(
    "lease_prefix,result_type,call_count",
    [
        (AW_LEASE_PREFIX, WorkerResult.Success, 3),
        ("foo_bar", WorkerResult.Success, 3),
        (None, WorkerResult.Success, 4),
    ],
)
def test_delete_lease(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarPhysicalHostWorker",
    database: "utils.DBFixtures",
    lease_prefix,
    result_type,
    call_count,
):
    """Test delete of a lease that has been removed from the availability window list.

    This case assumes:
    Blazar Host API:
        1. The host's hw UUID is already in blazar.
        2. The host has already been added to ironic, and therefore nova
        3. The task's state_details has cached the blazar host ID
    Blazar Lease API:
        1. The availability window list is empty
        2. Blazar has 1 lease stored
        3. Any lease with a name not matching an availability window's uuid should be removed.
    Cases:
        1. lease in blazar and in AW list and prefix matches, shouldn't remove
        2. lease in blazar, prefix doesn't match, shoudn't remove
        3. Lease in blazar, but not in AW list, should remove
    """
    hw_obj = get_fake_hardware(database)
    fake_window = database.add_availability_window(hardware_uuid=hw_obj.uuid)

    aw_obj = AvailabilityWindow(**fake_window)
    response_body = _fake_lease(aw_obj)

    window_list = []
    if lease_prefix:
        response_body["name"] = f"{lease_prefix}{aw_obj.uuid}"
        if lease_prefix == AW_LEASE_PREFIX:
            # pass matching window list to worker
            window_list = [aw_obj]

    def _stub_blazar_request(path, method=None, json=None, **kwargs):
        print(f"path: {path}; method: {method}")

        lease_response = _stub_blazar_lease_existing(path, method, json, response_body)
        if lease_response:
            return lease_response

        # If matched in leases, this will not execute
        host_response = _stub_blazar_host_exist(path, method, json)
        if host_response:
            return host_response

        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    blazar_request = get_mocked_blazar(mocker, _stub_blazar_request)
    result = blazar_worker.process(
        context=admin_context,
        hardware=hw_obj,
        availability_windows=window_list,
        state_details=TEST_STATE_DETAILS,
    )

    assert isinstance(result, result_type)
    assert blazar_request.call_count == call_count
