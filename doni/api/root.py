from flask import Blueprint

bp = Blueprint("root", __name__)


@bp.route("/")
def info():
    return {
        "name": "OpenStack Doni API",
        "description": (
            "Doni is an OpenStack project for managing hardware "
            "enrollment and availability."
        ),
        "versions": [
            {
                "id": "1.0",
                "status": "CURRENT"
            }
        ],
    }
