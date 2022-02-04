from textwrap import shorten

from keystoneauth1 import exceptions as kaexception

from doni.common import exception, keystone
from doni.driver.util import ks_service_requestor

BLAZAR_API_VERSION = "1"
BLAZAR_API_MICROVERSION = "1.0"
BLAZAR_DATE_FORMAT = "%Y-%m-%d %H:%M"
_BLAZAR_ADAPTER = None


class BlazarUnavailable(exception.DoniException):
    """Exception for when the Blazar service cannot be contacted."""

    _msg_fmt = (
        "Could not contact Blazar API. Please check the service "
        "configuration. The precise error was: %(message)s"
    )


class BlazarIsWrongError(exception.DoniException):
    """Exception for when the Blazar service is in a bad state of some kind."""

    _msg_fmt = "Blazar is in a bad state. The precise error was: %(message)s"


class BlazarAPIError(exception.DoniException):
    """Exception for an otherwise unhandled error passed from Blazar's API."""

    _msg_fmt = "Blazar responded with HTTP %(code)s: %(text)s"


class BlazarAPIMalformedResponse(exception.DoniException):
    """Exception for malformed response from Blazar's API."""

    _msg_fmt = "Blazar response malformed: %(text)s"


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
