from textwrap import shorten

from keystoneauth1 import exceptions as kaexception

from doni.common import exception, keystone

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


def call_blazar(context, path, method="get", json=None, allowed_status_codes=[]):
    try:
        blazar = _get_blazar_adapter()
        resp = blazar.request(
            path,
            method=method,
            json=json,
            microversion=BLAZAR_API_MICROVERSION,
            global_request_id=context.global_id,
            raise_exc=False,
        )
    except kaexception.ClientException as exc:
        raise BlazarUnavailable(message=str(exc))

    if resp.status_code >= 400 and resp.status_code not in allowed_status_codes:
        raise BlazarAPIError(code=resp.status_code, text=shorten(resp.text, width=50))

    try:
        # Treat empty response bodies as None
        return resp.json() if resp.text else None
    except Exception:
        raise BlazarAPIMalformedResponse(text=shorten(resp.text, width=50))
