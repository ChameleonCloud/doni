"""Unit tests for balena sync worker."""
from unittest import mock
import pytest
from oslo_utils import uuidutils
from balena import Balena
from doni.driver.worker.balena import BalenaWorker
from doni.common.context import RequestContext
from doni.tests.unit import utils
from doni.objects.hardware import Hardware

TEST_BALENA_DEVICE_ID = uuidutils.generate_uuid()
TEST_BALENA_DEVICE_TYPE = "raspberrypi4-64"
TEST_BALENA_DEVICE_TYPE_ID = "foobarbaz"

TEST_BALENA_SUPPORTED_DEVICES=[
    {"slug":TEST_BALENA_DEVICE_TYPE, "id":TEST_BALENA_DEVICE_TYPE_ID},
]

def get_fake_hardware(database: "utils.DBFixtures"):
    """Add a dummy hw device to the DB for testing."""
    db_hw = database.add_hardware(
        uuid=TEST_BALENA_DEVICE_ID,
        hardware_type="device.balena",
        properties={
            "machine_name": TEST_BALENA_DEVICE_TYPE,
            "contact_email": "fake-contact_email",
            "device_profiles": ["fake-device_profile"],
        },
    )
    return Hardware(**db_hw)


@pytest.fixture()
def balena_worker(test_config):
    test_config.config(
        enabled_hardware_types=["device.balena"],
        enabled_worker_types=["balena"],
    )
    worker = BalenaWorker()
    worker.register_opts(test_config)
    return worker


def mock_balena(mocker, request_fn):
    mock_adapter = mock.MagicMock()
    mock_request = mock_adapter.request
    mock_request.side_effect = request_fn
    mocker.patch(
        "doni.driver.worker.balena._get_balena_sdk"
    ).return_value = mock_adapter

    return mock_request


def test_set_device_type(
    mocker,
    admin_context: "RequestContext",
    balena_worker: "BalenaWorker",
    database: "utils.DBFixtures",
):
    """Test the method for setting device type."""

    def _fake_balena_for_set_device():
        pass

    fake_balena = mock_balena(mocker, _fake_balena_for_set_device)
    result = balena_worker._set_device_type(
        fake_balena,TEST_BALENA_DEVICE_ID, TEST_BALENA_DEVICE_TYPE
    )

def test_get_all_device_types(
    mocker,
    admin_context: "RequestContext",
    balena_worker: "BalenaWorker",
    database: "utils.DBFixtures",
):

    fake_balena = mock.MagicMock(Balena())
    fake_balena.models.device_type.get_all.return_value=TEST_BALENA_SUPPORTED_DEVICES
    result = balena_worker._get_all_device_types(fake_balena)

    assert result == {TEST_BALENA_DEVICE_TYPE: TEST_BALENA_DEVICE_TYPE_ID}


def test_register_device(
    mocker,
    admin_context: "RequestContext",
    balena_worker: "BalenaWorker",
    database: "utils.DBFixtures",
):
    """Test the method for setting device type."""

    fake_hardware = get_fake_hardware(database)
    fake_balena = mock.MagicMock(Balena())

    # override supported devices
    fake_balena.models.device_type.get_all.return_value=TEST_BALENA_SUPPORTED_DEVICES

    result = balena_worker._register_device(fake_balena, fake_hardware)

