import json
import re

import pytest
from flask.testing import FlaskClient
from oslo_policy.policy import PolicyNotAuthorized
from oslo_utils import uuidutils

from doni.common.context import RequestContext
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.objects.worker_task import WorkerTask
from doni.tests.unit import utils
from doni.worker import WorkerState


class AnyContext(RequestContext):
    """Check that an argument was a request context."""

    def __eq__(self, other):
        return True


class HardwareMatching(Hardware):
    """Check that an argument is a Hardware object w/ some exact field values."""

    def __init__(self, **kwargs):
        self.field_requirements = kwargs.copy()
        super().__init__(**kwargs)

    def __eq__(self, other):
        return all(
            getattr(self, k, None) == getattr(other, k, None)
            for k in self.field_requirements.keys()
        )


def _assert_hardware_json_ok(hw_json, expected):
    # Don't return internal IDs
    assert "id" not in hw_json
    # But we should get UUIDs
    assert "uuid" in hw_json
    # Don't strictly validate dates, but check they're present
    assert hw_json["created_at"] is not None

    for ignored_key in ["created_at", "updated_at", "deleted", "deleted_at"]:
        expected.pop(ignored_key, None)

    # Check that keys validate against expected values
    for key in expected.keys():
        assert hw_json[key] == expected[key]


def _with_masked_sensitive_fields(expected):
    expected_with_masked = expected.copy()
    for field in ["private-and-sensitive-field", "public-and-sensitive-field"]:
        if field in expected_with_masked["properties"]:
            expected_with_masked["properties"][field] = "************"
    return expected_with_masked


def _assert_hardware_has_workers(hw_json):
    workers = hw_json["workers"]
    assert len(workers) == 1
    assert workers[0]["worker_type"] == utils.FAKE_WORKER_TYPE
    assert isinstance(workers[0]["state_details"], dict)


def test_get_all_hardware(
    mocker,
    user_auth_headers,
    user_project_id,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    hw = database.add_hardware(project_id=user_project_id)
    # Also add a hardware NOT owned by this user/project.
    database.add_hardware()
    res = client.get("/v1/hardware", headers=user_auth_headers)
    assert res.status_code == 200
    assert len(res.json["hardware"]) == 1
    _assert_hardware_json_ok(res.json["hardware"][0], _with_masked_sensitive_fields(hw))
    mock_authorize.assert_called_once_with(
        "hardware:get", AnyContext(), {"project_id": user_project_id}
    )


def test_get_all_hardware_empty(
    mocker, user_auth_headers, user_project_id, client: "FlaskClient"
):
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    res = client.get("/v1/hardware", headers=user_auth_headers)
    assert res.status_code == 200
    assert res.json == {
        "hardware": [],
        "links": [],
    }
    mock_authorize.assert_called_once_with(
        "hardware:get", AnyContext(), {"project_id": user_project_id}
    )


def test_get_all_hardware_all_projects_not_admin(
    user_auth_headers,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware()
    res = client.get("/v1/hardware?all_projects=True", headers=user_auth_headers)
    assert res.status_code == 403
    assert "hardware" not in res.json


def test_get_all_hardware_all_projects_admin(
    admin_auth_headers,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    hw = database.add_hardware()
    res = client.get("/v1/hardware?all_projects=True", headers=admin_auth_headers)
    assert res.status_code == 200
    assert len(res.json["hardware"]) == 1
    _assert_hardware_json_ok(res.json["hardware"][0], _with_masked_sensitive_fields(hw))


def test_get_one_hardware(
    mocker, user_auth_headers, client: "FlaskClient", database: "utils.DBFixtures"
):
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    hw = database.add_hardware()
    res = client.get(f"/v1/hardware/{hw['uuid']}", headers=user_auth_headers)
    assert res.status_code == 200
    _assert_hardware_json_ok(res.json, _with_masked_sensitive_fields(hw))
    _assert_hardware_has_workers(res.json)
    mock_authorize.assert_called_once_with(
        "hardware:get", AnyContext(), HardwareMatching(uuid=hw["uuid"])
    )


def test_missing_hardware(mocker, user_auth_headers, client: "FlaskClient"):
    mocker.patch("doni.api.hardware.authorize")
    res = client.get(
        f"/v1/hardware/{uuidutils.generate_uuid()}", headers=user_auth_headers
    )
    assert res.status_code == 404
    assert re.match("Hardware .* could not be found", res.json["error"]) is not None


def test_enroll_hardware(mocker, user_auth_headers, client: "FlaskClient"):
    """Tests that enroll succeeds for valid payload."""
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    enroll_payload = {
        "name": "fake-name",
        "hardware_type": utils.FAKE_HARDWARE_TYPE,
        "properties": {
            "default_required_field": "fake-default_required_field",
            "private-field": "fake-private_field",
            "public-field": "fake-public_field",
            "private-and-sensitive-field": "fake-private_and_sensitive_field",
            "public-and-sensitive-field": "fake-public_and_sensitive_field",
            "default-field": "fake-default_field",
        },
    }
    res = client.post(
        f"/v1/hardware",
        headers=user_auth_headers,
        content_type="application/json",
        data=json.dumps(enroll_payload),
    )
    assert res.status_code == 201
    _assert_hardware_json_ok(res.json, _with_masked_sensitive_fields(enroll_payload))
    _assert_hardware_has_workers(res.json)
    mock_authorize.assert_called_once_with("hardware:create", AnyContext())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "name": "fake-name",
                "hardware_type": utils.FAKE_HARDWARE_TYPE,
                "properties": {},
            },
            id="invalid_properties",
        ),
        pytest.param(
            {"name": "fake-name", "hardware_type": utils.FAKE_HARDWARE_TYPE},
            id="no_properties",
        ),
        pytest.param(
            {"name": "fake-name"},
            id="no_hardware_type",
        ),
        pytest.param(
            {
                "name": "fake-name",
                "project_id": "fake-project_id",
                "hardware_type": utils.FAKE_HARDWARE_TYPE,
                "properties": {
                    "default_required_field": "fake-default_required_field",
                },
            },
            id="has_project_id",
        ),
    ],
)
def test_enroll_validation(payload, user_auth_headers, client: "FlaskClient"):
    """Tests that validation fails for various cases."""
    res = client.post(
        f"/v1/hardware",
        headers=user_auth_headers,
        content_type="application/json",
        data=json.dumps(payload),
    )
    assert res.status_code == 400


