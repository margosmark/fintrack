"""M75 scheduled export — the schedule endpoints (docs/11 § "Scheduled export").

The schedule is the M56 shape mirrored: five `users` columns that roam across
devices, a full-replace PUT, and domain ranges raised in the handler so the
envelope carries `export_invalid_schedule` instead of a generic
`validation_error`. These tests pin that contract end to end.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_privacy_export import _register

SCHEDULE_URL = "/api/v1/export/schedule"

DEFAULTS = {"frequency": "off", "hour": 3, "weekday": 0, "day_of_month": 1, "keep_last_n": 5}


def test_schedule_defaults_to_off_with_the_documented_shape(
    client: TestClient,
) -> None:
    headers, _email = _register(client)
    response = client.get(SCHEDULE_URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == DEFAULTS


def test_schedule_round_trips_and_roams_per_tenant(client: TestClient) -> None:
    headers_a, _email_a = _register(client)
    headers_b, _email_b = _register(client)

    weekly = {
        "frequency": "weekly",
        "hour": 6,
        "weekday": 3,
        "day_of_month": 1,
        "keep_last_n": 12,
    }
    put = client.put(SCHEDULE_URL, headers=headers_a, json=weekly)
    assert put.status_code == 200, put.text
    assert put.json() == weekly

    assert client.get(SCHEDULE_URL, headers=headers_a).json() == weekly
    # A different tenant is untouched — the schedule lives on the user row.
    assert client.get(SCHEDULE_URL, headers=headers_b).json() == DEFAULTS

    # Full replace: an omitted field goes back to its stored value, not kept.
    back_to_off = {"frequency": "off", "hour": 3, "weekday": 0, "day_of_month": 1, "keep_last_n": 5}
    assert client.put(SCHEDULE_URL, headers=headers_a, json=back_to_off).json() == back_to_off


def test_unknown_frequency_is_a_422_export_invalid_schedule(client: TestClient) -> None:
    headers, _email = _register(client)
    body = dict(DEFAULTS, frequency="fortnightly")
    response = client.put(SCHEDULE_URL, headers=headers, json=body)
    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["code"] == "export_invalid_schedule"
    assert "export_frequency" in payload["detail"]


def test_out_of_range_values_are_a_422_export_invalid_schedule(client: TestClient) -> None:
    headers, _email = _register(client)
    cases = (
        dict(DEFAULTS, hour=24),
        dict(DEFAULTS, hour=-1),
        dict(DEFAULTS, weekday=7),
        dict(DEFAULTS, day_of_month=0),
        dict(DEFAULTS, day_of_month=29),
        dict(DEFAULTS, keep_last_n=0),
        dict(DEFAULTS, keep_last_n=21),
    )
    for body in cases:
        response = client.put(SCHEDULE_URL, headers=headers, json=body)
        assert response.status_code == 422, (body, response.text)
        payload = response.json()
        assert payload["code"] == "export_invalid_schedule", payload
        # The message names the offending column, not just "invalid".
        assert payload["detail"].split()[0].startswith("export_"), payload["detail"]


def test_a_typo_d_field_is_a_plain_validation_error(client: TestClient) -> None:
    headers, _email = _register(client)
    body = dict(DEFAULTS, keep_lastn=5)
    response = client.put(SCHEDULE_URL, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_schedule_requires_authentication(client: TestClient) -> None:
    assert client.get(SCHEDULE_URL).status_code == 401
    assert client.put(SCHEDULE_URL, json=DEFAULTS).status_code == 401
