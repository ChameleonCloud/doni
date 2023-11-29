"""Unit tests for balena sync worker."""
from unittest import mock
import pytest
from oslo_utils import uuidutils
from balena import Balena
from doni.driver.worker.balena import BalenaWorker
from doni.common.context import RequestContext
from doni.tests.unit import utils
from doni.objects.hardware import Hardware
import dataclasses


TEST_BALENA_DEVICE_ID = uuidutils.generate_uuid()
TEST_BALENA_DEVICE_TYPE = "raspberrypi4-64"
TEST_BALENA_DEVICE_TYPE_ID = "foobarbaz"

TEST_BALENA_SUPPORTED_DEVICES = [
    {"slug": TEST_BALENA_DEVICE_TYPE, "id": TEST_BALENA_DEVICE_TYPE_ID},
]

TEST_ENV_VAR_KEY = "foo"
TEST_ENV_VAR_VALUE = "bar"


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
        fake_balena, TEST_BALENA_DEVICE_ID, TEST_BALENA_DEVICE_TYPE
    )


def test_get_all_device_types(
    mocker,
    admin_context: "RequestContext",
    balena_worker: "BalenaWorker",
    database: "utils.DBFixtures",
):
    fake_balena = mock.MagicMock(Balena())
    fake_balena.models.device_type.get_all.return_value = TEST_BALENA_SUPPORTED_DEVICES
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
    fake_balena.models.device_type.get_all.return_value = TEST_BALENA_SUPPORTED_DEVICES

    result = balena_worker._register_device(fake_balena, fake_hardware)


@dataclasses.dataclass
class DeviceVarTestItem:
    """Class for arguments to device var tests."""
    service_name: "str|None"
    get_all_input: "list[dict]"
    service_get_count: int
    service_create_count: int
    service_update_count: int
    device_get_count: int
    device_create_count: int
    device_update_count: int

env_sync_test_data = [
    DeviceVarTestItem(None, [{"name": TEST_ENV_VAR_KEY, "value": TEST_ENV_VAR_VALUE}], 0,0,0,1,0,0), # device env alreday set
    DeviceVarTestItem(None, [{"name": TEST_ENV_VAR_KEY+"a", "value": TEST_ENV_VAR_VALUE}], 0,0,0,1,1,0), # device, another env has target value
    DeviceVarTestItem(None, [{"name": TEST_ENV_VAR_KEY, "value": TEST_ENV_VAR_VALUE+"a", "id": 5}], 0,0,0,1,0,1), # device, env exists but different val
    DeviceVarTestItem(None, [{"name": TEST_ENV_VAR_KEY+"a", "value": TEST_ENV_VAR_VALUE+"a"}], 0,0,0,1,1,0), # device, another env has non-target value
    DeviceVarTestItem("Foo", [{"name": TEST_ENV_VAR_KEY, "value": TEST_ENV_VAR_VALUE}], 1,0,0,0,0,0), # service var, already set
    DeviceVarTestItem("Foo", [{"name": TEST_ENV_VAR_KEY+"a", "value": TEST_ENV_VAR_VALUE}], 1,1,0,0,0,0), # service var, another env with target value
    DeviceVarTestItem("Foo", [{"name": TEST_ENV_VAR_KEY, "value": TEST_ENV_VAR_VALUE+"a", "id": 5}], 1,0,1,0,0,0), # service var, env exists but different val
    DeviceVarTestItem("Foo", [{"name": TEST_ENV_VAR_KEY+"a", "value": TEST_ENV_VAR_VALUE+"a"}], 1,1,0,0,0,0), # service var, another env with non-target value
]
parametrize_keys = [n.name for n in dataclasses.fields(DeviceVarTestItem)]
parametrize_vals = [dataclasses.astuple(i) for i in env_sync_test_data]

@pytest.mark.parametrize(parametrize_keys,parametrize_vals)
def test_sync_device_var(
    get_all_input,
    service_name,
    service_get_count,
    service_create_count,
    service_update_count,
    device_get_count,
    device_create_count,
    device_update_count,
    balena_worker: "BalenaWorker",
    database: "utils.DBFixtures",
):
    """Test method to sync env variables from doni to balena "device variables" """

    fake_hardware = get_fake_hardware(database)
    fake_balena = mock.MagicMock(Balena())

    # override returned device vars
    fake_balena.models.environment_variables.device_service_environment_variable.get_all.return_value = (
        get_all_input
    )
    fake_balena.models.environment_variables.device.get_all.return_value = get_all_input
    balena_worker._sync_device_var(
        balena=fake_balena,
        service_name=service_name,
        hardware_uuid=fake_hardware.uuid,
        key=TEST_ENV_VAR_KEY,
        value=TEST_ENV_VAR_VALUE,
    )


    assert fake_balena.models.environment_variables.device_service_environment_variable.get_all.call_count==service_get_count
    assert fake_balena.models.environment_variables.device_service_environment_variable.create.call_count==service_create_count
    assert fake_balena.models.environment_variables.device_service_environment_variable.update.call_count==service_update_count
    assert fake_balena.models.environment_variables.device.get_all.call_count==device_get_count
    assert fake_balena.models.environment_variables.device.create.call_count==device_create_count
    assert fake_balena.models.environment_variables.device.update.call_count==device_update_count
