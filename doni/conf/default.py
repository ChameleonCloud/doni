from os.path import abspath, join, dirname
import socket
import tempfile

from oslo_config import cfg

path_opts = [
    cfg.StrOpt('pybasedir',
               default=abspath(join(dirname(__file__), '../')),
               sample_default='/usr/lib/python/site-packages/ironic/ironic',
               help=('Directory where the ironic python module is '
                      'installed.')),
    cfg.StrOpt('bindir',
               default='$pybasedir/bin',
               help=('Directory where ironic binaries are installed.')),
    cfg.StrOpt('state_path',
               default='$pybasedir',
               help=("Top-level directory for maintaining ironic's state.")),
]

service_opts = [
    cfg.StrOpt('host',
               default=socket.getfqdn(),
               sample_default='localhost',
               help=('Name of this node. This can be an opaque identifier. '
                      'It is not necessarily a hostname, FQDN, or IP address. '
                      'However, the node name must be valid within '
                      'an AMQP key, and if using ZeroMQ (will be removed in '
                      'the Stein release), a valid hostname, FQDN, '
                      'or IP address.')),
    cfg.StrOpt('rpc_transport',
               default='oslo',
               choices=[('oslo', ('use oslo.messaging transport')),
                        ('json-rpc', ('use JSON RPC transport'))],
               help=('Which RPC transport implementation to use between '
                      'conductor and API services')),
]

exc_log_opts = [
    cfg.BoolOpt('fatal_exception_format_errors',
                default=False,
                help=('Used if there is a formatting error when generating '
                       'an exception message (a programming error). If True, '
                       'raise an exception; if False, use the unformatted '
                       'message.')),
]

utils_opts = [
    cfg.StrOpt('rootwrap_config',
               default="/etc/doni/rootwrap.conf",
               help=('Path to the rootwrap configuration file to use for '
                      'running commands as root.')),
    cfg.StrOpt('tempdir',
               default=tempfile.gettempdir(),
               sample_default=tempfile.gettempdir(),
               help=('Temporary working directory, default is Python temp '
                      'dir.')),
]

def register_opts(conf):
    conf.register_opts(path_opts)
    conf.register_opts(service_opts)
    conf.register_opts(exc_log_opts)
    conf.register_opts(utils_opts)
