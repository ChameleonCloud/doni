from doni.common import exception, keystone
from doni.driver.util import ks_service_requestor

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
BLAZAR_DATE_FORMAT = "%Y-%m-%d %H:%M"
_BLAZAR_ADAPTER = None


class BlazarIsWrongError(exception.DoniException):
    """Exception for when the Blazar service is in a bad state of some kind."""

    _msg_fmt = "Blazar is in a bad state. The precise error was: %(message)s"


def _get_blazar_adapter():
    global _BLAZAR_ADAPTER
    if not _BLAZAR_ADAPTER:
        _BLAZAR_ADAPTER = keystone.get_adapter(
            "blazar",
            session=keystone.get_session("blazar"),
            auth=keystone.get_auth("blazar"),
            version=BLAZAR_API_VERSION,
        )
    return _BLAZAR_ADAPTER


def call_blazar(*args, **kwargs):
    return ks_service_requestor("Blazar", _get_blazar_adapter)(*args, **kwargs)
