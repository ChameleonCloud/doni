"""Unit tests for tunelo sync worker."""
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from keystoneauth1 import loading as ks_loading
from oslo_utils import uuidutils

from doni.driver.worker.tunelo import TuneloWorker
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.tests.unit import utils
from doni.worker import WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext

TEST_HARDWARE_UUID = uuidutils.generate_uuid()


@pytest.fixture
def tunelo_worker(test_config):
    """Generate a test TuneloWorker and ensure the environment is configured for it.

    Much of this is black magic to appease the gods of oslo_config.
    """
    # Configure the app to use a hardware type valid for this worker.
    test_config.config(
        enabled_hardware_types=["device.balena"],
        enabled_worker_types=["tunelo"],
    )

    worker = TuneloWorker()
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
        group="tunelo",
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
        hardware_type="device.balena",
        properties={
            "channels": {
                "user": {"channel_type": "wireguard", "public_key": "fake-public_key"}
            }
        },
    )
    return Hardware(**db_hw)


def mock_tunelo(mocker, request_fn):
    """Patch method to mock Tunelo client."""
    mock_adapter = mock.MagicMock()
    mock_request = mock_adapter.request
    mock_request.side_effect = request_fn
    mocker.patch(
        "doni.driver.worker.tunelo._get_tunelo_adapter"
    ).return_value = mock_adapter
    return mock_request


def test_new_channel(
    mocker,
    admin_context: "RequestContext",
    tunelo_worker: "TuneloWorker",
    database: "utils.DBFixtures",
):
    fake_channel_uuid = "fake-uuid"

    def _stub_tunelo_request(path, method=None, json=None, **kwargs):
        if method == "get" and path == "/channels":
            return utils.MockResponse(200, {"channels": []})
        if method == "post" and path == "/channels":
            return utils.MockResponse(
                201, {"uuid": fake_channel_uuid, "channel_type": "wireguard"}
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    mock_tunelo(mocker, _stub_tunelo_request)

    result = tunelo_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={},
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload["channels"]["user"] == fake_channel_uuid


def test_update_channel_no_diff(
    mocker,
    admin_context: "RequestContext",
    tunelo_worker: "TuneloWorker",
    database: "utils.DBFixtures",
):
    fake_channel_uuid = "fake-uuid"

    def _stub_tunelo_request(path, method=None, json=None, **kwargs):
        if method == "get" and path == "/channels":
            return utils.MockResponse(
                200,
                {
                    "channels": [
                        {
                            "uuid": fake_channel_uuid,
                            "channel_type": "wireguard",
                            "properties": {"public_key": "fake-public_key"},
                        }
                    ]
                },
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    mock_tunelo(mocker, _stub_tunelo_request)

    result = tunelo_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={"channels": {"user": fake_channel_uuid}},
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload["channels"]["user"] == fake_channel_uuid


def test_update_channel_diff(
    mocker,
    admin_context: "RequestContext",
    tunelo_worker: "TuneloWorker",
    database: "utils.DBFixtures",
):
    fake_channel_uuid = "fake-uuid"
    fake_new_channel_uuid = "fake-new-uuid"

    def _stub_tunelo_request(path, method=None, json=None, **kwargs):
        if method == "get" and path == "/channels":
            return utils.MockResponse(
                200,
                {
                    "channels": [
                        {
                            "uuid": fake_channel_uuid,
                            "channel_type": "wireguard",
                            "properties": {"public_key": "DIFFERENT-public_key"},
                        }
                    ]
                },
            )
        elif method == "delete" and path == "/channels/fake-uuid":
            return utils.MockResponse(204)
        elif method == "post" and path == "/channels":
            return utils.MockResponse(
                200,
                {
                    "uuid": fake_new_channel_uuid,
                    "channel_type": "wireguard",
                    "properties": {"public_key": "fake-public_key"},
                },
            )
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    mock_tunelo(mocker, _stub_tunelo_request)

    result = tunelo_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={"channels": {"user": fake_channel_uuid}},
    )

    assert isinstance(result, WorkerResult.Success)
    assert result.payload["channels"]["user"] == fake_new_channel_uuid


def test_delete_dangling_channels(
    mocker,
    admin_context: "RequestContext",
    tunelo_worker: "TuneloWorker",
    database: "utils.DBFixtures",
):
    fake_channel_uuid = "fake-uuid"

    def _stub_tunelo_request(path, method=None, json=None, **kwargs):
        if method == "get" and path == "/channels":
            return utils.MockResponse(
                200,
                {
                    "channels": [
                        {
                            "uuid": fake_channel_uuid,
                            "channel_type": "wireguard",
                            "properties": {"public_key": "fake-public_key"},
                        },
                        # This one should get deleted
                        {
                            "uuid": "dangling-fake-uuid",
                            "channel_type": "wireguard",
                            "properties": {"public_key": "dangling-public_key"},
                        },
                    ]
                },
            )
        elif method == "delete" and path == "/channels/dangling-fake-uuid":
            return utils.MockResponse(204)
        raise NotImplementedError(f"Unexpected request signature: {method} {path}")

    mock_tunelo(mocker, _stub_tunelo_request)

    result = tunelo_worker.process(
        context=admin_context,
        hardware=get_fake_hardware(database),
        state_details={"channels": {"user": fake_channel_uuid}},
    )

    assert isinstance(result, WorkerResult.Success)
