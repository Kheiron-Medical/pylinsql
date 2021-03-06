import asyncio
import dataclasses
import datetime
import io
import keyword
import sys
from dataclasses import MISSING, Field, dataclass
from typing import Any, Dict, List, Optional, TextIO, Tuple, TypeVar

from . import async_database
from .async_database import ConnectionParameters, DatabaseClient
from .base import DataClass, cast_if_not_none, is_optional_type, unwrap_optional_type
from .schema import ForeignKey, PrimaryKey, Reference

T = TypeVar("T")


def _db_type_to_py_type(
    db_type: str, is_nullable: bool, has_default: bool
) -> Tuple[type, type]:
    "Maps a PostgreSQL type to a Python type."

    if db_type in ["character varying", "text"]:
        py_type = str
    elif db_type in ["boolean"]:
        py_type = bool
    elif db_type in ["smallint", "integer", "bigint"]:
        py_type = int
    elif db_type in ["real", "double precision"]:
        py_type = float
    elif db_type in ["date"]:
        py_type = datetime.date
    elif db_type in ["time", "time with time zone", "time without time zone"]:
        py_type = datetime.time
    elif db_type in [
        "timestamp",
        "timestamp with time zone",
        "timestamp without time zone",
    ]:
        py_type = datetime.datetime
    else:
        raise RuntimeError(f"unrecognized database type: {db_type}")

    if is_nullable and not has_default:
        outer_type = Optional[py_type]
        value_type = py_type
    else:
        outer_type = py_type
        value_type = py_type

    return outer_type, value_type


@dataclass
class ColumnSchema:
    "Metadata associated with a database table column."

    name: str
    data_type: type
    default: Optional[Any]
    description: str
    references: Optional[ForeignKey] = None


@dataclass
class TableSchema:
    "Metadata associated with a database table."

    name: str
    description: str
    columns: Dict[str, ColumnSchema]
    primary_key: Optional[PrimaryKey] = None


@dataclass
class CatalogSchema:
    "Metadata associated with a database (a.k.a. catalog)."

    name: str
    tables: Dict[str, TableSchema]

    def __bool__(self) -> bool:
        return bool(self.tables)


@dataclass
class _UniqueConstraint:
    key_name: str
    key_schema: str
    key_table: str
    key_column: str


@dataclass
class _ReferenceConstraint:
    foreign_key_name: str
    foreign_key_schema: str
    foreign_key_table: str
    foreign_key_column: str
    primary_key_schema: str
    primary_key_table: str
    primary_key_column: str


