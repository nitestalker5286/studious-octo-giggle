from __future__ import annotations

import csv
import base64
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


def _component_write_values(
    connection: sqlite3.Connection,
    ship_id: int,
    name: str,
    category: str,
    size: str,
    grade: str | None,
    spec: str | None,
    quantity: int,
) -> dict[str, Any]:
    """Return values for both the web schema and any retained desktop aliases.

    SQLite cannot remove a legacy NOT NULL constraint with ALTER TABLE.  Railway
    databases upgraded in place can therefore still require component_name and
    component_type even after the current name/category columns are added.  Build
    writes from the columns that actually exist so those databases remain usable
    without rebuilding (and risking) the user's persistent inventory.
    """
    columns = _columns(connection, "components")
    values: dict[str, Any] = {
        "ship_id": ship_id,
        "name": name,
        "category": category,
        "size": size,
        "grade": grade,
        "spec": spec,
        "quantity": quantity,
    }
    for alias in ("component_name", "component"):
        if alias in columns:
            values[alias] = name
    for alias in ("component_type", "type"):
        if alias in columns:
            values[alias] = category
    if "qty" in columns:
        values["qty"] = quantity
    ship_aliases = columns.intersection(("ship", "ship_name"))
    if ship_aliases:
        ship = connection.execute("SELECT name FROM ships WHERE id = ?", (ship_id,)).fetchone()
        ship_name = ship["name"] if ship is not None else ""
        for alias in ship_aliases:
            values[alias] = ship_name
    return {column: value for column, value in values.items() if column in columns}


def insert_component(
    connection: sqlite3.Connection,
    ship_id: int,
    name: str,
    category: str,
    size: str,
    grade: str | None,
    spec: str | None,
    quantity: int,
) -> int:
    """Insert a component while satisfying retained legacy column constraints."""
    values = _component_write_values(
        connection, ship_id, name, category, size, grade, spec, quantity
    )
    columns = list(values)
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO components ({quoted_columns}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )
    return int(cursor.lastrowid)


def update_component(
    connection: sqlite3.Connection,
    component_id: int,
    ship_id: int,
    name: str,
    category: str,
    size: str,
    grade: str | None,
    spec: str | None,
    quantity: int,
) -> None:
    """Update current and retained legacy component columns together."""
    values = _component_write_values(
        connection, ship_id, name, category, size, grade, spec, quantity
    )
    assignments = [f'"{column}" = ?' for column in values]
    if "updated_at" in _columns(connection, "components"):
        assignments.append('"updated_at" = CURRENT_TIMESTAMP')
    connection.execute(
        f"UPDATE components SET {', '.join(assignments)} WHERE id = ?",
        [*values.values(), component_id],
    )


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


