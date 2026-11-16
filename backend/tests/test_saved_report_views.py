"""saved_report_views — M108 P2 saved-report presets round-trip (docs/09).

A saved view is a per-user preference stored on the `users` row exactly like
M97's `dashboard_preset` (docs/02 §users): it defaults to `[]`, survives a
fresh session — i.e. a reload — and belongs to the user who saved it. The
shape is a closed contract (`id`, `title`, `tab`, `kind`, `state`), so the
server rejects a preset it could never restore instead of storing a dead one.

`kind` picks the `state` schema: `reports` (M108's eight keys) or
`transactions` (M123's four search params), and it is always echoed on reads
so a client never has to infer which contract a stored row follows.
"""

from fastapi.testclient import TestClient

API = "/api/v1"
REGISTER = f"{API}/auth/register"
LOGIN = f"{API}/auth/login"
ME = f"{API}/users/me"
PASSWORD = "correct-horse-battery"


def _headers(client: TestClient, email: str) -> dict[str, str]:
    client.post(REGISTER, json={"email": email, "password": PASSWORD})
    login = client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _view(
    view_id: str = "v-abcdefgh",
    *,
    tab: str = "spending",
    kind: str = "reports",
    **state: object,
) -> dict:
    return {"id": view_id, "title": "Groceries YTD", "tab": tab, "kind": kind, "state": state}


