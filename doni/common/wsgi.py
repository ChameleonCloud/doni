from oslo_concurrency import processutils
from oslo_service import service
from oslo_service import wsgi

from doni.doni_flask import create_app
from doni.common import exception
from doni.conf import CONF


_MAX_DEFAULT_WORKERS = 4


class WSGIService(service.ServiceBase):
    """Provides ability to launch doni API from wsgi app."""

    def __init__(self, name, use_ssl=False):
        """Initialize, but do not start the WSGI server.

        :param name: The name of the WSGI server given to the loader.
        :param use_ssl: Wraps the socket in an SSL context if True.
        :returns: None
        """
        self.name = name
        self.app = create_app()
        self.workers = (
            CONF.api.api_workers
            # NOTE(dtantsur): each worker takes a substantial amount of memory,
            # so we don't want to end up with dozens of them.
            or min(processutils.get_worker_count(), _MAX_DEFAULT_WORKERS)
        )
        if self.workers and self.workers < 1:
            raise exception.ConfigInvalid(
                _("api_workers value of %d is invalid, " "must be greater than 0.")
                % self.workers
            )

        self.server = wsgi.Server(
            CONF,
            name,
            self.app,
            host=CONF.api.host_ip,
            port=CONF.api.port,
            use_ssl=use_ssl,
        )

    def start(self):
        """Start serving this service using loaded configuration.

        :returns: None
        """
        self.server.start()

    def stop(self):
        """Stop serving this API.

        :returns: None
        """
        self.server.stop()

    def wait(self):
        """Wait for the service to stop serving this API.

        :returns: None
        """
        self.server.wait()

    def reset(self):
        """Reset server greenpool size to default.

        :returns: None
        """
        self.server.reset()
