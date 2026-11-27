import csv
import io
import sqlite3
from pathlib import Path

import pytest

from app import create_app
from database import connect, initialize_database


@pytest.fixture()
def app(tmp_path: Path):
    database_path = tmp_path / "tracker.db"
    export_path = tmp_path / "inventory.csv"
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "ADMIN_PASSWORD": "correct horse battery staple",
            "DATABASE_PATH": str(database_path),
            "AUTO_EXPORT_PATH": str(export_path),
            "SESSION_COOKIE_SECURE": False,
        }
    )
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client) -> str:
    with client.session_transaction() as session:
        return session["_csrf_token"]


def login(client) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={"password": "correct horse battery staple", "csrf_token": csrf(client)},
        follow_redirects=True,
    )
    assert b"Signed in as administrator" in response.data


def post(client, url: str, data: dict, follow_redirects: bool = True):
    data = {**data, "csrf_token": csrf(client)}
    return client.post(url, data=data, follow_redirects=follow_redirects)


def seed(client) -> None:
    login(client)
    response = post(
        client,
        "/ships/new",
        {"name": "Hammerhead", "manufacturer": "Aegis", "source": "ERT contracts"},
    )
    assert b"Hammerhead" in response.data
    response = post(
        client,
        "/components/new",
        {
            "ship_id": "1",
            "name": "FR-86",
            "category": "Shield Generator",
            "size": "3",
            "grade": "A",
            "spec": "MIL",
            "quantity": "2",
        },
    )
    assert b"FR-86" in response.data


def test_health_and_empty_viewer_inventory(client):
    assert client.get("/health").get_json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200
    assert b"No components found" in response.data
    assert b"Add component" not in response.data


def test_viewer_cannot_modify_and_csrf_is_enforced(client):
    response = client.get("/ships/new")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]
    client.get("/")
    assert client.post("/admin/logout", data={}).status_code == 400


def test_admin_crud_search_filters_and_optional_fields(client, app, tmp_path):
    seed(client)
    response = client.get("/?q=FR-86")
    assert b"FR-86" in response.data and b"Hammerhead" in response.data
    assert b"ERT contracts" in response.data

    response = client.get("/?category=Radar")
    assert b"No components found" in response.data

    response = post(
        client,
        "/components/new",
        {
            "ship_id": "1",
            "name": "Surveyor Array",
            "category": "Radar",
            "size": "",
            "grade": "",
            "spec": "",
            "quantity": "1",
        },
    )
    assert b"Surveyor Array" in response.data

    with connect(app.config["DATABASE_PATH"]) as connection:
        radar = connection.execute("SELECT * FROM components WHERE name = 'Surveyor Array'").fetchone()
        assert radar["grade"] == ""
        assert radar["spec"] == ""

    snapshot = tmp_path / "inventory.csv"
    assert snapshot.exists()
    assert "Surveyor Array" in snapshot.read_text(encoding="utf-8-sig")

    post(client, f"/components/{radar['id']}/delete", {})
    with connect(app.config["DATABASE_PATH"]) as connection:
        assert connection.execute("SELECT 1 FROM components WHERE id = ?", (radar["id"],)).fetchone() is None


def test_csv_export_has_ship_source_and_current_filters(client):
    seed(client)
    response = client.get("/export.csv?q=FR-86")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert rows[0] == ["Ship", "Manufacturer", "Ship Source", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
    assert rows[1] == ["Hammerhead", "Aegis", "ERT contracts", "FR-86", "Shield Generator", "3", "A", "MIL", "2"]


def test_ship_with_components_cannot_be_deleted(client):
    seed(client)
    response = post(client, "/ships/1/delete", {})
    assert b"Delete or reassign" in response.data
    assert b"Hammerhead" in response.data


def test_legacy_schema_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE ships (id INTEGER PRIMARY KEY, ship_name TEXT, manufacturer TEXT, ship_source TEXT);
        INSERT INTO ships VALUES (7, 'Cutlass Black', 'Drake', 'Bounties');
        CREATE TABLE components (
            id INTEGER PRIMARY KEY, ship TEXT, component_name TEXT, component_type TEXT,
            size TEXT, grade TEXT, spec TEXT, qty INTEGER
        );
        INSERT INTO components VALUES (4, 'Cutlass Black', 'Crossfield', 'Quantum Drive', '2', 'B', 'COMP', 1);
        """
    )
    connection.commit()
    connection.close()

    initialize_database(str(database_path))
    with connect(str(database_path)) as migrated:
        ship = migrated.execute("SELECT * FROM ships WHERE id = 7").fetchone()
        component = migrated.execute("SELECT * FROM components WHERE id = 4").fetchone()
        assert ship["name"] == "Cutlass Black"
        assert ship["source"] == "Bounties"
        assert component["ship_id"] == 7
        assert component["name"] == "Crossfield"
        assert component["category"] == "Quantum Drive"
        assert component["quantity"] == 1
        assert component["spec"] == "CMP"