def test_defaults_to_empty_and_round_trips_across_sessions(client: TestClient) -> None:
    headers = _headers(client, "sv-user@example.com")
    assert client.get(ME, headers=headers).json()["saved_report_views"] == []

    # `from`/`to` are the JSON keys the Reports page restores (docs/09).
    saved = [
        _view(
            **{
                "from": "2026-01-01",
                "to": "2026-09-30",
                "granularity": "month",
                "view": "treemap",
            }
        )
    ]
    patched = client.patch(ME, json={"saved_report_views": saved}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["saved_report_views"] == saved

    fresh = _headers(client, "sv-user@example.com")
    assert client.get(ME, headers=fresh).json()["saved_report_views"] == saved


def test_rename_and_delete_are_whole_list_writes(client: TestClient) -> None:
    headers = _headers(client, "sv-edit@example.com")
    original = [_view(), _view("v-bbbbbbbb", tab="cashflow")]
    assert (
        client.patch(ME, json={"saved_report_views": original}, headers=headers).status_code == 200
    )

    renamed = [{**original[0], "title": "Renamed"}]
    assert (
        client.patch(ME, json={"saved_report_views": renamed}, headers=headers).json()[
            "saved_report_views"
        ]
        == renamed
    )

    assert (
        client.patch(ME, json={"saved_report_views": []}, headers=headers).json()[
            "saved_report_views"
        ]
        == []
    )


def test_unknown_tab_is_rejected_and_changes_nothing(client: TestClient) -> None:
    headers = _headers(client, "sv-badtab@example.com")
    bad = client.patch(ME, json={"saved_report_views": [_view(tab="taxes")]}, headers=headers)
    assert bad.status_code == 422
    assert client.get(ME, headers=headers).json()["saved_report_views"] == []


def test_state_is_a_closed_set(client: TestClient) -> None:
    headers = _headers(client, "sv-state@example.com")

    unknown_key = [_view(nonsense="1")]
    assert (
        client.patch(ME, json={"saved_report_views": unknown_key}, headers=headers).status_code
        == 422
    )

    bad_granularity = [_view(granularity="fortnight")]
    assert (
        client.patch(ME, json={"saved_report_views": bad_granularity}, headers=headers).status_code
        == 422
    )

    bad_date = [_view(**{"from": "01/02/2026"})]
    assert (
        client.patch(ME, json={"saved_report_views": bad_date}, headers=headers).status_code == 422
    )

    bad_group_by = [_view(group_by="household")]
    assert (
        client.patch(ME, json={"saved_report_views": bad_group_by}, headers=headers).status_code
        == 422
    )

    # M108 P3: the Owner dimension's lens round-trips when it IS offered.
    ok_group_by = [_view(group_by="owner")]
    assert (
        client.patch(ME, json={"saved_report_views": ok_group_by}, headers=headers).json()[
            "saved_report_views"
        ]
        == ok_group_by
    )
    assert client.patch(ME, json={"saved_report_views": []}, headers=headers).status_code == 200

    # M124: the Tag lens is the control's third option, so a saved view has to
    # restore it exactly like the other two (docs/12 §Tag dimension).
    ok_tag_group_by = [_view(group_by="tag")]
    assert (
        client.patch(ME, json={"saved_report_views": ok_tag_group_by}, headers=headers).json()[
            "saved_report_views"
        ]
        == ok_tag_group_by
    )
    assert client.patch(ME, json={"saved_report_views": []}, headers=headers).status_code == 200

    bad_sort_desc = [_view(sort_desc="yes")]
    assert (
        client.patch(ME, json={"saved_report_views": bad_sort_desc}, headers=headers).status_code
        == 422
    )

    assert client.get(ME, headers=headers).json()["saved_report_views"] == []


def test_duplicate_ids_and_oversized_lists_are_rejected(client: TestClient) -> None:
    headers = _headers(client, "sv-dup@example.com")
    dupes = [_view(), _view()]
    assert client.patch(ME, json={"saved_report_views": dupes}, headers=headers).status_code == 422

    too_many = [_view(f"v-{i:07d}") for i in range(51)]
    assert (
        client.patch(ME, json={"saved_report_views": too_many}, headers=headers).status_code == 422
    )

    blank_title = [_view()]
    blank_title[0]["title"] = "   "
    assert (
        client.patch(ME, json={"saved_report_views": blank_title}, headers=headers).status_code
        == 422
    )

    assert client.get(ME, headers=headers).json()["saved_report_views"] == []


def test_saved_views_are_per_user(client: TestClient) -> None:
    a = _headers(client, "sv-a@example.com")
    b = _headers(client, "sv-b@example.com")

    saved = [_view()]
    assert client.patch(ME, json={"saved_report_views": saved}, headers=a).status_code == 200
    assert client.get(ME, headers=b).json()["saved_report_views"] == []
    assert client.get(ME, headers=a).json()["saved_report_views"] == saved


# --- M123 (docs/09 §Create from search) — kind="transactions" ---------------


def _tx_view(
    view_id: str = "v-tx-aaaaaa", *, title: str = "Metro groceries", **state: object
) -> dict:
    """A transaction view: the four `/transactions` search params verbatim."""
    return {
        "id": view_id,
        "title": title,
        "tab": "transactions",
        "kind": "transactions",
        "state": state,
    }


def test_transaction_views_round_trip_the_four_search_params(client: TestClient) -> None:
    headers = _headers(client, "sv-tx@example.com")

    # A one-param view and a full four-param view both save and come back
    # byte-identical — `state` IS the `?…` query, so restoring is a navigation.
    one = [_tx_view("v-tx-oneone", q="metro")]
    four = [_tx_view("v-tx-fourfour", q="rogers", category="5", account="3", tag="1,2")]
    patched = client.patch(ME, json={"saved_report_views": one + four}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["saved_report_views"] == one + four

    # A fresh session (= a reload) still has them, and `kind` is echoed so the
    # client never has to infer which state schema the row follows.
    fresh = _headers(client, "sv-tx@example.com")
    assert client.get(ME, headers=fresh).json()["saved_report_views"] == one + four

    # `uncategorized` is a real filter value, not an id.
    uncat = [_tx_view(category="uncategorized")]
    assert (
        client.patch(ME, json={"saved_report_views": uncat}, headers=headers).json()[
            "saved_report_views"
        ]
        == uncat
    )

    # Clearing is the same whole-list write M108 established.
    assert (
        client.patch(ME, json={"saved_report_views": []}, headers=headers).json()[
            "saved_report_views"
        ]
        == []
    )


def test_transaction_state_is_a_closed_set_of_valid_params(client: TestClient) -> None:
    headers = _headers(client, "sv-tx2@example.com")

    rejected = [
        # A report-only key under the wrong kind — the two schemas never mix.
        _tx_view(**{"from": "2026-01-01"}),
        _tx_view(bogus="x"),
        _tx_view(category="groceries"),
        _tx_view(account="abc"),
        _tx_view(tag="1,;2"),
        _tx_view(q=""),
        _tx_view(q="x" * 241),
        # `kind` and `tab` must describe the same surface, both ways.
        {**_tx_view(), "tab": "spending"},
        {**_view(), "tab": "transactions"},
        {**_view(), "kind": "banana"},
        {**_tx_view(), "kind": "reports"},
    ]
    for bad in rejected:
        assert (
            client.patch(ME, json={"saved_report_views": [bad]}, headers=headers).status_code == 422
        ), bad

    assert client.get(ME, headers=headers).json()["saved_report_views"] == []
