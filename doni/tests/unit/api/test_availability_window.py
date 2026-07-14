from flask.testing import FlaskClient

from doni.tests.unit import utils


def test_list_availability_windows(
    mocker, user_auth_headers, client: "FlaskClient", database: "utils.DBFixtures"
):
    mock_authorize = mocker.patch("doni.api.availability_window.authorize")
    hw = database.add_hardware()
    res = client.get(
        f"/v1/hardware/{hw['uuid']}/availability", headers=user_auth_headers
    )
    assert res.status_code == 200
    assert res.json == {
        "availability": [],
    }
    mock_authorize.assert_called_once_with("hardware:get", mocker.ANY, mocker.ANY)
