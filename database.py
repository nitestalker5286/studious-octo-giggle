from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def connect(database_path: str) -> sqlite3.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction(database_path: str) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _add_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(connection, table):
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def _make_optional_component_fields_nullable(connection: sqlite3.Connection) -> None:
    """Rebuild older web schemas whose Grade/Spec columns were incorrectly NOT NULL."""
    info = {row["name"]: row for row in connection.execute('PRAGMA table_info("components")')}
    if not any(info.get(name) and info[name]["notnull"] for name in ("grade", "spec")):
        return

    connection.execute(
        """
        CREATE TABLE components_nullable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            size TEXT NOT NULL DEFAULT '',
            grade TEXT,
            spec TEXT,
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ship_id) REFERENCES ships(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO components_nullable
            (id, ship_id, name, category, size, grade, spec, quantity, created_at, updated_at)
        SELECT id, ship_id, name, category, COALESCE(size, ''),
               NULLIF(TRIM(grade), ''), NULLIF(TRIM(spec), ''),
               CASE WHEN quantity IS NULL OR quantity < 1 THEN 1 ELSE quantity END,
               COALESCE(NULLIF(created_at, ''), CURRENT_TIMESTAMP),
               COALESCE(NULLIF(updated_at, ''), CURRENT_TIMESTAMP)
        FROM components
        """
    )
    connection.execute("DROP TABLE components")
    connection.execute("ALTER TABLE components_nullable RENAME TO components")


def initialize_database(database_path: str) -> None:
    """Create the schema and upgrade common desktop-tracker schemas in place."""
    with transaction(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                manufacturer TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                size TEXT NOT NULL DEFAULT '',
                grade TEXT,
                spec TEXT,
                quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ship_id) REFERENCES ships(id) ON DELETE RESTRICT
            )
            """
        )

        # Add fields introduced over the desktop tracker's lifetime. SQLite preserves data.
        for name, definition in (
            ("name", "TEXT NOT NULL DEFAULT ''"),
            ("manufacturer", "TEXT NOT NULL DEFAULT ''"),
            ("source", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(connection, "ships", name, definition)

        ship_columns = _columns(connection, "ships")
        for legacy in ("ship_name", "ship", "model"):
            if legacy in ship_columns:
                connection.execute(
                    f'UPDATE ships SET name = "{legacy}" WHERE (name IS NULL OR TRIM(name) = \'\') AND "{legacy}" IS NOT NULL'
                )
                break
        if "ship_source" in ship_columns:
            connection.execute(
                "UPDATE ships SET source = ship_source WHERE (source IS NULL OR TRIM(source) = '') AND ship_source IS NOT NULL"
            )

        for name, definition in (
            ("ship_id", "INTEGER"),
            ("name", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT 'Other'"),
            ("size", "TEXT NOT NULL DEFAULT ''"),
            ("grade", "TEXT"),
            ("spec", "TEXT"),
            ("quantity", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column(connection, "components", name, definition)

        component_columns = _columns(connection, "components")
        aliases = {
            "name": ("component_name", "component"),
            "category": ("component_type", "type"),
            "quantity": ("qty",),
        }
        for canonical, legacy_names in aliases.items():
            for legacy in legacy_names:
                if legacy in component_columns:
                    if canonical == "quantity":
                        connection.execute(
                            f'UPDATE components SET quantity = "{legacy}" WHERE "{legacy}" IS NOT NULL AND CAST("{legacy}" AS INTEGER) > 0'
                        )
                    else:
                        empty_condition = f'("{canonical}" IS NULL OR TRIM("{canonical}") = \'\')'
                        if canonical == "category":
                            empty_condition = f'({empty_condition} OR "{canonical}" = \'Other\')'
                        connection.execute(
                            f'UPDATE components SET "{canonical}" = "{legacy}" WHERE {empty_condition} AND "{legacy}" IS NOT NULL'
                        )
                    break

        # If an older schema stored the ship name directly, connect it to a ship row.
        for legacy_ship_column in ("ship", "ship_name"):
            if legacy_ship_column in component_columns:
                connection.execute(
                    f"""
                    UPDATE components
                    SET ship_id = (
                        SELECT ships.id FROM ships
                        WHERE LOWER(ships.name) = LOWER(components."{legacy_ship_column}")
                        LIMIT 1
                    )
                    WHERE ship_id IS NULL AND "{legacy_ship_column}" IS NOT NULL
                    """
                )
                break

        _make_optional_component_fields_nullable(connection)
        connection.execute(
            "UPDATE components SET spec = 'CMP' WHERE UPPER(TRIM(spec)) IN ('COMP', 'CMP')"
        )
        connection.execute("UPDATE components SET grade = NULL WHERE TRIM(COALESCE(grade, '')) = ''")
        connection.execute("UPDATE components SET spec = NULL WHERE TRIM(COALESCE(spec, '')) = ''")
        connection.execute("UPDATE components SET quantity = 1 WHERE quantity IS NULL OR quantity < 1")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_ships_name_nocase ON ships(name COLLATE NOCASE)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_components_ship_id ON components(ship_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_components_name ON components(name COLLATE NOCASE)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_components_category ON components(category COLLATE NOCASE)")


def inventory_rows(database_path: str, filters: dict[str, str] | None = None) -> list[sqlite3.Row]:
    filters = filters or {}
    clauses: list[str] = []
    values: list[Any] = []

    search = filters.get("q", "").strip()
    if search:
        like = f"%{search}%"
        clauses.append("c.name LIKE ? COLLATE NOCASE")
        values.append(like)

    for key, column in (
        ("ship_id", "c.ship_id"),
        ("category", "c.category"),
        ("size", "c.size"),
        ("grade", "c.grade"),
        ("spec", "c.spec"),
    ):
        value = filters.get(key, "").strip()
        if value == "__none__":
            clauses.append(f"({column} IS NULL OR TRIM({column}) = '')")
        elif value:
            clauses.append(f"{column} = ?")
            values.append(value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT c.id, c.ship_id, c.name, c.category, c.size, c.grade, c.spec, c.quantity,
               s.name AS ship_name, s.manufacturer, s.source
        FROM components c
        JOIN ships s ON s.id = c.ship_id
        {where}
        ORDER BY c.name COLLATE NOCASE, s.name COLLATE NOCASE, c.id
    """
    with connect(database_path) as connection:
        return connection.execute(query, values).fetchall()


def write_inventory_csv(database_path: str, output_path: str) -> None:
    """Write the full inventory atomically; useful for optional volume snapshots."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = inventory_rows(database_path)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Ship", "Manufacturer", "Ship Source", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["ship_name"], row["manufacturer"], row["source"], row["name"],
                        row["category"], row["size"], row["grade"], row["spec"], row["quantity"],
                    ]
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
