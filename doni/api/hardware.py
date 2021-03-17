from flask import Blueprint
from flask import request

from doni.api.hooks import route
from doni.api import utils as api_utils
from doni.common import args
from doni.common import driver_factory
from doni.common.policy import authorize
from doni.objects import transaction
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.objects.worker_task import WorkerTask
from doni.worker import WorkerField

bp = Blueprint("hardware", __name__)


DEFAULT_FIELDS = ('name', 'project_id', 'hardware_type', 'properties',)
WORKER_TASK_DEFAULT_FIELDS = ('worker_type', 'state', 'state_details',)

HARDWARE_SCHEMA = {
    'type': 'object',
    'properties': {
        'name': args.STRING,
        'uuid': args.STRING,
        'hardware_type': args.STRING,
        'project_id': args.STRING,
        'properties': {'type': 'object', 'additionalProperties': True},
    },
    'additionalProperties': False,
}


def hardware_validator():
    enabled_workers = driver_factory.worker_types()
    enabled_hardware_types = driver_factory.hardware_types()

    hardware_type_schemas = []
    for hwt_name, hwt in enabled_hardware_types.items():
        properties_schema = {
            "type": "object",
            "properties": {
                field.name: field.schema
                for field in hwt.default_fields
            },
            "required": [
                field.name for field in hwt.default_fields
                if field.required
            ],
            # Disallow keys that don't match any worker
            "additionalProperties": False,
        }
        for worker_name, worker in enabled_workers.items():
            if worker_name not in hwt.enabled_workers:
                continue
            worker_schema = worker.json_schema()
            properties_schema["properties"].update(worker_schema["properties"])
            properties_schema["required"].extend(worker_schema["required"])

        # JSONSchema doesn't like 'required' to be an empty array.
        if not properties_schema["required"]:
            del properties_schema["required"]

        hardware_type_schemas.append({
            "type": "object",
            "properties": {
                "hardware_type": {"const": hwt_name},
                "properties": properties_schema,
            }
        })

    schema = {
        "definitions": {
            "hardware": HARDWARE_SCHEMA
        },
        "allOf": [
            # Check base hardware schema
            {"$ref": "#/definitions/hardware"},
            # Check schema for hardware types
            {"oneOf": hardware_type_schemas},
        ]
    }

    return args.schema(schema)


def hardware_serializer(with_private_fields=False):
    """Create a hardware serializer, which can be used to render API responses.

    Args:
        with_private_fields (bool): Whether the serializer should serialize
            'private' fields declared by workers valid for the given hardware.
            Defaults to False.

    Returns:
        A function that takes a Hardware object as its sole argument and returns
            a JSON-safe dictionary value representing the serialized object.
    """
    enabled_workers = driver_factory.worker_types()
    enabled_hardware_types = driver_factory.hardware_types()

    def _mask_sensitive(value):
        return "*" * 12

    def _serialize(hardware: "Hardware"):
        hardware_json = api_utils.object_to_dict(hardware, fields=DEFAULT_FIELDS)
        properties = hardware_json["properties"].copy()

        hwt = enabled_hardware_types[hardware.hardware_type]
        worker_fields: "list[WorkerField]" = hwt.default_fields.copy()
        for worker_type in hwt.enabled_workers:
            worker_fields.extend(enabled_workers[worker_type].fields)

        # Filter all hardware properties down based on what workers are active
        # for the given hardware.
        filtered_properties = {}
        for field in worker_fields:
            if with_private_fields or not field.private:
                value = properties.get(field.name, field.default)
                if value is None:
                    # Don't serialize 'None'
                    continue
                filtered_properties[field.name] = (
                    _mask_sensitive(value) if field.sensitive else value)

        hardware_json["properties"] = filtered_properties
        return hardware_json

    return _serialize


@route("/", methods=["GET"], blueprint=bp)
def get_all():
    ctx = request.context
    authorize("hardware:get", ctx)
    serialize = hardware_serializer(with_private_fields=True)
    return {
        "hardware": [serialize(hw) for hw in Hardware.list(ctx)],
    }


@route("/export/", methods=["GET"], blueprint=bp)
def export():
    ctx = request.context
    serialize = hardware_serializer(with_private_fields=False)
    return {
        "hardware": [serialize(hw) for hw in Hardware.list(ctx)],
    }

@route("/<hardware_uuid>/", methods=["GET"], blueprint=bp)
@args.validate(hardware_uuid=args.uuid)
def get_one(hardware_uuid=None):
    ctx = request.context
    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:get", ctx, hardware)
    serialize = hardware_serializer(with_private_fields=True)
    response = serialize(hardware)
    response["workers"] = [
        api_utils.object_to_dict(
            wt,
            include_created_at=False,
            include_updated_at=False,
            include_uuid=False,
            fields=WORKER_TASK_DEFAULT_FIELDS
        )
        for wt in WorkerTask.list_for_hardware(ctx, hardware_uuid)
    ]
    return response


@route("/", methods=["POST"], json_body="hardware_params", blueprint=bp)
@args.validate(hardware_params=hardware_validator())
def create(hardware_params=None):
    ctx = request.context

    hardware = Hardware(ctx, **hardware_params)
    authorize("hardware:create", ctx, hardware)
    serialize = hardware_serializer(with_private_fields=True)
    hardware.create()

    return serialize(hardware), 201


@route("/<hardware_uuid>/", methods=["PATCH"], json_body="patch", blueprint=bp)
@args.validate(hardware_uuid=args.uuid, patch=args.schema(args.PATCH))
def update(hardware_uuid=None, patch=None):
    ctx = request.context

    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:update", ctx, hardware)
    serialize = hardware_serializer(with_private_fields=True)

    state = {
        "self": hardware,
    }

    if api_utils.is_path_updated(patch, "/availability"):
        # If updating availability windows, pull current values to compute the
        # delta from the patch.
        state["availability"] = AvailabilityWindow.list(ctx, hardware_uuid)

    patched_state = api_utils.apply_jsonpatch(state, patch)

    with transaction():
        api_utils.apply_patch_updates(hardware, patched_state)
        hardware.save()

        if "availability" in patched_state:
            to_add, to_update, to_remove = api_utils.apply_patch_updates_to_list(
                state["availability"], patched_state["availability"])
            for window in to_add:
                window.create()
            for window in to_update:
                window.save()
            for window in to_remove:
                window.destroy()

    return serialize(hardware)