FAKE_UUID = uuidutils.generate_uuid()
FAKE_WINDOW_UUID = uuidutils.generate_uuid()


@pytest.mark.parametrize(
    "path,req_kwargs",
    [
        pytest.param("/v1/hardware/", {"method": "GET"}, id="get_all"),
        pytest.param(f"/v1/hardware/{FAKE_UUID}/", {"method": "GET"}, id="get_one"),
        pytest.param(
            "/v1/hardware/",
            {
                "method": "POST",
                "content_type": "application/json",
                "data": json.dumps(
                    {
                        "name": "fake-name",
                        "hardware_type": utils.FAKE_HARDWARE_TYPE,
                        "properties": {
                            "default_required_field": "fake-default_required_field"
                        },
                    }
                ),
            },
            id="enroll",
        ),
        pytest.param(
            f"/v1/hardware/{FAKE_UUID}/",
            {
                "method": "PATCH",
                "content_type": "application/json",
                "data": json.dumps([]),
            },
            id="update",
        ),
        pytest.param(f"/v1/hardware/{FAKE_UUID}/", {"method": "DELETE"}, id="destroy"),
        pytest.param(f"/v1/hardware/{FAKE_UUID}/sync/", {"method": "POST"}, id="sync"),
    ],
)
def test_policy_disallow(
    mocker,
    user_auth_headers,
    path,
    req_kwargs,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    """Check that for a given handler, it properly handles authorization errors."""
    database.add_hardware(uuid=FAKE_UUID)
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    mock_authorize.side_effect = PolicyNotAuthorized("fakerule", {}, {})
    res = client.open(path, headers=user_auth_headers, **req_kwargs)
    assert res.status_code == 403


def test_update_hardware(
    mocker,
    admin_context,
    user_auth_headers,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware(uuid=FAKE_UUID, initial_worker_state=WorkerState.STEADY)
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    patch = [{"path": "/name", "op": "replace", "value": "new-fake-name"}]
    res = client.patch(
        f"/v1/hardware/{FAKE_UUID}",
        headers=user_auth_headers,
        content_type="application/json",
        data=json.dumps(patch),
    )
    assert res.status_code == 200
    mock_authorize.assert_called_once_with(
        "hardware:update", AnyContext(), HardwareMatching(uuid=FAKE_UUID)
    )
    _assert_hardware_json_ok(res.json, {"uuid": FAKE_UUID, "name": "new-fake-name"})
    _assert_hardware_has_workers(res.json)
    for task in WorkerTask.list_for_hardware(admin_context, FAKE_UUID):
        assert task.state == WorkerState.PENDING


@pytest.mark.parametrize(
    "patch,expected_status",
    [
        pytest.param(
            [
                {
                    "path": "/availability/-",
                    "op": "add",
                    "value": {"start": "x", "end": "y"},
                }
            ],
            400,
            id="add_invalid",
        ),
        pytest.param(
            [
                {
                    "path": "/availability/-",
                    "op": "add",
                    "value": {
                        "start": "2021-03-03T00:00:00Z",
                        "end": "2021-04-01T00:00:00Z",
                    },
                }
            ],
            200,
            id="add_valid",
        ),
        pytest.param(
            [
                {
                    "path": f"/availability/{FAKE_WINDOW_UUID}/start",
                    "op": "replace",
                    "value": "x",
                }
            ],
            400,
            id="replace_invalid",
        ),
        pytest.param(
            [
                {
                    "path": f"/availability/{FAKE_WINDOW_UUID}/not_allowed_field",
                    "op": "add",
                    "value": "x",
                }
            ],
            400,
            id="add_not_allowed_field",
        ),
        pytest.param(
            [{"path": f"/availability/{FAKE_WINDOW_UUID}", "op": "remove"}],
            200,
            id="remove",
        ),
        pytest.param(
            [{"path": f"/availability/non_existent_uuid", "op": "remove"}],
            400,
            id="remove",
        ),
    ],
)
def test_update_availability(
    mocker,
    user_auth_headers,
    patch,
    expected_status,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware(uuid=FAKE_UUID)
    database.add_availability_window(uuid=FAKE_WINDOW_UUID, hardware_uuid=FAKE_UUID)
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    res = client.patch(
        f"/v1/hardware/{FAKE_UUID}",
        headers=user_auth_headers,
        content_type="application/json",
        data=json.dumps(patch),
    )
    assert res.status_code == expected_status
    mock_authorize.assert_called_once_with(
        "hardware:update", AnyContext(), HardwareMatching(uuid=FAKE_UUID)
    )


def test_update_availability_final_state(
    mocker,
    user_auth_headers,
    admin_context,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware(uuid=FAKE_UUID)
    aw1 = database.add_availability_window(hardware_uuid=FAKE_UUID)
    aw2 = database.add_availability_window(hardware_uuid=FAKE_UUID)
    mocker.patch("doni.api.hardware.authorize")
    patch = [
        # Add new aw3 to end of list
        {
            "path": "/availability/-",
            "op": "add",
            "value": {
                "start": "2021-03-03T00:00:00Z",
                "end": "2021-04-01T00:00:00Z",
            },
        },
        # Update aw2
        {
            "path": f"/availability/{aw2['uuid']}/start",
            "op": "replace",
            "value": "2021-03-04T00:00:00Z",
        },
        # Delete aw1
        {
            "path": f"/availability/{aw1['uuid']}",
            "op": "remove",
        },
    ]
    res = client.patch(
        f"/v1/hardware/{FAKE_UUID}",
        headers=user_auth_headers,
        content_type="application/json",
        data=json.dumps(patch),
    )
    assert res.status_code == 200
    windows = AvailabilityWindow.list_for_hardware(admin_context, FAKE_UUID)
    assert len(windows) == 2
    assert windows[0].uuid == aw2["uuid"]
    assert windows[0].start.isoformat() == "2021-03-04T00:00:00+00:00"
    assert windows[1].start.isoformat() == "2021-03-03T00:00:00+00:00"
    assert windows[1].end.isoformat() == "2021-04-01T00:00:00+00:00"


def test_destroy_hardware(
    mocker, user_auth_headers, client: "FlaskClient", database: "utils.DBFixtures"
):
    database.add_hardware(uuid=FAKE_UUID)
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    res = client.delete(f"/v1/hardware/{FAKE_UUID}/", headers=user_auth_headers)
    assert res.status_code == 200
    mock_authorize.assert_called_once_with(
        "hardware:delete", AnyContext(), HardwareMatching(uuid=FAKE_UUID)
    )


def test_sync(
    mocker,
    admin_context,
    user_auth_headers,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware(uuid=FAKE_UUID)
    task = WorkerTask.list_for_hardware(admin_context, FAKE_UUID)[0]
    # Simulate successful state transition
    task.state = WorkerState.IN_PROGRESS
    task.state = WorkerState.STEADY
    task.save()
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    res = client.post(f"/v1/hardware/{FAKE_UUID}/sync", headers=user_auth_headers)
    assert res.status_code == 200
    mock_authorize.assert_called_once_with(
        "hardware:update", AnyContext(), HardwareMatching(uuid=FAKE_UUID)
    )
    assert (
        WorkerTask.list_for_hardware(admin_context, FAKE_UUID)[0].state
        == WorkerState.PENDING
    )


def test_sync_handles_in_progress(
    mocker,
    admin_context,
    user_auth_headers,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    database.add_hardware(uuid=FAKE_UUID)
    task = WorkerTask.list_for_hardware(admin_context, FAKE_UUID)[0]
    task.state = WorkerState.IN_PROGRESS
    task.save()
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    res = client.post(f"/v1/hardware/{FAKE_UUID}/sync", headers=user_auth_headers)
    assert res.status_code == 200
    mock_authorize.assert_called_once_with(
        "hardware:update", AnyContext(), HardwareMatching(uuid=FAKE_UUID)
    )