def _initialize_ship_sources(connection: sqlite3.Connection) -> None:
    """Create the multi-source relationship and migrate the legacy source exactly once."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ship_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL,
            source TEXT NOT NULL CHECK (TRIM(source) <> ''),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ship_id) REFERENCES ships(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("DELETE FROM ship_sources WHERE TRIM(source) = ''")
    connection.execute(
        """
        DELETE FROM ship_sources
        WHERE id NOT IN (
            SELECT MIN(id) FROM ship_sources
            GROUP BY ship_id, LOWER(TRIM(source))
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ship_sources_unique_nocase
        ON ship_sources(ship_id, source COLLATE NOCASE)
        """
    )
    # INSERT OR IGNORE plus the unique index makes every startup/redeploy idempotent.
    connection.execute(
        """
        INSERT OR IGNORE INTO ship_sources (ship_id, source)
        SELECT id, TRIM(source)
        FROM ships
        WHERE source IS NOT NULL AND TRIM(source) <> ''
        """
    )


def ship_sources_map(
    connection: sqlite3.Connection, ship_ids: list[int] | None = None
) -> dict[int, list[str]]:
    values: list[Any] = []
    where = ""
    if ship_ids is not None:
        if not ship_ids:
            return {}
        placeholders = ",".join("?" for _ in ship_ids)
        where = f"WHERE ship_id IN ({placeholders})"
        values.extend(ship_ids)
    rows = connection.execute(
        f"""
        SELECT ship_id, source FROM ship_sources
        {where}
        ORDER BY source COLLATE NOCASE, id
        """,
        values,
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(int(row["ship_id"]), []).append(row["source"])
    # Preserve visibility for legacy/external writers that still populate only ships.source.
    legacy_where = ""
    legacy_values: list[Any] = []
    if ship_ids is not None:
        placeholders = ",".join("?" for _ in ship_ids)
        legacy_where = f"WHERE id IN ({placeholders})"
        legacy_values.extend(ship_ids)
    for row in connection.execute(
        f"SELECT id, source FROM ships {legacy_where}", legacy_values
    ).fetchall():
        ship_id = int(row["id"])
        legacy_source = (row["source"] or "").strip()
        if legacy_source and not result.get(ship_id):
            result[ship_id] = [legacy_source]
    return result


def replace_ship_sources(
    connection: sqlite3.Connection, ship_id: int, sources: list[str]
) -> list[str]:
    """Replace a ship's source relationships with a normalized, case-insensitive set."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in sources:
        source = str(raw).strip()
        key = source.casefold()
        if not source or key in seen:
            continue
        seen.add(key)
        normalized.append(source)
    connection.execute("DELETE FROM ship_sources WHERE ship_id = ?", (ship_id,))
    connection.executemany(
        "INSERT INTO ship_sources (ship_id, source) VALUES (?, ?)",
        [(ship_id, source) for source in normalized],
    )
    # Keep the old column synchronized for legacy desktop/database consumers.
    connection.execute(
        "UPDATE ships SET source = ? WHERE id = ?",
        (normalized[0] if normalized else "", ship_id),
    )
    return normalized


def enrich_ship_rows(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """Attach every related source to ship query rows without multiplying aggregates."""
    sources = ship_sources_map(connection, [int(row["id"]) for row in rows])
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["sources"] = sources.get(int(row["id"]), [])
        item["source"] = item["sources"][0] if item["sources"] else ""
        result.append(item)
    return result


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
        _initialize_ship_sources(connection)

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

        # Keep retained desktop columns usable for external/older consumers too.
        # These statements are safe to repeat on every Railway restart.
        for legacy in ("component_name", "component"):
            if legacy in component_columns:
                connection.execute(
                    f'''UPDATE components SET "{legacy}" = name
                        WHERE ("{legacy}" IS NULL OR TRIM("{legacy}") = '')
                          AND name IS NOT NULL'''
                )
        for legacy in ("component_type", "type"):
            if legacy in component_columns:
                connection.execute(
                    f'''UPDATE components SET "{legacy}" = category
                        WHERE ("{legacy}" IS NULL OR TRIM("{legacy}") = '')
                          AND category IS NOT NULL'''
                )
        if "qty" in component_columns:
            connection.execute(
                "UPDATE components SET qty = quantity "
                "WHERE (qty IS NULL OR CAST(qty AS INTEGER) < 1) AND quantity IS NOT NULL"
            )
        for legacy in ("ship", "ship_name"):
            if legacy in component_columns:
                connection.execute(
                    f'''UPDATE components
                        SET "{legacy}" = (SELECT name FROM ships WHERE ships.id = components.ship_id)
                        WHERE ("{legacy}" IS NULL OR TRIM("{legacy}") = '')
                          AND ship_id IS NOT NULL'''
                )

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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_ship_sources_ship_id ON ship_sources(ship_id)")


def inventory_rows(database_path: str, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
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
               s.name AS ship_name, s.manufacturer
        FROM components c
        JOIN ships s ON s.id = c.ship_id
        {where}
        ORDER BY c.name COLLATE NOCASE, s.name COLLATE NOCASE, c.id
    """
    with connect(database_path) as connection:
        rows = connection.execute(query, values).fetchall()
        sources = ship_sources_map(connection, sorted({int(row["ship_id"]) for row in rows}))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["sources"] = sources.get(int(row["ship_id"]), [])
            item["source"] = " | ".join(item["sources"])
            result.append(item)
        return result


def _group_value(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def component_group_id(name: Any, category: Any, size: Any, grade: Any, spec: Any) -> str:
    """Return a deterministic browser-safe ID for one component configuration."""
    canonical = "\x1f".join(
        _group_value(value) for value in (name, category, size, grade, spec)
    )
    encoded = base64.urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")
    return f"group-{encoded}"


def group_inventory_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group duplicate public component rows and aggregate their per-ship sources."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = component_group_id(
            row["name"], row["category"], row["size"], row["grade"], row["spec"]
        )
        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "name": row["name"],
                "category": row["category"],
                "size": row["size"] or "",
                "grade": row["grade"],
                "spec": row["spec"],
                "quantity": 0,
                "record_ids": [],
                "_sources": {},
            },
        )
        quantity = int(row["quantity"] or 0)
        group["quantity"] += quantity
        group["record_ids"].append(int(row["id"]))
        source_key = int(row["ship_id"])
        source = group["_sources"].setdefault(
            source_key,
            {
                "ship_id": int(row["ship_id"]),
                "ship_name": row["ship_name"],
                "manufacturer": row["manufacturer"] or "",
                "sources": list(row.get("sources") or []),
                "source": " | ".join(row.get("sources") or []),
                "quantity": 0,
            },
        )
        source["quantity"] += quantity

    result: list[dict[str, Any]] = []
    for group in groups.values():
        group["sources"] = sorted(
            group.pop("_sources").values(),
            key=lambda source: (
                _group_value(source["ship_name"]),
                source["ship_id"],
            ),
        )
        group["ship_count"] = len(group["sources"])
        group["source_count"] = sum(len(source["sources"]) for source in group["sources"])
        result.append(group)
    return sorted(
        result,
        key=lambda group: (
            _group_value(group["name"]),
            _group_value(group["category"]),
            _group_value(group["size"]),
            _group_value(group["grade"]),
            _group_value(group["spec"]),
        ),
    )


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
                ["Ship", "Manufacturer", "Ship Sources", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
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
