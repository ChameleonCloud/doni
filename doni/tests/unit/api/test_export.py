import pytest
from doni.tests.unit import utils
from flask.testing import FlaskClient


def _test_validate_export(res):
    # one hardware object
    assert len(res.json["hardware"]) == 1

    hw_item = res.json["hardware"][0]
    assert hw_item["properties"] == {
        "public-field": "fake-public-field",
        "public-and-sensitive-field": "************",
    }


@pytest.mark.parametrize(
    "use_headers",
    [
        pytest.param(True, id="auth"),
        pytest.param(False, id="anonymous"),
    ],
)
def test_export_hardware_public(
    use_headers,
    user_auth_headers,
    mocker,
    client: "FlaskClient",
    database: "utils.DBFixtures",
):
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    hw = database.add_hardware()
    if use_headers:
        headers = user_auth_headers
    else:
        headers = None
    res = client.get(f"/v1/hardware/export/", headers=headers)
    assert res.status_code == 200

    # this is a public endpoint, this should fail
    mock_authorize.assert_not_called()

    _test_validate_export(res)