class _CatalogSchemaBuilder:
    conn: DatabaseClient
    db_schema: str

    def __init__(self, conn: DatabaseClient, db_schema: str):
        self.conn = conn
        self.db_schema = db_schema

    async def get_catalog_schema(self) -> CatalogSchema:
        "Retrieves metadata for the current catalog."

        # query table names in dependency order
        query = """
            WITH RECURSIVE dependencies(
                    depth,
                    parent_catalog,
                    parent_schema,
                    parent_name,
                    child_catalog,
                    child_schema,
                    child_name
            ) AS (
                -- tables that have no foreign keys
                SELECT
                    1 AS depth,
                    pkey.table_catalog,
                    pkey.table_schema,
                    pkey.table_name,
                    tab.table_catalog,
                    tab.table_schema,
                    tab.table_name
                FROM
                    information_schema.referential_constraints AS ref_con
                        INNER JOIN information_schema.key_column_usage AS pkey ON
                            ref_con.unique_constraint_catalog = pkey.constraint_catalog AND
                            ref_con.unique_constraint_schema = pkey.constraint_schema AND
                            ref_con.unique_constraint_name = pkey.constraint_name
                        INNER JOIN information_schema.key_column_usage AS fkey ON
                            ref_con.constraint_catalog = fkey.constraint_catalog AND
                            ref_con.constraint_schema = fkey.constraint_schema AND
                            ref_con.constraint_name = fkey.constraint_name
                        RIGHT JOIN information_schema.tables AS tab ON
                            tab.table_catalog = fkey.table_catalog AND
                            tab.table_schema = fkey.table_schema AND
                            tab.table_name = fkey.table_name
                WHERE
                    tab.table_catalog = CURRENT_CATALOG AND
                    tab.table_schema = $1 AND
                    pkey.table_catalog IS NULL AND
                    pkey.table_schema IS NULL AND
                    pkey.table_name IS NULL
            UNION ALL
                -- tables that only depend on tables returned by the previous recursion steps
                SELECT
                    dep.depth + 1,
                    dep.child_catalog,
                    dep.child_schema,
                    dep.child_name,
                    tab.table_catalog,
                    tab.table_schema,
                    tab.table_name
                FROM
                    information_schema.referential_constraints AS ref_con
                        INNER JOIN information_schema.key_column_usage AS pkey ON
                            ref_con.unique_constraint_catalog = pkey.constraint_catalog AND
                            ref_con.unique_constraint_schema = pkey.constraint_schema AND
                            ref_con.unique_constraint_name = pkey.constraint_name
                        INNER JOIN information_schema.key_column_usage AS fkey ON
                            ref_con.constraint_catalog = fkey.constraint_catalog AND
                            ref_con.constraint_schema = fkey.constraint_schema AND
                            ref_con.constraint_name = fkey.constraint_name
                        INNER JOIN information_schema.tables AS tab ON
                            tab.table_catalog = fkey.table_catalog AND
                            tab.table_schema = fkey.table_schema AND
                            tab.table_name = fkey.table_name
                        INNER JOIN dependencies AS dep ON
                            dep.child_catalog = pkey.table_catalog AND
                            dep.child_schema = pkey.table_schema AND
                            dep.child_name = pkey.table_name
                WHERE
                    tab.table_catalog = CURRENT_CATALOG AND
                    tab.table_schema = $1
            )
            SELECT
                child_name
            FROM
                dependencies
            GROUP BY
                child_name
            ORDER BY
                -- minimum depth reflects the first encounter of a table (tables may depend on several tables)
                MIN(depth), child_name
        """
        tables = await self.conn.typed_fetch_column(str, query, self.db_schema)
        table_schemas = [await self._get_table_schema(table) for table in tables]
        table_schema_map = dict((table.name, table) for table in table_schemas)
        return CatalogSchema(name=self.db_schema, tables=table_schema_map)

    async def _get_table_schema(self, db_table: str) -> TableSchema:
        "Retrieves metadata for a table in the current catalog."

        query = """
            SELECT
                dsc.description
            FROM
                pg_catalog.pg_class cls
                    INNER JOIN pg_catalog.pg_namespace ns ON cls.relnamespace = ns.oid
                    INNER JOIN pg_catalog.pg_description dsc ON cls.oid = dsc.objoid
            WHERE
                ns.nspname = $1 AND cls.relname = $2 AND dsc.objsubid = 0
        """
        description = await self.conn.typed_fetch_value(
            str, query, self.db_schema, db_table
        )

        query = """
            WITH
                column_description AS (
                    SELECT
                        dsc.objsubid,
                        dsc.description
                    FROM
                        pg_catalog.pg_class cls
                            INNER JOIN pg_catalog.pg_namespace ns ON cls.relnamespace = ns.oid
                            INNER JOIN pg_catalog.pg_description dsc ON cls.oid = dsc.objoid
                    WHERE
                        ns.nspname = $1 AND cls.relname = $2
                )
            SELECT
                column_name,
                CASE
                    WHEN is_nullable = 'YES' THEN TRUE
                    WHEN is_nullable = 'NO' THEN FALSE
                    ELSE NULL
                END AS is_nullable,
                data_type,
                column_default,
                character_maximum_length,
                CASE
                    WHEN is_identity = 'YES' THEN TRUE
                    WHEN is_identity = 'NO' THEN FALSE
                    ELSE NULL
                END AS is_identity,
                description
            FROM
                information_schema.columns cols
                    LEFT JOIN column_description ON cols.ordinal_position = objsubid
            WHERE
                table_catalog = CURRENT_CATALOG AND table_schema = $1 AND table_name = $2
            ORDER BY
                ordinal_position
        """
        columns = await self.conn.raw_fetch(query, self.db_schema, db_table)
        column_schemas = {}
        for column in columns:
            outer_type, value_type = _db_type_to_py_type(
                column["data_type"],
                column["is_nullable"],
                column["column_default"] is not None,
            )

            column_schema = ColumnSchema(
                name=column["column_name"],
                data_type=outer_type,
                default=cast_if_not_none(value_type, column["column_default"]),
                description=column["description"],
            )
            column_schemas[column_schema.name] = column_schema

        table_schema = TableSchema(
            name=db_table, description=description, columns=column_schemas
        )
        await self._set_foreign_keys(table_schema)
        await self._set_unique_keys(table_schema)
        return table_schema

    async def _set_unique_keys(self, table_schema: TableSchema) -> None:
        query = """
            SELECT
                ukey.constraint_name AS key_name,
                ukey.table_schema AS key_schema,
                ukey.table_name AS key_table,
                ukey.column_name AS key_column

            FROM
                information_schema.table_constraints tab_con
                    INNER JOIN information_schema.key_column_usage ukey ON
                        tab_con.constraint_catalog = ukey.constraint_catalog AND
                        tab_con.constraint_schema = ukey.constraint_schema AND
                        tab_con.constraint_name = ukey.constraint_name
                        
            WHERE ukey.table_catalog = CURRENT_CATALOG
                AND ukey.table_schema = $1
                AND ukey.table_name = $2
                AND tab_con.constraint_type = 'PRIMARY KEY'
        """
        constraints = await self.conn.typed_fetch(
            _UniqueConstraint, query, self.db_schema, table_schema.name
        )
        if len(constraints) > 1:
            table_schema.primary_key = PrimaryKey(
                constraints[0].key_name,
                [constraint.key_column for constraint in constraints],
            )
        elif len(constraints) > 0:
            table_schema.primary_key = PrimaryKey(
                constraints[0].key_name, constraints[0].key_column
            )
        else:
            table_schema.primary_key = None

    async def _set_foreign_keys(self, table_schema: TableSchema) -> None:
        "Binds table relations associating foreign keys with primary keys."

        query = """
            SELECT
                fkey.constraint_name AS foreign_key_name,
                fkey.table_schema AS foreign_key_schema,
                fkey.table_name AS foreign_key_table,
                fkey.column_name AS foreign_key_column,
                pkey.constraint_name AS primary_key_name,
                pkey.table_schema AS primary_key_schema,
                pkey.table_name AS primary_key_table,
                pkey.column_name AS primary_key_column
            FROM
                information_schema.referential_constraints ref_con
                    INNER JOIN information_schema.key_column_usage pkey ON
                        ref_con.unique_constraint_catalog = pkey.constraint_catalog AND
                        ref_con.unique_constraint_schema = pkey.constraint_schema AND
                        ref_con.unique_constraint_name = pkey.constraint_name
                    INNER JOIN information_schema.key_column_usage fkey ON
                        ref_con.constraint_catalog = fkey.constraint_catalog AND
                        ref_con.constraint_schema = fkey.constraint_schema AND
                        ref_con.constraint_name = fkey.constraint_name
            WHERE
                fkey.table_catalog = CURRENT_CATALOG
                    AND fkey.table_schema = $1
                    AND fkey.table_name = $2
        """
        constraints = await self.conn.typed_fetch(
            _ReferenceConstraint, query, self.db_schema, table_schema.name
        )
        for constraint in constraints:
            if constraint.foreign_key_schema != constraint.primary_key_schema:
                raise RuntimeError(
                    f"foreign key table schema {constraint.foreign_key_schema} and primary key table schema {constraint.primary_key_schema} are not the same"
                )

            column = table_schema.columns[constraint.foreign_key_column]
            if column.references is not None:
                raise RuntimeError(
                    f"column {column.name} already has a foreign key constraint"
                )

            column.references = ForeignKey(
                name=constraint.foreign_key_name,
                references=Reference(
                    table=constraint.primary_key_table,
                    column=constraint.primary_key_column,
                ),
            )


