from flask import Blueprint, request

from doni.api import utils as api_utils
from doni.api.hooks import route
from doni.common import args, driver_factory
from doni.common.policy import authorize
from doni.objects import transaction
from doni.objects.availability_window import AvailabilityWindow
from doni.objects.hardware import Hardware
from doni.objects.worker_task import WorkerTask
from doni.worker import WorkerField, WorkerState

bp = Blueprint("hardware", __name__)

SENSITIVE_MASK = "*" * 12

DEFAULT_FIELDS = (
    "name",
    "project_id",
    "hardware_type",
    "properties",
)
WORKER_TASK_DEFAULT_FIELDS = (
    "worker_type",
    "state",
    "state_details",
)

HARDWARE_ENROLL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": args.STRING,
        "hardware_type": args.STRING,
        "properties": {
            "type": "object",
            "additionalProperties": True,
        },
    },
    "required": ["name", "hardware_type", "properties"],
    "additionalProperties": False,
}
HARDWARE_UPDATE_ALLOWED_FIELDS = (
    "name",
    "hardware_type",
    "properties",
    "availability",
)

AVAILABILITY_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "start": args.optional(args.DATETIME),
        "end": args.optional(args.DATETIME),
    },
    "required": ["start", "end"],
}
AVAILABILITY_WINDOW_UPDATE_ALLOWED_FIELDS = (
    "start",
    "end",
)
AVAILABILITY_WINDOW_VALID_BASE = {
    "start": "2020-01-01T00:00:00Z",
    "end": "2020-01-01T00:00:00Z",
}


