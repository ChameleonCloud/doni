from collections import deque
import logging
import os

from flask import Flask
from oslo_middleware import healthcheck
from werkzeug.middleware import dispatcher as wsgi_dispatcher

from doni.api import hooks
from doni.conf import CONF


def _add_vary_x_auth_token_header(res):
    res.headers['Vary'] = 'X-Auth-Token'
    return res


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)

    if test_config:
        app.config.from_mapping(test_config)
    else:
        # TODO: set anything relevant from CONF
        app.config.update(
            PROPAGATE_EXCEPTIONS=True
        )

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Register Error Handler Function for Keystone Errors.
    # NOTE(morgan): Flask passes errors to an error handling function. All of
    # keystone's api errors are explicitly registered in
    # keystone.exception.KEYSTONE_API_EXCEPTIONS and those are in turn
    # registered here to ensure a proper error is bubbled up to the end user
    # instead of a 500 error.
    # for exc in exception.KEYSTONE_API_EXCEPTIONS:
    #     app.register_error_handler(exc, _handle_keystone_exception)

    # Register extra (python) exceptions with the proper exception handler,
    # specifically TypeError. It will render as a 400 error, but presented in
    # a "web-ified" manner
    # app.register_error_handler(TypeError, _handle_unknown_keystone_exception)

    # Add core before request functions
    # app.before_request(req_logging.log_request_info)
    # app.before_request(json_body.json_body_before_request)
    middlewares = [
        hooks.AuthTokenFlaskMiddleware(),
        hooks.ContextMiddleware(),
    ]
    deque(
        app.before_request(m.before_request)
        for m in middlewares if hasattr(m, 'before_request')
    )
    deque(
        app.after_request(m.after_request)
        for m in reversed(middlewares) if hasattr(m, 'after_request')
    )
    app.after_request(_add_vary_x_auth_token_header)

    from .api import availability_window
    from .api import hardware
    from .api import root
    app.register_blueprint(root.bp)
    app.register_blueprint(hardware.bp, url_prefix="/v1/hardware")
    app.register_blueprint(availability_window.bp, url_prefix="/v1/hardware")
    app.logger.info("Registered apps")

    # Propagate gunicorn log level to Flask
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.setLevel(gunicorn_logger.level)

    app_mounts = {}

    # oslo_middleware healthcheck is a separate app; mount it at
    # the well-known /healthcheck endpoint.
    hc_app = healthcheck.Healthcheck.app_factory(
        {}, oslo_config_project=PROJECT_NAME)
    app_mounts['/healthcheck'] = hc_app

    app.wsgi_app = wsgi_dispatcher.DispatcherMiddleware(
        app.wsgi_app,
        app_mounts)

    return app
