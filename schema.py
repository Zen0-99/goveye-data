"""Schema parser for Room exported schema JSON.

Parses the Room exported schema JSON (8.json) and exposes functions to
extract table creation SQL, FTS4 content sync triggers, room_master_table
setup queries, and the identity hash / version.

Per D-01: Python stdlib json only — no external dependencies.
"""

import json


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
