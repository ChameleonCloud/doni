from typing import TYPE_CHECKING
from unittest import mock

import pytest
from doni.driver.worker.blazar import BlazarPhysicalHostWorker
from doni.objects.hardware import Hardware
from doni.tests.unit import utils
from doni.worker import WorkerResult
from keystoneauth1 import loading as ks_loading
from oslo_utils import uuidutils

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


def get_fake_blazar(mocker, request_fn):
    mock_adapter = mock.MagicMock()
    mock_request = mock_adapter.request
    mock_request.side_effect = request_fn
    mocker.patch(
        "doni.driver.worker.blazar._get_blazar_adapter"
    ).return_value = mock_adapter
    return mock_request


def test_create_new_physical_host(
    mocker,
    admin_context: "RequestContext",
    blazar_worker: "BlazarWorker",
    database: "utils.DBFixtures",
):
    def _fake_blazar_for_create(path, method=None, json=None, **kwargs):
        if method == "get" and path == f"v1/os-hosts/{TEST_BLAZAR_HOST_ID}":
            return utils.MockResponse(404)
        elif method == "post" and path == f"/os-hosts":
            assert json["name"] == "compute-1"
            return utils.MockResponse(201, {"created_at": "fake-created_at"})
        raise NotImplementedError("Unexpected request signature")

    fake_blazar = get_fake_blazar(mocker, _fake_blazar_for_create)
    result = blazar_worker.process(admin_context, get_fake_hardware(database), {})

    assert isinstance(result, WorkerResult.Success)
