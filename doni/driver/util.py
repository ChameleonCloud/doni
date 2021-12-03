from collections import namedtuple
from functools import wraps
from textwrap import shorten
import typing

from keystoneauth1 import exceptions as kaexception

from doni.common import exception

if typing.TYPE_CHECKING:
    from doni.common.context import RequestContext
    from keystoneauth1.adapter import Adapter
    from requests import Response
    from typing import Callable, Optional, Union


class KeystoneServiceUnavailable(exception.DoniException):
    """Exception for when the service cannot be contacted."""

    _msg_fmt = (
        "Could not contact %(service)s API. Please check the service "
        "configuration. The precise error was: %(message)s"
    )


class KeystoneServiceAPIError(exception.DoniException):
    """Exception for an otherwise unhandled error passed from the service's API."""

    _msg_fmt = "%(service)s responded with HTTP %(code)s: %(text)s"


class KeystoneServiceMalformedResponse(exception.DoniException):
    """Exception for malformed response from the service's API."""

    _msg_fmt = "%(service)s response malformed: %(text)s"


def ks_service_requestor(
    name,
    client_factory: "Callable[[],Adapter]" = None,
    microversion=None,
    parse_error=None,
) -> "Callable[[RequestContext, str, str, dict, list[int]], Union[Optional[dict],Optional[list]]]":
    @wraps
    def _request(context, path, method="get", json=None, allowed_status_codes=[]):
        try:
            blazar = client_factory()
            resp: "Response" = blazar.request(
                path,
                method=method,
                json=json,
                microversion=microversion,
                global_request_id=context.global_id,
                raise_exc=False,
            )
        except kaexception.ClientException as exc:
            raise KeystoneServiceUnavailable(service=name, message=str(exc))

        if resp.status_code >= 400 and resp.status_code not in allowed_status_codes:
            if callable(parse_error):
                error_message = parse_error(resp.text)
            else:
                error_message = shorten(resp.text, width=50)
            raise KeystoneServiceAPIError(
                service=name, code=resp.status_code, text=error_message
            )

        try:
            # Treat empty response bodies as None
            return resp.json() if resp.text else None
        except Exception:
            raise KeystoneServiceMalformedResponse(
                service=name, text=shorten(resp.text, width=50)
            )

    return _request