def hardware_validator():
    enabled_workers = driver_factory.worker_types()
    enabled_hardware_types = driver_factory.hardware_types()

    hardware_type_schemas = []
    for hwt_name, hwt in enabled_hardware_types.items():
        properties_schema = {
            "type": "object",
            "properties": {field.name: field.schema for field in hwt.default_fields},
            "required": [field.name for field in hwt.default_fields if field.required],
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

        hardware_type_schemas.append(
            {
                "type": "object",
                "properties": {
                    "hardware_type": {"const": hwt_name},
                    "properties": properties_schema,
                },
            }
        )

    schema = {
        "definitions": {"hardware": HARDWARE_ENROLL_SCHEMA},
        "allOf": [
            # Check base hardware schema
            {"$ref": "#/definitions/hardware"},
            # Check schema for hardware types
            {"oneOf": hardware_type_schemas},
        ],
    }

    return args.schema(schema)


def hardware_serializer(with_private_fields=False):
    """Create a hardware serializer, which can be used to render API responses.

    Args:
        with_private_fields (bool): Whether the serializer should serialize
            'private' fields declared by workers valid for the given hardware.
            Defaults to False.

    Returns:
        A function that takes a Hardware object as its first argument and returns
            a JSON-safe dictionary value representing the serialized object.
            The function additionally takes the following keyword arguments:

            worker_tasks (list[WorkerTask]): A list of worker tasks to include
                on the JSON response under a "workers" top-level key.
    """
    globally_enabled_workers = driver_factory.worker_types()
    globally_enabled_hardware_types = driver_factory.hardware_types()

    def _mask_sensitive(value):
        return SENSITIVE_MASK

    def _serialize(hardware: "Hardware", worker_tasks: "list[WorkerTask]" = None):
        hardware_json = api_utils.object_to_dict(hardware, fields=DEFAULT_FIELDS)
        properties = hardware_json["properties"].copy()

        hwt = globally_enabled_hardware_types[hardware.hardware_type]
        worker_fields: "list[WorkerField]" = hwt.default_fields.copy()
        for worker_type in hwt.enabled_workers:
            if worker_type not in globally_enabled_workers:
                continue
            worker_fields.extend(globally_enabled_workers[worker_type].fields)

        # Filter all hardware properties down based on what workers are active
        # for the given hardware.
        filtered_properties = {}
        for field in worker_fields:
            if with_private_fields or not field.private:
                value = properties.get(field.name)
                if value is None:
                    # Don't serialize 'None'
                    continue
                filtered_properties[field.name] = (
                    _mask_sensitive(value) if field.sensitive else value
                )

        hardware_json["properties"] = filtered_properties

        if worker_tasks is not None:
            hardware_json["workers"] = [
                api_utils.object_to_dict(
                    wt,
                    include_created_at=False,
                    include_updated_at=False,
                    include_uuid=False,
                    fields=WORKER_TASK_DEFAULT_FIELDS,
                )
                for wt in worker_tasks
            ]

        return hardware_json

    return _serialize


@route("/", methods=["GET"], blueprint=bp)
def get_all():
    ctx = request.context
    project_id = None if request.args.get("all_projects") else ctx.project_id
    limit = request.args.get("limit")
    marker = request.args.get("marker")
    sort_key = request.args.get("sort_key")
    sort_dir = request.args.get("sort_dir")

    authorize("hardware:get", ctx, {"project_id": project_id})
    serialize = hardware_serializer(with_private_fields=True)
    hardwares = Hardware.list(
        ctx,
        limit=limit,
        marker=marker,
        sort_key=sort_key,
        sort_dir=sort_dir,
        project_id=project_id,
    )
    links = []
    if hardwares and len(hardwares) == limit:
        links.append(
            {
                "href": api_utils.get_next_href(request, marker=hardwares[-1].uuid),
                "rel": "next",
            }
        )

    # Also batch-fetch all associated worker tasks
    worker_tasks = WorkerTask.list_for_hardwares(ctx, [hw.uuid for hw in hardwares])

    return {
        "hardware": [serialize(hw, worker_tasks.get(hw.uuid)) for hw in hardwares],
        "links": links,
    }


@route("/export", methods=["GET"], blueprint=bp)
def export():
    ctx = request.context
    serialize = hardware_serializer(with_private_fields=False)
    return {
        "hardware": [serialize(hw) for hw in Hardware.list(ctx)],
    }


@route("/<hardware_uuid>", methods=["GET"], blueprint=bp)
@args.validate(hardware_uuid=args.uuid)
def get_one(hardware_uuid=None):
    ctx = request.context
    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:get", ctx, hardware)
    serialize = hardware_serializer(with_private_fields=True)
    worker_tasks = WorkerTask.list_for_hardware(ctx, hardware_uuid)
    return serialize(hardware, worker_tasks=worker_tasks)


@route("/<hardware_uuid>", methods=["DELETE"], blueprint=bp)
@args.validate(hardware_uuid=args.uuid)
def destroy(hardware_uuid=None):
    ctx = request.context
    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:delete", ctx, hardware)
    with transaction():
        hardware.destroy()
        _mark_tasks_pending(WorkerTask.list_for_hardware(ctx, hardware_uuid))
    return None


@route("/", methods=["POST"], json_body="hardware_params", blueprint=bp)
@args.validate(hardware_params=hardware_validator())
def create(hardware_params=None):
    ctx = request.context
    assert hardware_params is not None
    # Hardware will be owned by requesting user's project
    hardware_params["project_id"] = ctx.project_id
    hardware_params.setdefault("properties", {})

    hardware = Hardware(ctx, **hardware_params)
    authorize("hardware:create", ctx)
    serialize = hardware_serializer(with_private_fields=True)
    hardware.create()
    worker_tasks = WorkerTask.list_for_hardware(ctx, hardware.uuid)
    return serialize(hardware, worker_tasks=worker_tasks), 201


@route("/<hardware_uuid>", methods=["PATCH"], json_body="patch", blueprint=bp)
@args.validate(hardware_uuid=args.uuid, patch=args.schema(args.PATCH))
def update(hardware_uuid=None, patch=None):
    ctx = request.context
    assert patch is not None

    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:update", ctx, hardware)
    serialize = hardware_serializer(with_private_fields=True)

    state = {
        "self": hardware,
    }

    api_utils.patch_validate(patch, allowed_fields=HARDWARE_UPDATE_ALLOWED_FIELDS)

    is_updating_availability = api_utils.is_path_updated(patch, "/availability")

    if is_updating_availability:
        api_utils.patch_validate_list(
            patch,
            prefix="/availability",
            allowed_fields=AVAILABILITY_WINDOW_UPDATE_ALLOWED_FIELDS,
            validate_schema=args.schema(AVAILABILITY_WINDOW_SCHEMA),
            validation_base=AVAILABILITY_WINDOW_VALID_BASE,
        )
        # If updating availability windows, pull current values to compute the
        # delta from the patch.
        state["availability"] = {
            aw.uuid: aw
            for aw in AvailabilityWindow.list_for_hardware(ctx, hardware_uuid)
        }

    patched_state = api_utils.apply_jsonpatch(state, patch)

    with transaction():
        api_utils.apply_patch_updates(hardware, patched_state)
        hardware.save()

        if is_updating_availability:
            to_add, to_update, to_remove = api_utils.apply_patch_updates_to_list(
                state["availability"],
                patched_state["availability"],
                obj_class=AvailabilityWindow,
                context=ctx,
            )
            for window in to_add:
                window.hardware_uuid = hardware.uuid
                window.create()
            for window in to_update:
                window.save()
            for window in to_remove:
                window.destroy()

        worker_tasks = WorkerTask.list_for_hardware(ctx, hardware_uuid)
        _mark_tasks_pending(worker_tasks)

    return serialize(hardware, worker_tasks=worker_tasks)


@route("/<hardware_uuid>/sync", methods=["POST"], blueprint=bp)
@args.validate(hardware_uuid=args.uuid)
def sync(hardware_uuid=None):
    ctx = request.context
    hardware = Hardware.get_by_uuid(ctx, hardware_uuid)
    authorize("hardware:update", ctx, hardware)
    with transaction():
        _mark_tasks_pending(WorkerTask.list_for_hardware(ctx, hardware_uuid))
    return None


def _mark_tasks_pending(worker_tasks: "list[WorkerTask]"):
    for task in worker_tasks:
        if not (task.is_pending or task.is_in_progress):
            # Take care not to interrupt tasks in progress
            task.state = WorkerState.PENDING
            task.save()
