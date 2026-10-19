"""Integration tests against a real PostgreSQL (see CI workflow).

These exist because readiness probes only prove the pod can reach the
database, not that the code's SQL matches the schema. Scenario 04 (bad
release) is exactly the class of bug these tests catch before deploy.
"""

import os

import pytest

pytest.importorskip("psycopg")
if "DB_HOST" not in os.environ:
    pytest.skip("DB_HOST not set; integration tests need PostgreSQL", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_liveness_does_not_need_db(client):
    assert client.get("/healthz").status_code == 200


def test_readiness(client):
    assert client.get("/readyz").status_code == 200


def test_create_get_update_search(client):
    created = client.post(
        "/customers",
        json={"email": "ada@example.com", "full_name": "Ada Lovelace", "country": "gb"},
    )
    assert created.status_code == 201, created.text
    customer = created.json()
    assert customer["country"] == "GB"

    fetched = client.get(f"/customers/{customer['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["email"] == "ada@example.com"

    patched = client.patch(f"/customers/{customer['id']}", json={"full_name": "Ada King"})
    assert patched.status_code == 200
    assert patched.json()["full_name"] == "Ada King"

    found = client.get("/customers", params={"email": "ada@example.com"})
    assert found.status_code == 200
    assert any(row["id"] == customer["id"] for row in found.json())


def test_not_found(client):
    assert client.get("/customers/999999999").status_code == 404


def test_error_classification():
    # Regression for scenario 04: a SQL bug must be a 500 app error, not a
    # 503 "database" error that sends responders to a healthy database.
    import psycopg

    bug = main.db_failure("create", psycopg.errors.UndefinedColumn("column x does not exist"))
    outage = main.db_failure("create", psycopg.OperationalError("connection refused"))
    assert bug.status_code == 500
    assert outage.status_code == 503


def test_metrics_use_route_templates(client):
    client.get("/customers/1")
    body = client.get("/metrics").text
    assert 'route="/customers/{customer_id}"' in body
    assert 'route="/customers/1"' not in body
