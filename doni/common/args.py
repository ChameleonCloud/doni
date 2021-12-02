import inspect
import re
from functools import partial, wraps
from typing import TYPE_CHECKING

import jsonschema
from oslo_utils import uuidutils

from doni.common import exception

if TYPE_CHECKING:
    from typing import Optional


def uuid(name, value) -> "Optional[str]":
    """Validate that the value is a UUID

    Args:
        name (str): Name of the argument
        value (any): A UUID string value

    Returns:
        The value, or None if value is None

    Raises:
        InvalidParameterValue: if the value is not a valid UUID
    """
    if value is None:
        return
    if not uuidutils.is_uuid_like(value):
        raise exception.InvalidParameterValue(
            ("Expected UUID for %s: %s") % (name, value)
        )
    return value


# Some JSON schema helpers
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}  # can be integer or floating-point
BOOLEAN = {"type": "boolean"}
DATETIME = {"type": "string", "format": "date-time"}
UUID = {"type": "string", "format": "uuid"}
EMAIL = {"type": "string", "format": "email"}
PORT_RANGE = {"type": "integer", "minimum": 1, "maximum": 65536}
HOST_OR_IP = {
    "anyOf": [
        {"type": "string", "format": "hostname"},
        {"type": "string", "format": "ipv4"},
        {"type": "string", "format": "ipv6"},
    ]
}
CPU_ARCH = {"type": "string", "enum": ["x86_64", "aarch64"]}
PATCH = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "pattern": "^(/[\\w-]+)+$"},
            # Only support a subset of possible operations
            # http://jsonpatch.com/#operations
            "op": {"type": "string", "enum": ["add", "replace", "remove"]},
            "value": {},
        },
        "additionalProperties": False,
        "required": ["op", "path"],
    },
}


def enum(values, type="string"):
    return {"type": type, "enum": values}


def optional(schema):
    return {
        "anyOf": [
            schema,
            {"type": "null"},
        ]
    }


def array(schema, min_items=0):
    return {"type": "array", "items": schema, "minItems": min_items}


def _validate_schema(name, value, schema):
    if value is None:
        return
    try:
        jsonschema.validate(
            value,
            schema,
            cls=jsonschema.Draft7Validator,
            format_checker=jsonschema.draft7_format_checker,
        )
    except jsonschema.exceptions.ValidationError as e:
        # The error message includes the whole schema which can be very
        # large and unhelpful, so truncate it to be brief and useful
        details = str(e).split("\n")[:3]
        error_msg = f"Schema error for {name}: {details[0]}"
        schema_loc = re.sub("^(.*in schema)", "", details[-1])
        # SUPER hacky bracket-to-dot-notation thing
        schema_loc = schema_loc.replace("']", "").replace("['", ".")
        error_msg += f" (in '{schema_loc[:-1]}')"  # Strip trailing ':'
        raise exception.InvalidParameterValue(error_msg)
    return value


def schema(schema):
    """Return a validator function which validates the value with jsonschema

    Args:
        schema (dict): JSON schema to validate with.

    Returns:
        A validator function, which takes name and value arguments.

    Raises:
        jsonschema.SchemaError: if the schema is not valid.
    """
    jsonschema.Draft7Validator.check_schema(schema)

    return partial(_validate_schema, schema=schema)


def _inspect(function):
    sig = inspect.signature(function)
    params = []

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            params.append(param)
        else:
            assert False, "Unsupported parameter kind %s %s" % (param.name, param.kind)
    return params


def validate(*args, **kwargs):
    """Decorator which validates and transforms function arguments"""
    assert not args, "Validators must be specifed by argument name"
    assert kwargs, "No validators specified"
    validators = kwargs

    def inner_function(function):
        params = _inspect(function)

        @wraps(function)
        def inner_check_args(*args, **kwargs):
            args = list(args)
            kwargs_next = {}

            # ensure each named argument belongs to a param
            kwarg_keys = set(kwargs)
            param_names = set(p.name for p in params)
            extra_args = kwarg_keys - param_names
            if extra_args:
                raise exception.InvalidParameterValue(
                    ("Unexpected arguments: %s") % ", ".join(extra_args)
                )

            args_len = len(args)

            for i, param in enumerate(params):
                val_function = validators.get(param.name)
                if not val_function:
                    continue

                if i < args_len:
                    # validate positional argument
                    args[i] = val_function(param.name, args[i])
                elif param.name in kwargs:
                    # validate keyword argument
                    kwargs_next[param.name] = val_function(
                        param.name, kwargs.pop(param.name)
                    )
                elif param.default == inspect.Parameter.empty:
                    # no argument was provided, and there is no default
                    # in the parameter, so this is a mandatory argument
                    raise exception.MissingParameterValue(
                        ("Missing mandatory parameter: %s") % param.name
                    )

            return function(*args, **kwargs_next)

        return inner_check_args

    return inner_function