async def get_catalog_schema(conn: DatabaseClient, db_schema: str) -> CatalogSchema:
    builder = _CatalogSchemaBuilder(conn, db_schema)
    return await builder.get_catalog_schema()


def column_to_field(
    column: ColumnSchema,
) -> Tuple[str, type, Field]:
    if keyword.iskeyword(column.name):
        field_name = f"{column.name}_"  # PEP 8: single trailing underscore to avoid conflicts with Python keyword
    else:
        field_name = column.name

    metadata: Dict[str, Any] = {}
    if column.description is not None:
        metadata["description"] = column.description
    if column.references is not None:
        metadata["foreign_key"] = column.references

    default = MISSING
    if column.default is not None:
        default = column.default
    elif is_optional_type(column.data_type):
        default = None

    return (
        field_name,
        column.data_type,
        dataclasses.field(default=default, metadata=metadata if metadata else None),
    )


def table_to_dataclass(table: TableSchema) -> DataClass:
    "Generates a dataclass type corresponding to a table schema."

    fields = [column_to_field(column) for column in table.columns.values()]
    if keyword.iskeyword(table.name):
        class_name = f"{table.name}_"  # PEP 8: single trailing underscore to avoid conflicts with Python keyword
    else:
        class_name = table.name

    # default arguments must follow non-default arguments
    fields.sort(key=lambda f: f[2].default is not MISSING)

    typ = dataclasses.make_dataclass(class_name, fields)
    typ.__doc__ = table.description
    if table.primary_key is not None:
        typ.primary_key = table.primary_key
    return typ


