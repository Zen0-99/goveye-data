"""Schema parser for Room exported schema JSON.

Parses the Room exported schema JSON (8.json) and exposes functions to
extract table creation SQL, FTS4 content sync triggers, room_master_table
setup queries, and the identity hash / version.

Per D-01: Python stdlib json only — no external dependencies.
"""

import json
import os
import sqlite3


def load_schema(schema_path):
    """Load and return the Room exported schema JSON.

    Args:
        schema_path: Path to the schema JSON file (e.g. schemas/8.json).

    Returns:
        The parsed schema dict.
    """
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_identity_hash(schema):
    """Return the Room identity hash from the schema.

    This hash (187aeb854a2e69de65200c666d6555d1) is what Room checks
    in room_master_table on database open.
    """
    return schema["database"]["identityHash"]


def get_version(schema):
    """Return the Room database version (8)."""
    return schema["database"]["version"]


def get_entities(schema):
    """Return the list of entity definitions from the schema."""
    return schema["database"]["entities"]


def get_table_names(schema):
    """Return a set of all table names defined in the schema."""
    return {e["tableName"] for e in get_entities(schema)}


def get_create_sql(schema):
    """Return a list of (table_name, create_sql) tuples for all entities.

    The ${TABLE_NAME} placeholder in createSql is replaced with the
    actual tableName from the entity definition.
    """
    result = []
    for entity in get_entities(schema):
        table_name = entity["tableName"]
        create_sql = entity["createSql"].replace("${TABLE_NAME}", table_name)
        result.append((table_name, create_sql))
    return result


def get_fts_triggers(schema):
    """Return a list of FTS4 content sync trigger SQL statements.

    These triggers must be created BEFORE inserting data into the parent
    table so that the FTS index populates automatically (Pitfall 2).
    """
    triggers = []
    for entity in get_entities(schema):
        for trigger in entity.get("contentSyncTriggers", []):
            triggers.append(trigger)
    return triggers


def get_setup_queries(schema):
    """Return the room_master_table setup queries from database.setupQueries.

    These create the room_master_table and insert the identity hash
    at id=42, which Room validates on database open.
    """
    return schema["database"].get("setupQueries", [])


def get_entity_fields(schema, table_name):
    """Return the list of field definitions for a specific table.

    Args:
        schema: The parsed schema dict.
        table_name: The table name to look up.

    Returns:
        List of field dicts (each with columnName, affinity, notNull, etc.).
    """
    for entity in get_entities(schema):
        if entity["tableName"] == table_name:
            return entity.get("fields", [])
    return []


def get_primary_keys(schema, table_name):
    """Return the list of primary key column names for a table.

    Handles both single-column (PRIMARY KEY(`id`)) and composite
    (PRIMARY KEY(`a`, `b`)) primary keys.
    """
    for entity in get_entities(schema):
        if entity["tableName"] == table_name:
            pk = entity.get("primaryKey", {})
            return pk.get("columnNames", [])
    return []


# --- Per-API table name sets (D-10) ---

# Maps each build script's API name to the tables it owns.
API_TABLE_NAMES = {
    "mps": ["mps", "mps_fts"],
    "votes": ["divisions", "division_votes"],
    "bills": ["bills", "bill_stages"],
    "committees": ["committees", "mp_committee_cross_ref"],
    "recess": ["recess_dates", "recess_dates_meta"],
    "interests": ["interests"],
    "debates": ["debate_speeches"],
    "bio_data": ["bio_data"],
    "expenses": ["expenses"],
    "mp_links": ["mp_links"],
    "party_manifestos": ["party_manifestos", "party_manifestos_fts4"],
    "party_stats": ["party_stats"],
    "historical_members": ["historical_members", "historical_members_fts4"],
    "precompute": ["mp_stats", "peer_averages"],
}


def get_table_names_for_api(api_name):
    """Return the list of table names owned by a given build script.

    Args:
        api_name: One of "mps", "votes", "bills", "committees", "recess".

    Returns:
        List of table names for that build script.

    Raises:
        KeyError: If api_name is not a known build script.
    """
    return list(API_TABLE_NAMES[api_name])


def get_all_table_names(schema_path=None):
    """Return the full list of all 16 table names in the schema.

    If schema_path is provided, loads the schema and returns all table
    names from it (all 16). Otherwise falls back to the union of the
    per-API build-script table sets (10 tables — the 6 user/future tables
    like follows, interests, hansard_contributions are not owned by any
    build script).
    """
    if schema_path is not None:
        schema = load_schema(schema_path)
        return sorted(get_table_names(schema))
    names = []
    for tables in API_TABLE_NAMES.values():
        names.extend(tables)
    return names


def create_database_with_tables(output_path, schema_path, table_names):
    """Create a new SQLite DB with only the specified tables + their FTS
    triggers + room_master_table with the FULL schema's identity hash.

    Per D-10: each per-API build script creates a DB with only its own
    tables, but the room_master_table gets the full schema's identity hash
    so that merge_dbs.py can produce a goveye.db that Room accepts.

    Per Pitfall 2: FTS4 sync triggers are created BEFORE any data insertion
    so the FTS index populates automatically when rows are inserted.

    Args:
        output_path: Path for the new SQLite DB file.
        schema_path: Path to the Room exported schema JSON (8.json).
        table_names: List of table names to create (subset of all 16).

    Returns:
        The sqlite3.Connection (open, ready for inserts).
    """
    schema = load_schema(schema_path)
    identity_hash = get_identity_hash(schema)
    db_version = get_version(schema)

    table_names_set = set(table_names)

    # Remove existing DB file if it exists
    if os.path.exists(output_path):
        os.remove(output_path)

    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    # 1. Create only the specified tables using createSql from schema JSON
    for table_name, create_sql in get_create_sql(schema):
        if table_name in table_names_set:
            cursor.execute(create_sql)

    # 2. Create FTS4 content sync triggers for the specified tables.
    # FTS triggers reference the parent table (e.g. triggers for mps_fts
    # fire on `mps`), so include a trigger if its FTS table name is in the
    # requested set OR if the trigger SQL references a requested table.
    for trigger_sql in get_fts_triggers(schema):
        # The trigger name encodes the FTS table:
        # room_fts_content_sync_<fts_table>_<event>
        # Include if any requested table name appears in the trigger SQL.
        if any(name in trigger_sql for name in table_names_set):
            cursor.execute(trigger_sql)

    # 3. Execute setupQueries to create room_master_table with the FULL
    # schema's identity hash. Even per-API DBs get the full identity hash
    # so merge_dbs.py can produce a goveye.db that Room accepts.
    for setup_query in get_setup_queries(schema):
        cursor.execute(setup_query)

    conn.commit()
    return conn
