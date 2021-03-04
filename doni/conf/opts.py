from oslo_log import log

import doni.conf

_opts = [
    (conf.GROUP, conf.opts) for conf in [
        doni.conf.default,
        doni.conf.worker,
    ]
]


def list_opts():
    """Return a list of oslo.config options available in Doni code.

    The returned list includes all oslo.config options. Each element of
    the list is a tuple. The first element is the name of the group, the
    second element is the options.

    The function is discoverable via the 'doni' entry point under the
    'oslo.config.opts' namespace.

    The function is used by Oslo sample config file generator to discover the
    options.

    Returns:
        list[(str,?)]: A list of (group, options) tuples
    """
    return _opts


def update_opt_defaults():
    log.set_defaults(
        default_log_levels=[
            'amqp=WARNING',
            'amqplib=WARNING',
            'qpid.messaging=INFO',
            # This comes in two flavors
            'oslo.messaging=INFO',
            'oslo_messaging=INFO',
            'sqlalchemy=WARNING',
            'stevedore=INFO',
            'eventlet.wsgi.server=INFO',
            'iso8601=WARNING',
            'requests=WARNING',
            'urllib3.connectionpool=WARNING',
            'keystonemiddleware.auth_token=INFO',
            'keystoneauth.session=INFO',
            'openstack=WARNING',
        ]
    )
