from __future__ import annotations

import re
from types import TracebackType
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Literal, Optional, Sequence, Type, Union, cast

import duckdb
import pyarrow
import pyarrow.lib
import pyarrow.types
import snowflake.connector.errors
import sqlglot
from duckdb import DuckDBPyConnection
from snowflake.connector.cursor import DictCursor, ResultMetadata, SnowflakeCursor
from snowflake.connector.result_batch import ResultBatch
from sqlglot import exp, parse_one
from typing_extensions import Self

import fakesnow.transforms as transforms

if TYPE_CHECKING:
    import pandas as pd


class FakeSnowflakeCursor:
    def __init__(
        self,
        duck_conn: DuckDBPyConnection,
        use_dict_result: bool = False,
    ) -> None:
        """Create a fake snowflake cursor backed by DuckDB.

        Args:
            duck_conn (DuckDBPyConnection): DuckDB connection.
            use_dict_result (bool, optional): If true rows are returned as dicts otherwise they
                are returned as tuples. Defaults to False.
        """
        self._duck_conn = duck_conn
        self._use_dict_result = use_dict_result

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc_value: Optional[BaseException] = ...,
        traceback: Optional[TracebackType] = ...,
    ) -> bool:
        return False

    def describe(self, command: str, *args: Any, **kwargs: Any) -> list[ResultMetadata]:
        """Return the schema of the result without executing the query.

        Takes the same arguments as execute

        Returns:
            list[ResultMetadata]: _description_
        """

        # fmt: off
        def as_result_metadata(column_name: str, column_type: str, _: str) -> ResultMetadata:
            # see https://docs.snowflake.com/en/user-guide/python-connector-api.html#type-codes
            # and https://arrow.apache.org/docs/python/api/datatypes.html#type-checking
            # type ignore because of https://github.com/snowflakedb/snowflake-connector-python/issues/1423
            if column_type == "INTEGER":
                return ResultMetadata(
                    name=column_name, type_code=0, display_size=None, internal_size=None, precision=38, scale=0, is_nullable=True                    # type: ignore # noqa: E501
                )
            elif column_type.startswith("DECIMAL"):
                match = re.search(r'\((\d+),(\d+)\)', column_type)
                if match:
                    precision = int(match[1])
                    scale = int(match[2])
                else:
                    precision = scale = None
                return ResultMetadata(
                    name=column_name, type_code=0, display_size=None, internal_size=None, precision=precision, scale=scale, is_nullable=True # type: ignore # noqa: E501
                )
            elif column_type == "VARCHAR":
                return ResultMetadata(
                    name=column_name, type_code=2, display_size=None, internal_size=16777216, precision=None, scale=None, is_nullable=True   # type: ignore # noqa: E501
                )
            elif column_type == "FLOAT":
                return ResultMetadata(
                    name=column_name, type_code=1, display_size=None, internal_size=None, precision=None, scale=None, is_nullable=True       # type: ignore # noqa: E501
                )
            elif column_type == "TIMESTAMP":
                return ResultMetadata(
                    name=column_name, type_code=8, display_size=None, internal_size=None, precision=0, scale=9, is_nullable=True             # type: ignore # noqa: E501
                )
            else:
                # TODO handle more types
                raise NotImplementedError(f"for column type {column_type}")

        # fmt: on

        describe = transforms.as_describe(parse_one(command, read="snowflake"))
        self.execute(describe, *args, **kwargs)

        meta = [
            as_result_metadata(column_name, column_type, null)
            for (column_name, column_type, null, _, _, _) in self._duck_conn.fetchall()
        ]

        return meta

    def execute(
        self,
        command: str | exp.Expression,
        params: Sequence[Any] | dict[Any, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> FakeSnowflakeCursor:

        expression = command if isinstance(command, exp.Expression) else parse_one(command, read="snowflake")

        for t in [transforms.database_as_schema, transforms.set_schema]:
            expression = t(expression)

        transformed = expression.sql()

        try:
            self._duck_conn.execute(transformed)
        except duckdb.CatalogException as e:
            raise snowflake.connector.errors.ProgrammingError(e.args[0]) from e

        self._arrow_table = None
        return self

    def fetchall(self) -> list[tuple] | list[dict]:
        if self._use_dict_result:
            return self._duck_conn.fetch_arrow_table().to_pylist()
        else:
            return self._duck_conn.fetchall()

    def fetchone(self) -> dict | tuple | None:
        if not self._use_dict_result:
            return cast(Union[tuple, None], self._duck_conn.fetchone())

        if not self._arrow_table:
            self._arrow_table = self._duck_conn.fetch_arrow_table()
            self._arrow_table_fetch_one_index = -1

        self._arrow_table_fetch_one_index += 1

        try:
            return self._arrow_table.take([self._arrow_table_fetch_one_index]).to_pylist()
        except pyarrow.lib.ArrowIndexError:
            return None

    def get_result_batches(self) -> list[ResultBatch] | None:
        # chunk_size is multiple of 1024
        # see https://github.com/duckdb/duckdb/issues/4755
        reader = self._duck_conn.fetch_record_batch(chunk_size=1024)

        batches = []
        while True:
            try:
                batches.append(DuckResultBatch(self._use_dict_result, reader.read_next_batch()))
            except StopIteration:
                break

        return batches


class FakeSnowflakeConnection:
    def __init__(
        self,
        duck_conn: DuckDBPyConnection,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ):
        # TODO handle if database only supplied
        if schema:
            self._schema = f"{database}_{schema}" if database else schema
            duck_conn.execute(f"set schema = '{self._schema}'")

        self._duck_conn = duck_conn

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc_value: Optional[BaseException] = ...,
        traceback: Optional[TracebackType] = ...,
    ) -> bool:
        return False

    def cursor(self, cursor_class: Type[SnowflakeCursor] = SnowflakeCursor) -> FakeSnowflakeCursor:
        return FakeSnowflakeCursor(duck_conn=self._duck_conn, use_dict_result=cursor_class == DictCursor)

    def execute_string(
        self,
        sql_text: str,
        remove_comments: bool = False,
        return_cursors: bool = True,
        cursor_class: Type[SnowflakeCursor] = SnowflakeCursor,
        **kwargs: dict[str, Any],
    ) -> Iterable[FakeSnowflakeCursor]:
        cursors = [self.cursor(cursor_class).execute(e.sql()) for e in sqlglot.parse(sql_text, read="snowflake") if e]
        return cursors if return_cursors else []

    def insert_df(
        self, df: pd.DataFrame, table_name: str, database: str | None = None, schema: str | None = None
    ) -> int:
        self._duck_conn.execute(f"INSERT INTO {table_name} SELECT * FROM df")
        return self._duck_conn.fetchall()[0][0]


class DuckResultBatch(ResultBatch):
    def __init__(self, use_dict_result: bool, batch: pyarrow.RecordBatch):
        self._use_dict_result = use_dict_result
        self._batch = batch

    def create_iter(
        self, **kwargs: dict[str, Any]
    ) -> (Iterator[dict | Exception] | Iterator[tuple | Exception] | Iterator[pyarrow.Table] | Iterator[pd.DataFrame]):
        if self._use_dict_result:
            return iter(self._batch.to_pylist())

        return iter(tuple(d.values()) for d in self._batch.to_pylist())

    @property
    def rowcount(self) -> int:
        return self._batch.num_rows

    def to_pandas(self) -> pd.DataFrame:
        raise NotImplementedError()

    def to_arrow(self) -> pyarrow.Table:
        raise NotImplementedError()


def write_pandas(
    conn: FakeSnowflakeConnection,
    df: pd.DataFrame,
    table_name: str,
    database: str | None = None,
    schema: str | None = None,
    chunk_size: int | None = None,
    compression: str = "gzip",
    on_error: str = "abort_statement",
    parallel: int = 4,
    quote_identifiers: bool = True,
    auto_create_table: bool = False,
    create_temp_table: bool = False,
    overwrite: bool = False,
    table_type: Literal["", "temp", "temporary", "transient"] = "",
    **kwargs: Any,
) -> tuple[
    bool,
    int,
    int,
    Sequence[
        tuple[
            str,
            str,
            int,
            int,
            int,
            int,
            str | None,
            int | None,
            int | None,
            str | None,
        ]
    ],
]:
    count = conn.insert_df(df, table_name, database, schema)

    # mocks https://docs.snowflake.com/en/sql-reference/sql/copy-into-table.html#output
    mock_copy_results = [("fakesnow/file0.txt", "LOADED", count, count, 1, 0, None, None, None, None)]

    # return success
    return (True, len(mock_copy_results), count, mock_copy_results)
