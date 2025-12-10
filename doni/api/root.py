from flask import Blueprint, request

bp = Blueprint("root", __name__)


@bp.route("/")
def info():
    # Get the base URL from the request
    base_url = request.url_root.rstrip("/")

    v1_info = {
        "id": "v1",
        "links": [{"rel": "self", "href": f"{base_url}/v1/"}],
        "status": "CURRENT",
        "updated": "2024-01-15T00:00:00Z",
        "min_version": "1.1",
        "max_version": "1.1",
    }

    version_dict = {
        "name": "OpenStack Doni API",
        "description": (
            "Doni is an OpenStack project for managing hardware "
            "enrollment and availability."
        ),
        "default_version": v1_info,
        "versions": [
            v1_info,
        ],
    }

    return version_dict
