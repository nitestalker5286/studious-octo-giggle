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


def test_health_and_empty_viewer_search_page(client):
    assert client.get("/health").get_json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200
    assert b"Enter a component name" in response.data
    assert b"Matching results" not in response.data
    assert b"Recovered inventory" not in response.data
    assert b"Source ships" not in response.data
    assert b"Add component" not in response.data
    assert b"Component Name" in response.data
    assert b">Category<" in response.data
    assert b">Size<" in response.data
    assert b">Grade<" in response.data
    assert b">Spec<" in response.data
    assert b">Ship<" not in response.data
    assert b"Quick category buttons" in response.data
    assert b"data-live-component-search" in response.data
    assert b"app.js" in response.data


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
    assert b"Recovered inventory" in response.data
    assert b"Source ships" in response.data

    # Component Name supports partial matching; Size has its own exact filter.
    assert b"FR-86" in client.get("/?q=FR").data
    assert b"FR-86" in client.get("/?size=3").data
    assert b"No matching components" in client.get("/?size=2").data

    response = client.get("/?category=Radar")
    assert b"No matching components" in response.data

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
        assert radar["grade"] is None
        assert radar["spec"] is None

    snapshot = tmp_path / "inventory.csv"
    assert snapshot.exists()
    assert "Surveyor Array" in snapshot.read_text(encoding="utf-8-sig")

    post(client, f"/components/{radar['id']}/delete", {})
    with connect(app.config["DATABASE_PATH"]) as connection:
        assert connection.execute("SELECT 1 FROM components WHERE id = ?", (radar["id"],)).fetchone() is None


def test_public_view_is_search_only_and_returns_matching_results(client):
    seed(client)
    post(client, "/admin/logout", {})

    landing = client.get("/")
    assert b"FR-86" not in landing.data
    assert b"Recovered inventory" not in landing.data
    assert b"Source ships" not in landing.data
    assert b"Matching results" not in landing.data

    assert b"No matching components" in client.get("/?q=Hammerhead").data

    results = client.get("/?size=3")
    assert b"Matching results" in results.data
    assert b"FR-86" in results.data
    assert b"Hammerhead" in results.data
    assert b"Recovered inventory" not in results.data
    assert b"Source ships" not in results.data
    assert b"Edit" not in results.data
    assert b"Delete" not in results.data
    export = client.get("/export.csv")
    assert export.status_code == 302
    assert "/admin/login" in export.headers["Location"]


def test_public_ship_directory_is_alphabetical_and_ship_page_lists_its_components(client, app):
    seed(client)
    with connect(app.config["DATABASE_PATH"]) as connection:
        arrow_id = connection.execute(
            "INSERT INTO ships(name, manufacturer, source) VALUES (?, ?, ?)",
            ("Arrow", "Anvil", "Bounties"),
        ).lastrowid
        cutlass_id = connection.execute(
            "INSERT INTO ships(name, manufacturer, source) VALUES (?, ?, ?)",
            ("Cutlass Black", "Drake", "Salvage contracts"),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO components(ship_id, name, category, size, grade, spec, quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cutlass_id, "Crossfield", "Quantum Drive", "2", "B", "CIV", 1),
        )
        connection.commit()
    post(client, "/admin/logout", {})

    directory = client.get("/ships")
    assert directory.status_code == 200
    assert directory.data.index(b"Arrow") < directory.data.index(b"Cutlass Black") < directory.data.index(b"Hammerhead")
    assert f'/ships/{cutlass_id}'.encode() in directory.data
    assert b"Search ships" in directory.data
    assert b"Browse ships by first letter" in directory.data

    searched = client.get("/ships?q=drake")
    assert b"Cutlass Black" in searched.data
    assert b"Hammerhead" not in searched.data
    letter = client.get("/ships?letter=H")
    assert b"Hammerhead" in letter.data
    assert b"Cutlass Black" not in letter.data

    cutlass = client.get(f"/ships/{cutlass_id}")
    assert cutlass.status_code == 200
    assert b"Cutlass Black" in cutlass.data
    assert b"Crossfield" in cutlass.data
    assert b"FR-86" not in cutlass.data
    assert b"Quantum Drive" in cutlass.data
    assert b"Edit" not in cutlass.data

    arrow = client.get(f"/ships/{arrow_id}")
    assert b"No components associated" in arrow.data
    assert client.get("/ships/999999").status_code == 404


def test_csv_export_has_ship_source_and_current_filters(client):
    seed(client)
    response = client.get("/export.csv?size=3")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert rows[0] == ["Ship", "Manufacturer", "Ship Source", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
    assert rows[1] == ["Hammerhead", "Aegis", "ERT contracts", "FR-86", "Shield Generator", "3", "A", "MIL", "2"]


def test_save_and_add_another_preserves_defaults_and_stores_optional_nulls(client, app):
    login(client)
    post(client, "/ships/new", {"name": "Reclaimer", "manufacturer": "Aegis", "source": ""})
    response = post(
        client,
        "/components/new",
        {
            "ship_id": "1",
            "name": "Avalanche",
            "category": "Cooler",
            "size": "3",
            "grade": "",
            "spec": "",
            "quantity": "1",
            "save_action": "add_another",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/components/new" in response.headers["Location"]
    assert "ship_id=1" in response.headers["Location"]
    assert "category=Cooler" in response.headers["Location"]
    with connect(app.config["DATABASE_PATH"]) as connection:
        row = connection.execute("SELECT grade, spec FROM components WHERE name = 'Avalanche'").fetchone()
        assert row["grade"] is None
        assert row["spec"] is None


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


def test_existing_not_null_grade_spec_schema_is_migrated_without_data_loss(tmp_path):
    database_path = tmp_path / "old_web.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE ships (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            manufacturer TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO ships(id, name) VALUES (1, 'Hammerhead');
        CREATE TABLE components (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ship_id INTEGER NOT NULL,
            name TEXT NOT NULL, category TEXT NOT NULL, size TEXT NOT NULL DEFAULT '',
            grade TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO components(id, ship_id, name, category, size, grade, spec, quantity)
        VALUES (9, 1, 'FR-86', 'Shield Generator', '3', '', '', 2);
        """
    )
    connection.commit()
    connection.close()

    initialize_database(str(database_path))
    with connect(str(database_path)) as migrated:
        row = migrated.execute("SELECT * FROM components WHERE id = 9").fetchone()
        info = {r["name"]: r for r in migrated.execute("PRAGMA table_info(components)")}
        assert row["name"] == "FR-86"
        assert row["quantity"] == 2
        assert row["grade"] is None
        assert row["spec"] is None
        assert info["grade"]["notnull"] == 0
        assert info["spec"]["notnull"] == 0
