import collections

from oslo_concurrency import lockutils
from oslo_log import log
from stevedore import named

from doni.common import exception
from doni.conf import CONF

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping
    from doni.driver.hardware_type.base import BaseHardwareType
    from doni.driver.worker.base import BaseWorker


LOG = log.getLogger(__name__)

EM_SEMAPHORE = "extension_manager"


def _get_all_drivers(factory):
    """Get all drivers for `factory` as a dict name -> driver object."""
    return {name: factory[name].obj for name in factory.names}


def get_worker_type(worker_type) -> "BaseWorker":
    """Get a worker type instance by name.

    Args:
        worker_type (str): The name of the worker type to find.

    Returns:
        An instance of ~:class:`doni.worker.BaseWorker`

    Raises:
        DriverNotFound: If requested worker type cannot be found.
    """
    try:
        return WorkerTypeFactory().get_driver(worker_type)
    except KeyError:
        raise exception.DriverNotFound(driver_name=worker_type)


def worker_types() -> "Mapping[str,BaseWorker]":
    """Get all worker types.

    Returns:
        Dictionary mapping worker type name to worker type object.
    """
    return _get_all_drivers(WorkerTypeFactory())


def get_hardware_type(hardware_type) -> "BaseHardwareType":
    """Get a hardware type instance by name.

    Args:
        hardware_type (str): The name of the hardware type to find.

    Returns:
        An instance of ~:class:`doni.driver.hardware_type.HardwareType`

    Raises:
        DriverNotFound: If requested hardware type cannot be found.
    """
    try:
        return HardwareTypeFactory().get_driver(hardware_type)
    except KeyError:
        raise exception.DriverNotFound(driver_name=hardware_type)


def hardware_types() -> "Mapping[str,BaseHardwareType]":
    """Get all hardware types.

    Returns:
        Dictionary mapping hardware type name to hardware type object.
    """
    return _get_all_drivers(HardwareTypeFactory())


def _create_extension_manager(
    namespace,
    names,
    on_load_failure_callback=None,
    on_missing_entrypoints_callback=None,
):
    return named.NamedExtensionManager(
        namespace,
        names,
        invoke_on_load=True,
        on_load_failure_callback=on_load_failure_callback,
        propagate_map_exceptions=True,
        on_missing_entrypoints_callback=on_missing_entrypoints_callback,
    )


class BaseDriverFactory(object):
    """Discover, load and manage the drivers available.

    This is subclassed to load both main drivers and extra interfaces.
    """

    _extension_manager = None
    # Entrypoint name containing the list of all available drivers/interfaces
    _entrypoint_name = None
    # Name of the [DEFAULT] section config option containing a list of enabled
    # drivers/interfaces
    _enabled_driver_list_config_option = ""
    # This field will contain the list of the enabled drivers/interfaces names
    # without duplicates
    _enabled_driver_list = None
    # Template for logging loaded drivers
    _logging_template = "Loaded the following drivers: %s"

    def __init__(self):
        if not self.__class__._extension_manager:
            self.__class__._init_extension_manager()
            for _, obj in self._extension_manager.items():
                self.on_load(obj)

    def __getitem__(self, name):
        return self._extension_manager[name]

    def get_driver(self, name):
        return self[name].obj

    # NOTE(tenbrae): Use lockutils to avoid a potential race in eventlet
    #             that might try to create two driver factories.
    @classmethod
    @lockutils.synchronized(EM_SEMAPHORE)
    def _init_extension_manager(cls):
        # NOTE(tenbrae): In case multiple greenthreads queue up on this lock
        #             before _extension_manager is initialized, prevent
        #             creation of multiple NameDispatchExtensionManagers.
        if cls._extension_manager:
            return
        enabled_drivers = getattr(CONF, cls._enabled_driver_list_config_option, [])

        # Check for duplicated driver entries and warn the operator
        # about them
        counter = collections.Counter(enabled_drivers).items()
        duplicated_drivers = []
        cls._enabled_driver_list = []
        for item, cnt in counter:
            if not item:
                LOG.warning(
                    'An empty driver was specified in the "%s" '
                    "configuration option and will be ignored. Please "
                    "fix your ironic.conf file to avoid this warning "
                    "message.",
                    cls._enabled_driver_list_config_option,
                )
                continue
            if cnt > 1:
                duplicated_drivers.append(item)
            cls._enabled_driver_list.append(item)
        if duplicated_drivers:
            LOG.warning(
                'The driver(s) "%s" is/are duplicated in the '
                "list of enabled_drivers. Please check your "
                "configuration file.",
                ", ".join(duplicated_drivers),
            )

        # NOTE(tenbrae): Drivers raise "DriverLoadError" if they are unable to
        #             be loaded, eg. due to missing external dependencies.
        #             We capture that exception, and, only if it is for an
        #             enabled driver, raise it from here. If enabled driver
        #             raises other exception type, it is wrapped in
        #             "DriverLoadError", providing the name of the driver that
        #             caused it, and raised. If the exception is for a
        #             non-enabled driver, we suppress it.
        def _catch_driver_not_found(mgr, ep, exc):
            # NOTE(tenbrae): stevedore loads plugins *before* evaluating
            #             _check_func, so we need to check here, too.
            if ep.name in cls._enabled_driver_list:
                if not isinstance(exc, exception.DriverLoadError):
                    raise exception.DriverLoadError(driver=ep.name, reason=exc)
                raise exc

        def missing_callback(names):
            names = ", ".join(names)
            raise exception.DriverNotFoundInEntrypoint(
                names=names, entrypoint=cls._entrypoint_name
            )

        cls._extension_manager = _create_extension_manager(
            cls._entrypoint_name,
            cls._enabled_driver_list,
            on_load_failure_callback=_catch_driver_not_found,
            on_missing_entrypoints_callback=missing_callback,
        )

        LOG.info(f"Loaded the following drivers: {cls._extension_manager.names()}")

    @property
    def names(self):
        """The list of driver names available."""
        return self._extension_manager.names()

    def items(self):
        """Iterator over pairs (name, instance)."""
        return ((ext.name, ext.obj) for ext in self._extension_manager)

    def on_load(self, obj):
        """Optional callback to invoke on each loaded driver."""
        pass


class HardwareTypeFactory(BaseDriverFactory):
    _entrypoint_name = "doni.driver.hardware_type"
    _enabled_driver_list_config_option = "enabled_hardware_types"
    _logging_template = "Loaded the following hardware types: %s"


class WorkerTypeFactory(BaseDriverFactory):
    _entrypoint_name = "doni.driver.worker_type"
    _enabled_driver_list_config_option = "enabled_worker_types"
    _logging_template = "Loaded the following worker types: %s"

    def on_load(self, extension):
        worker = extension.obj
        if callable(getattr(worker, "register_opts", None)):
            worker.register_opts(CONF)
