"""Doni test utilities."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from oslo_utils import uuidutils
from stevedore import extension, named

from doni.common import driver_factory, exception
from doni.db import api as db_api

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


FAKE_HARDWARE_TYPE = "fake-hardware"
FAKE_WORKER_TYPE = "fake-worker"


def get_test_hardware(**kw):
    default_uuid = uuidutils.generate_uuid()
    return {
        "created_at": kw.get("created_at"),
        "updated_at": kw.get("updated_at"),
        "deleted": kw.get("deleted"),
        "deleted_at": kw.get("deleted_at"),
        "id": kw.get("id", 234),
        "name": kw.get("name", "fake_name"),
        "uuid": kw.get("uuid", default_uuid),
        "hardware_type": kw.get("hardware_type", FAKE_HARDWARE_TYPE),
        "project_id": kw.get("project_id", "fake_project_id"),
        "properties": kw.get(
            "properties",
            {
                "private-field": "fake-private-field",
                "private-and-sensitive-field": "fake-private-and-sensitive-field",
                "public-field": "fake-public-field",
                "public-and-sensitive-field": "fake-public-and-sensitive-field",
            },
        ),
    }


def get_test_availability_window(**kw):
    default_uuid = uuidutils.generate_uuid()
    return {
        "created_at": kw.get("created_at"),
        "updated_at": kw.get("updated_at"),
        "id": kw.get("id", 456),
        "uuid": kw.get("uuid", default_uuid),
        "hardware_uuid": kw.get("hardware_uuid"),
        "start": kw.get("start", datetime.now()),
        "end": kw.get("end", datetime.now() + timedelta(days=1)),
    }


def mock_drivers(mocker: "MockerFixture", namespaces: dict = None):
    """Mock out drivers dynamically included via entry_points.

    This can be used to create one-off test drivers for hardware types or
    workers in unit tests.

    This works by mocking ``_create_extension_manager`` under the hood.

    Args:
        namespaces (dict): A mapping of entry_point namespaces to the
            drivers that should be mocked under that entry_point. These drivers
            will replace any drivers already configured. The drivers should
            be a mapping of driver name to the implementation class, e.g.::

                {
                    "my-driver": MyDriver,
                    "my-other-driver": MyOtherDriver,
                }
    """
    orig_create_extension_manager = driver_factory._create_extension_manager

    def _create_extension_manager(_namespace, names, **kwargs):
        if _namespace not in namespaces:
            return orig_create_extension_manager(_namespace, names, **kwargs)

        drivers = namespaces[_namespace]
        extensions = [
            extension.Extension(name, entry_point=None, plugin=driver_class, obj=None)
            for name, driver_class in drivers.items()
            # The caller will specify an allowlist of names to add to the
            # extension manager; ensure we respect this.
            if name in names
        ]
        on_load_failure_callback = kwargs["on_load_failure_callback"]
        em = named.NamedExtensionManager.make_test_instance(
            extensions, _namespace, on_load_failure_callback=on_load_failure_callback
        )
        # NOTE(jason): ``make_test_instance`` doesn't actually do anything with
        # the on_load_failure_callback, nor does it attempt to invoke the
        # entrypoints. So, we do a kludgy mimicry of this here.
        for ext in extensions:
            try:
                ext.obj = ext.plugin()
            except Exception as exc:
                on_load_failure_callback(em, ext, exc)
        return em

    (
        mocker.patch("doni.common.driver_factory._create_extension_manager").side_effect
    ) = _create_extension_manager


class DBFixtures(object):
    """A helper utility for setting up test data in the test DB."""

    def __init__(self):
        self.db = db_api.get_instance()
        self._hardwares = []
        self._availability_windows = []
        self._counter = 0

    def add_hardware(self, initial_worker_state=None, **hardware_kwargs) -> dict:
        """Add a hardware item to the test database.

        Args:
            **hardware_kwargs: attributes to set on the Hardware object.

        Returns:
            A dict representing the added hardware, with all fields set.
        """
        self._counter += 1
        # Avoid name collisions
        hardware_kwargs.setdefault("name", f"fake_name_{self._counter}")
        fake_hw = get_test_hardware(**hardware_kwargs)
        # ID will be auto-assigned by DB
        fake_hw.pop("id")
        db_hw = self.db.create_hardware(
            fake_hw, initial_worker_state=initial_worker_state
        )
        self._hardwares.append(db_hw)
        # Copy auto-generated fields
        fake_hw["created_at"] = db_hw.created_at
        return fake_hw

    def add_availability_window(self, **window_kwargs) -> dict:
        fake_window = get_test_availability_window(**window_kwargs)
        # ID will be auto-assigned by DB
        fake_window.pop("id")
        self._availability_windows.append(
            self.db.create_availability_window(fake_window)
        )
        return fake_window

    def remove_hardware(self, hardware_uuid):
        """Remove a hardware item from the test database.

        Args:
            hardware_uuid (str): The UUID of the hardware item to remove.

        Raises:
            ValueError: if a Hardware could not be found for the UUID.
        """
        hw = next((hw for hw in self._hardwares if hw.uuid == hardware_uuid), None)
        if not hw:
            raise ValueError(f"Could not find Hardware {hardware_uuid} to delete")
        self.db.destroy_hardware(hw.uuid)
        self._hardwares.remove(hw)

    @property
    def hardwares(self):
        return [hw.as_dict() for hw in self._hardwares]

    def cleanup(self):
        for hw in self._hardwares:
            try:
                self.db.destroy_hardware(hw.uuid)
            except exception.HardwareNotFound:
                # Allow tests to destroy hardware
                pass
        self._hardwares = []

        for aw in self._availability_windows:
            try:
                self.db.destroy_availability_window(aw.uuid)
            except exception.AvailabilityWindowNotFound:
                pass
        self._availability_windows = []


class MockResponse:
    """A simple class for mocking HTTP responses."""

    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.body = body

    @property
    def text(self):
        return str(self.body) if self.body is not None else ""

    def json(self):
        return self.body