def catalog_to_dataclasses(catalog: CatalogSchema) -> List[DataClass]:
    "Generates a list of dataclass types corresponding to a catalog schema."

    return [table_to_dataclass(table) for table in catalog.tables.values()]


def dataclasses_to_stream(types: List[DataClass], target: TextIO):
    "Generates Python code corresponding to a dataclass type."

    print("# This source file has been generated by a tool, do not edit", file=target)
    print("from dataclasses import dataclass, field", file=target)
    print("from datetime import date, datetime, time", file=target)
    print("from typing import Optional", file=target)
    print("from pylinsql.schema import *", file=target)
    print(file=target)

    for typ in types:
        print(file=target)
        print("@dataclass", file=target)
        print(f"class {typ.__name__}:", file=target)
        if typ.__doc__:
            print(f"    {repr(typ.__doc__)}", file=target)
            print(file=target)

        # primary key
        if getattr(typ, "primary_key", None) is not None:
            print(f"    primary_key = {repr(typ.primary_key)}", file=target)
            print(file=target)

        # table columns
        for field in dataclasses.fields(typ):
            if is_optional_type(field.type):
                inner_type = unwrap_optional_type(field.type)
                type_name = f"Optional[{inner_type.__name__}]"
            else:
                type_name = field.type.__name__
            if field.default is not MISSING and field.metadata:
                initializer = f" = field(default = {repr(field.default)}, metadata = {repr(dict(field.metadata))})"
            elif field.metadata:
                initializer = f" = field(metadata = {repr(dict(field.metadata))})"
            elif field.default is not MISSING:
                initializer = f" = {repr(field.default)}"
            else:
                initializer = ""
            print(f"    {field.name}: {type_name}{initializer}", file=target)
        print(file=target)


def dataclasses_to_code(types: List[DataClass]) -> str:
    f = io.StringIO()
    dataclasses_to_stream(types, f)
    return f.getvalue()


async def main(output_path: str, db_schema: str) -> None:
    async with async_database.connection(ConnectionParameters()) as conn:
        catalog = await get_catalog_schema(conn, db_schema)

    if not catalog:
        raise RuntimeError(f'catalog schema "{db_schema}" is empty')

    types = catalog_to_dataclasses(catalog)
    code = dataclasses_to_code(types)
    with open(output_path, "w") as f:
        f.write(code)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Python data classes from a PostgreSQL database schema",
        epilog="""
            Use environment variables PSQL_USERNAME, PSQL_PASSWORD, PSQL_DATABASE, PSQL_HOSTNAME and PSQL_PORT
            to set PostgreSQL connection parameters.
        """,
    )
    parser.add_argument(
        "output", help="Python source file to write generated data classes to"
    )
    parser.add_argument("--schema", default="public", help="database schema to export")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.output, args.schema))
    except Exception as e:
        print(f"error: {e}")
        sys.exit(1)
