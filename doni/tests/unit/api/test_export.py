from flask.testing import FlaskClient

from doni.tests.unit import utils


def test_export_hardware(
    mocker, user_auth_headers, client: "FlaskClient", database: "utils.DBFixtures"
):
    mock_authorize = mocker.patch("doni.api.hardware.authorize")
    hw = database.add_hardware()
    res = client.get(f"/v1/hardware/export/", headers=user_auth_headers)
    assert res.status_code == 200

    # What should this retun
    # assert res.json == {
    #     "availability": [],
    # }

    assert mock_authorize.called_once_with("hardware:get")
    print(mock_authorize.calls)
    raise Exception
