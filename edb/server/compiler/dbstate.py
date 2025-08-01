#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from __future__ import annotations
from typing import (
    Any,
    Optional,
    Iterator,
    NamedTuple,
    Self,
    cast,
)

import dataclasses
import enum
import io
import pickle
import time
import uuid

import immutables

from edb import errors

from edb.edgeql import ast as qlast
from edb.edgeql import qltypes

from edb.schema import delta as s_delta
from edb.schema import migrations as s_migrations
from edb.schema import objects as s_obj
from edb.schema import schema as s_schema
from edb.schema import name as s_name

from edb.server import config
from edb.server import defines

from edb.pgsql import codegen as pgcodegen

from . import enums
from . import sertypes


class TxAction(enum.IntEnum):
    START = 1
    COMMIT = 2
    ROLLBACK = 3

    DECLARE_SAVEPOINT = 4
    RELEASE_SAVEPOINT = 5
    ROLLBACK_TO_SAVEPOINT = 6


class MigrationAction(enum.IntEnum):
    START = 1
    POPULATE = 2
    DESCRIBE = 3
    ABORT = 4
    COMMIT = 5
    REJECT_PROPOSED = 6


@dataclasses.dataclass(frozen=True, kw_only=True)
class BaseQuery:
    sql: bytes
    is_transactional: bool = True
    has_dml: bool = False
    cache_sql: Optional[tuple[bytes, bytes]] = dataclasses.field(
        kw_only=True, default=None
    )  # (persist, evict)
    cache_func_call: Optional[tuple[bytes, bytes]] = dataclasses.field(
        kw_only=True, default=None
    )
    warnings: tuple[errors.EdgeDBError, ...] = dataclasses.field(
        kw_only=True, default=()
    )
    unsafe_isolation_dangers: tuple[errors.UnsafeIsolationLevelError, ...] = (
        dataclasses.field(kw_only=True, default=())
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class NullQuery(BaseQuery):
    sql: bytes = b""


@dataclasses.dataclass(frozen=True, kw_only=True)
class ServerParamConversion:
    param_name: str
    conversion_name: str
    additional_info: tuple[str, ...]

    # If the parameter is a query parameter, track its bind_args index.
    script_param_index: Optional[int] = None

    # If the parameter is a constant value, pass to directly to the server.
    constant_value: Optional[Any] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class Query(BaseQuery):
    sql_hash: bytes

    cardinality: enums.Cardinality

    out_type_data: bytes
    out_type_id: bytes
    in_type_data: bytes
    in_type_id: bytes
    in_type_args: Optional[list[Param]] = None

    globals: Optional[list[tuple[str, bool]]] = None
    permissions: Optional[list[str]] = None
    json_permissions: Optional[list[str]] = None
    required_permissions: Optional[list[str]] = None

    server_param_conversions: Optional[list[ServerParamConversion]] = None

    cacheable: bool = True
    is_explain: bool = False
    query_asts: Any = None
    run_and_rollback: bool = False


@dataclasses.dataclass(frozen=True, kw_only=True)
class SimpleQuery(BaseQuery):
    # XXX: Temporary hack, since SimpleQuery will die
    in_type_args: Optional[list[Param]] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class SessionStateQuery(BaseQuery):
    sql: bytes = b""
    config_scope: Optional[qltypes.ConfigScope] = None
    is_backend_setting: bool = False
    requires_restart: bool = False
    is_system_config: bool = False
    config_op: Optional[config.Operation] = None
    is_transactional: bool = True
    globals: Optional[list[tuple[str, bool]]] = None

    in_type_data: Optional[bytes] = None
    in_type_id: Optional[bytes] = None
    in_type_args: Optional[list[Param]] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class DDLQuery(BaseQuery):
    user_schema: Optional[s_schema.FlatSchema]
    feature_used_metrics: Optional[dict[str, float]]
    global_schema: Optional[s_schema.FlatSchema] = None
    cached_reflection: Any = None
    is_transactional: bool = True
    create_db: Optional[str] = None
    drop_db: Optional[str] = None
    drop_db_reset_connections: bool = False
    create_db_template: Optional[str] = None
    create_db_mode: Optional[qlast.BranchType] = None
    db_op_trailer: tuple[bytes, ...] = ()
    ddl_stmt_id: Optional[str] = None
    config_ops: list[config.Operation] = dataclasses.field(default_factory=list)
    early_non_tx_sql: Optional[tuple[bytes, ...]] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class TxControlQuery(BaseQuery):
    action: TxAction
    cacheable: bool

    modaliases: Optional[immutables.Map[Optional[str], str]]

    isolation_level: Optional[qltypes.TransactionIsolationLevel] = None

    user_schema: Optional[s_schema.Schema] = None
    global_schema: Optional[s_schema.Schema] = None
    cached_reflection: Any = None
    feature_used_metrics: Optional[dict[str, float]] = None

    sp_name: Optional[str] = None
    sp_id: Optional[int] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class MigrationControlQuery(BaseQuery):
    action: MigrationAction
    tx_action: Optional[TxAction]
    cacheable: bool

    modaliases: Optional[immutables.Map[Optional[str], str]]

    user_schema: Optional[s_schema.FlatSchema] = None
    cached_reflection: Any = None
    ddl_stmt_id: Optional[str] = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class MaintenanceQuery(BaseQuery):
    pass


@dataclasses.dataclass(frozen=True)
class Param:
    name: str
    required: bool
    array_type_id: Optional[uuid.UUID]
    outer_idx: Optional[int]
    sub_params: Optional[tuple[list[Optional[uuid.UUID]], tuple[Any, ...]]]
    typename: str


#############################


@dataclasses.dataclass(kw_only=True)
class QueryUnit:
    sql: bytes

    introspection_sql: Optional[bytes] = None

    # Status-line for the compiled command; returned to front-end
    # in a CommandComplete protocol message if the command is
    # executed successfully.  When a QueryUnit contains multiple
    # EdgeQL queries, the status reflects the last query in the unit.
    status: bytes

    cache_key: Optional[uuid.UUID] = None
    cache_sql: Optional[tuple[bytes, bytes]] = None  # (persist, evict)
    cache_func_call: Optional[tuple[bytes, bytes]] = None  # (sql, hash)

    # Output format of this query unit
    output_format: enums.OutputFormat = enums.OutputFormat.NONE

    # Set only for units that contain queries that can be cached
    # as prepared statements in Postgres.
    sql_hash: bytes = b""

    # True if all statements in *sql* can be executed inside a transaction.
    # If False, they will be executed separately.
    is_transactional: bool = True

    # SQL to run *before* the main command, non transactionally
    early_non_tx_sql: Optional[tuple[bytes, ...]] = None

    # Capabilities used in this query
    capabilities: enums.Capability = enums.Capability(0)

    # True if this unit contains SET commands.
    has_set: bool = False

    # If tx_id is set, it means that the unit
    # starts a new transaction.
    tx_id: Optional[int] = None

    # If this is the start of the transaction, the isolation level of it.
    tx_isolation_level: Optional[qltypes.TransactionIsolationLevel] = None

    # True if this unit is single 'COMMIT' command.
    # 'COMMIT' is always compiled to a separate QueryUnit.
    tx_commit: bool = False

    # True if this unit is single 'ROLLBACK' command.
    # 'ROLLBACK' is always compiled to a separate QueryUnit.
    tx_rollback: bool = False

    # True if this unit is single 'ROLLBACK TO SAVEPOINT' command.
    # 'ROLLBACK TO SAVEPOINT' is always compiled to a separate QueryUnit.
    tx_savepoint_rollback: bool = False
    tx_savepoint_declare: bool = False

    # True if this unit is `ABORT MIGRATION` command within a transaction,
    # that means abort_migration and tx_rollback cannot be both True
    tx_abort_migration: bool = False

    # For SAVEPOINT commands, the name and sp_id
    sp_name: Optional[str] = None
    sp_id: Optional[int] = None

    # True if it is safe to cache this unit.
    cacheable: bool = False

    # If non-None, contains a name of the DB that is about to be
    # created/deleted. If it's the former, the IO process needs to
    # introspect the new db. If it's the later, the server should
    # close all inactive unused pooled connections to it.
    create_db: Optional[str] = None
    drop_db: Optional[str] = None
    drop_db_reset_connections: bool = False

    # If non-None, contains a name of the DB that will be used as
    # a template database to create the database. The server should
    # close all inactive unused pooled connections to the template db.
    create_db_template: Optional[str] = None
    create_db_mode: Optional[str] = None

    # If a branch command needs extra SQL commands to be performed,
    # those would end up here.
    db_op_trailer: tuple[bytes, ...] = ()

    # If non-None, the DDL statement will emit data packets marked
    # with the indicated ID.
    ddl_stmt_id: Optional[str] = None

    # Cardinality of the result set.  Set to NO_RESULT if the
    # unit represents multiple queries compiled as one script.
    cardinality: enums.Cardinality = enums.Cardinality.NO_RESULT

    out_type_data: bytes = sertypes.NULL_TYPE_DESC
    out_type_id: bytes = sertypes.NULL_TYPE_ID.bytes
    in_type_data: bytes = sertypes.NULL_TYPE_DESC
    in_type_id: bytes = sertypes.NULL_TYPE_ID.bytes
    in_type_args: Optional[list[Param]] = None
    in_type_args_real_count: int = 0
    globals: Optional[list[tuple[str, bool]]] = None
    permissions: Optional[list[str]] = None
    json_permissions: Optional[list[str]] = None
    required_permissions: Optional[list[str]] = None

    server_param_conversions: Optional[list[ServerParamConversion]] = None

    warnings: tuple[errors.EdgeDBError, ...] = ()
    unsafe_isolation_dangers: tuple[errors.UnsafeIsolationLevelError, ...] = ()

    # Set only when this unit contains a CONFIGURE INSTANCE command.
    system_config: bool = False
    # Set only when this unit contains a CONFIGURE DATABASE command.
    database_config: bool = False
    # Set only when this unit contains an operation that needs to have
    # its results read back in the middle of the script.
    # (SET GLOBAL, CONFIGURE DATABASE)
    needs_readback: bool = False
    # Whether any configuration change requires a server restart
    config_requires_restart: bool = False
    # Set only when this unit contains a CONFIGURE command which
    # alters a backend configuration setting.
    backend_config: bool = False
    # Set only when this unit contains a CONFIGURE command which
    # alters a system configuration setting.
    is_system_config: bool = False
    config_ops: list[config.Operation] = dataclasses.field(default_factory=list)
    modaliases: Optional[immutables.Map[Optional[str], str]] = None

    # If present, represents the future schema state after
    # the command is run. The schema is pickled.
    user_schema: Optional[bytes] = None
    # If present, represents updated metrics about feature use induced
    # by the new user_schema.
    feature_used_metrics: Optional[dict[str, float]] = None

    # Unlike user_schema, user_schema_version usually exist, pointing to the
    # latest user schema, which is self.user_schema if changed, or the user
    # schema this QueryUnit was compiled upon.
    user_schema_version: uuid.UUID | None = None
    cached_reflection: Optional[bytes] = None
    extensions: Optional[set[str]] = None
    ext_config_settings: Optional[list[config.Setting]] = None

    # If present, represents the future global schema state
    # after the command is run. The schema is pickled.
    global_schema: Optional[bytes] = None
    roles: immutables.Map[str, immutables.Map[str, Any]] | None = None

    is_explain: bool = False
    query_asts: Any = None
    run_and_rollback: bool = False
    append_tx_op: bool = False

    # Translation source map.
    source_map: Optional[pgcodegen.SourceMap] = None
    # For SQL queries, the length of the query prefix applied
    # after translation.
    sql_prefix_len: int = 0

    @property
    def has_ddl(self) -> bool:
        return bool(self.capabilities & enums.Capability.DDL)

    @property
    def tx_control(self) -> bool:
        return (
            bool(self.tx_id)
            or self.tx_rollback
            or self.tx_commit
            or self.tx_savepoint_declare
            or self.tx_savepoint_rollback
        )

    def serialize(self) -> bytes:
        rv = io.BytesIO()
        rv.write(b"\x01")  # 1 byte of version number
        pickle.dump(self, rv, -1)
        return rv.getvalue()

    @classmethod
    def deserialize(cls, data: bytes) -> Self:
        buf = memoryview(data)
        match buf[0]:
            case 0x00 | 0x01:
                return pickle.loads(buf[1:])  # type: ignore[no-any-return]
        raise ValueError(f"Bad version number: {buf[0]}")

    def maybe_use_func_cache(self) -> None:
        if self.cache_func_call is not None:
            sql, sql_hash = self.cache_func_call
            self.sql = sql
            self.sql_hash = sql_hash


@dataclasses.dataclass
class QueryUnitGroup:
    # All capabilities used by any query units in this group
    capabilities: enums.Capability = enums.Capability(0)

    # True if it is safe to cache this unit.
    cacheable: bool = True

    # True if any query unit has transaction control commands, like COMMIT,
    # ROLLBACK, START TRANSACTION or SAVEPOINT-related commands
    tx_control: bool = False

    # Cardinality of the result set.  Set to NO_RESULT if the
    # unit group is not expected or desired to return data.
    cardinality: enums.Cardinality = enums.Cardinality.NO_RESULT

    out_type_data: bytes = sertypes.NULL_TYPE_DESC
    out_type_id: bytes = sertypes.NULL_TYPE_ID.bytes
    in_type_data: bytes = sertypes.NULL_TYPE_DESC
    in_type_id: bytes = sertypes.NULL_TYPE_ID.bytes
    in_type_args: Optional[list[Param]] = None
    in_type_args_real_count: int = 0
    globals: Optional[list[tuple[str, bool]]] = None
    permissions: Optional[list[str]] = None
    json_permissions: Optional[list[str]] = None
    required_permissions: Optional[list[str]] = None

    server_param_conversions: Optional[list[ServerParamConversion]] = None
    unit_converted_param_indexes: Optional[dict[int, list[int]]] = None

    warnings: Optional[list[errors.EdgeDBError]] = None
    unsafe_isolation_dangers: (
        Optional[list[errors.UnsafeIsolationLevelError]]
    ) = None

    # Cacheable QueryUnit is serialized in the compiler, so that the I/O server
    # doesn't need to serialize it again for persistence.
    _units: list[QueryUnit | bytes] = dataclasses.field(default_factory=list)
    # This is a I/O server-only cache for unpacked QueryUnits
    _unpacked_units: list[QueryUnit] | None = None

    state_serializer: Optional[sertypes.StateSerializer] = None

    cache_state: int = 0
    tx_seq_id: int = 0

    force_non_normalized: bool = False

    graphql_key_variables: Optional[list[str]] = None

    @property
    def units(self) -> list[QueryUnit]:
        if self._unpacked_units is None:
            self._unpacked_units = [
                QueryUnit.deserialize(unit) if isinstance(unit, bytes) else unit
                for unit in self._units
            ]
        return self._unpacked_units

    def __iter__(self) -> Iterator[QueryUnit]:
        return iter(self.units)

    def __len__(self) -> int:
        return len(self._units)

    def __getitem__(self, item: int) -> QueryUnit:
        return self.units[item]

    def maybe_get_serialized(self, item: int) -> bytes | None:
        unit = self._units[item]
        if isinstance(unit, bytes):
            return unit
        return None

    def append(
        self,
        query_unit: QueryUnit,
        serialize: bool = True,
    ) -> None:
        self.capabilities |= query_unit.capabilities

        if not query_unit.cacheable:
            self.cacheable = False

        if query_unit.tx_control:
            self.tx_control = True

        self.cardinality = query_unit.cardinality
        self.out_type_data = query_unit.out_type_data
        self.out_type_id = query_unit.out_type_id
        self.in_type_data = query_unit.in_type_data
        self.in_type_id = query_unit.in_type_id
        self.in_type_args = query_unit.in_type_args
        self.in_type_args_real_count = query_unit.in_type_args_real_count
        if query_unit.globals is not None:
            if self.globals is None:
                self.globals = []
            self.globals.extend(query_unit.globals)
        if query_unit.permissions is not None:
            if self.permissions is None:
                self.permissions = []
            self.permissions.extend(query_unit.permissions)
        if query_unit.json_permissions is not None:
            if self.json_permissions is None:
                self.json_permissions = []
            self.json_permissions.extend(query_unit.json_permissions)
        if query_unit.required_permissions is not None:
            if self.required_permissions is None:
                self.required_permissions = []
            for perm in query_unit.required_permissions:
                if perm not in self.required_permissions:
                    self.required_permissions.append(perm)
        if query_unit.server_param_conversions is not None:
            if self.server_param_conversions is None:
                self.server_param_conversions = []
            if self.unit_converted_param_indexes is None:
                self.unit_converted_param_indexes = {}

            # De-duplicate param conversions and store information about which
            # units access which converted params.
            # If two units request the same conversion on the same parameter,
            # we should assume the conversion is stable and only do it once.
            unit_index = len(self._units)
            converted_param_indexes: list[int] = []
            for spc in query_unit.server_param_conversions:
                if spc in self.server_param_conversions:
                    converted_param_indexes.append(
                        self.server_param_conversions.index(spc)
                    )
                else:
                    converted_param_indexes.append(
                        len(self.server_param_conversions)
                    )
                    self.server_param_conversions.append(spc)
            self.unit_converted_param_indexes[unit_index] = (
                converted_param_indexes
            )

        if query_unit.warnings is not None:
            if self.warnings is None:
                self.warnings = []
            self.warnings.extend(query_unit.warnings)
        if query_unit.unsafe_isolation_dangers is not None:
            if self.unsafe_isolation_dangers is None:
                self.unsafe_isolation_dangers = []
            self.unsafe_isolation_dangers.extend(
                query_unit.unsafe_isolation_dangers)

        if not serialize or query_unit.cache_sql is None:
            self._units.append(query_unit)
        else:
            self._units.append(query_unit.serialize())


@dataclasses.dataclass(frozen=True, kw_only=True)
class PreparedStmtOpData:
    """Common prepared statement metadata"""

    stmt_name: str
    """Original statement name as passed by the frontend"""

    be_stmt_name: bytes = b""
    """Computed statement name as passed to the backend"""


@dataclasses.dataclass(frozen=True, kw_only=True)
class PrepareData(PreparedStmtOpData):
    """PREPARE statement data"""

    query: str
    """Translated query string"""
    source_map: Optional[pgcodegen.SourceMap] = None
    """Translation source map"""


@dataclasses.dataclass(frozen=True, kw_only=True)
class ExecuteData(PreparedStmtOpData):
    """EXECUTE statement data"""

    pass


@dataclasses.dataclass(frozen=True, kw_only=True)
class DeallocateData(PreparedStmtOpData):
    """DEALLOCATE statement data"""

    pass


@dataclasses.dataclass(kw_only=True)
class SQLQueryUnit:
    query: str = dataclasses.field(repr=False)
    """Translated query text."""

    prefix_len: int = 0
    source_map: Optional[pgcodegen.SourceMap] = None
    """Translation source map."""

    eql_format_query: Optional[str] = dataclasses.field(
        repr=False, default=None)
    """Translated query text returning data in single-column format."""

    orig_query: str = dataclasses.field(repr=False)
    """Original query text before translation."""

    # True if it is safe to cache this unit.
    cacheable: bool = True

    cardinality: enums.Cardinality = enums.Cardinality.NO_RESULT

    capabilities: enums.Capability = enums.Capability.NONE

    fe_settings: SQLSettings
    """Frontend-only settings effective during translation of this unit."""

    tx_action: Optional[TxAction] = None
    tx_chain: bool = False
    sp_name: Optional[str] = None

    prepare: Optional[PrepareData] = None
    execute: Optional[ExecuteData] = None
    deallocate: Optional[DeallocateData] = None

    set_vars: Optional[dict[Optional[str], Optional[SQLSetting]]] = None
    is_local: bool = False

    stmt_name: bytes = b""
    """Computed prepared statement name for this query."""

    frontend_only: bool = False
    """Whether the query is completely emulated outside of backend and so
    the response should be synthesized also."""

    command_complete_tag: Optional[CommandCompleteTag] = None
    """When set, CommandComplete for this query will be overridden.
    This is useful, for example, for setting the tag of DML statements,
    which return the number of modified rows."""

    params: Optional[list[SQLParam]] = None


class CommandCompleteTag:
    """Dictates the tag of CommandComplete message that concludes this query."""


@dataclasses.dataclass(kw_only=True)
class TagPlain(CommandCompleteTag):
    """Set the tag verbatim"""

    tag: bytes


@dataclasses.dataclass(kw_only=True)
class TagCountMessages(CommandCompleteTag):
    """Count DataRow messages in the response and set the tag to
    f'{prefix} {count_of_messages}'."""

    prefix: str


@dataclasses.dataclass(kw_only=True)
class TagUnpackRow(CommandCompleteTag):
    """Intercept a single DataRow message with a single column which represents
    the number of modified rows.
    Sets the CommandComplete tag to f'{prefix} {modified_rows}'."""

    prefix: str


class SQLParam:
    # Internal query param. Represents params in the compiled SQL, so the params
    # that are sent to PostgreSQL.

    # True for params that are actually used in the compiled query.
    used: bool = False


@dataclasses.dataclass(kw_only=True, eq=False, slots=True, repr=False)
class SQLParamExternal(SQLParam):
    # An internal query param whose value is provided by an external param.
    # So a user-visible param.

    # External params share the index with internal params
    pass


@dataclasses.dataclass(kw_only=True, eq=False, slots=True, repr=False)
class SQLParamExtractedConst(SQLParam):
    # An internal query param whose value is a constant that this param has
    # replaced during query normalization.

    type_oid: int


@dataclasses.dataclass(kw_only=True, eq=False, slots=True, repr=False)
class SQLParamGlobal(SQLParam):
    # An internal query param whose value is provided by a global.

    global_name: s_name.QualName

    pg_type: tuple[str, ...]

    is_permission: bool


@dataclasses.dataclass
class ParsedDatabase:
    user_schema_pickle: bytes
    schema_version: uuid.UUID
    database_config: immutables.Map[str, config.SettingValue]
    ext_config_settings: list[config.Setting]
    feature_used_metrics: dict[str, float]

    protocol_version: defines.ProtocolVersion
    state_serializer: sertypes.StateSerializer


SQLSetting = tuple[str | int | float, ...]
SQLSettings = immutables.Map[Optional[str], Optional[SQLSetting]]
DEFAULT_SQL_SETTINGS: SQLSettings = immutables.Map()
DEFAULT_SQL_FE_SETTINGS: SQLSettings = immutables.Map(
    {
        "search_path": ("public",),
        "server_version": cast(SQLSetting, (defines.PGEXT_POSTGRES_VERSION,)),
        "server_version_num": cast(
            SQLSetting, (defines.PGEXT_POSTGRES_VERSION_NUM,)
        ),
    }
)


@dataclasses.dataclass
class SQLTransactionState:
    in_tx: bool
    settings: SQLSettings
    in_tx_settings: Optional[SQLSettings]
    in_tx_local_settings: Optional[SQLSettings]
    savepoints: list[tuple[str, SQLSettings, SQLSettings]]

    def current_fe_settings(self) -> SQLSettings:
        if self.in_tx:
            return self.in_tx_local_settings or DEFAULT_SQL_FE_SETTINGS
        else:
            return self.settings or DEFAULT_SQL_FE_SETTINGS

    def get(self, name: str) -> Optional[SQLSetting]:
        if self.in_tx:
            # For easier access, in_tx_local_settings is always a superset of
            # in_tx_settings; in_tx_settings only keeps track of non-local
            # settings, so that the local settings don't go across tx bounds
            assert self.in_tx_local_settings
            return self.in_tx_local_settings[name]
        else:
            return self.settings[name]

    def apply(self, query_unit: SQLQueryUnit) -> None:
        if query_unit.tx_action == TxAction.COMMIT:
            self.in_tx = False
            self.settings = self.in_tx_settings  # type: ignore
            self.in_tx_settings = None
            self.in_tx_local_settings = None
            self.savepoints.clear()
        elif query_unit.tx_action == TxAction.ROLLBACK:
            self.in_tx = False
            self.in_tx_settings = None
            self.in_tx_local_settings = None
            self.savepoints.clear()
        elif query_unit.tx_action == TxAction.DECLARE_SAVEPOINT:
            assert query_unit.sp_name is not None
            assert self.in_tx_settings is not None
            assert self.in_tx_local_settings is not None
            self.savepoints.append(
                (
                    query_unit.sp_name,
                    self.in_tx_settings,
                    self.in_tx_local_settings,
                )
            )
        elif query_unit.tx_action == TxAction.ROLLBACK_TO_SAVEPOINT:
            while self.savepoints:
                sp_name, settings, local_settings = self.savepoints[-1]
                if query_unit.sp_name == sp_name:
                    self.in_tx_settings = settings
                    self.in_tx_local_settings = local_settings
                    break
                else:
                    self.savepoints.pop(0)
            else:
                raise errors.TransactionError(
                    f'savepoint "{query_unit.sp_name}" does not exist'
                )
        if not self.in_tx:
            # Always start an implicit transaction here, because in the
            # compiler, multiple apply() calls only happen in simple query,
            # and any query would start an implicit transaction. For example,
            # we need to support a single ROLLBACK without a matching BEGIN
            # rolling back an implicit transaction.
            self.in_tx = True
            self.in_tx_settings = self.settings
            self.in_tx_local_settings = self.settings
        if query_unit.frontend_only and query_unit.set_vars:
            for name, value in query_unit.set_vars.items():
                self.set(name, value, query_unit.is_local)

    def set(
        self, name: Optional[str], value: Optional[SQLSetting], is_local: bool
    ) -> None:
        def _set(attr_name: str) -> None:
            settings = getattr(self, attr_name)
            if value is None:
                if name in settings:
                    settings = settings.delete(name)
            else:
                settings = settings.set(name, value)
            setattr(self, attr_name, settings)

        if self.in_tx:
            _set("in_tx_local_settings")
            if not is_local:
                _set("in_tx_settings")
        elif not is_local:
            _set("settings")


#############################


class ProposedMigrationStep(NamedTuple):
    statements: tuple[str, ...]
    confidence: float
    prompt: str
    prompt_id: str
    data_safe: bool
    required_user_input: tuple[dict[str, str], ...]
    # This isn't part of the output data, but is used to figure out
    # what to prohibit when something is rejected.
    operation_key: s_delta.CommandKey

    def to_json(self) -> dict[str, Any]:
        return {
            "statements": [{"text": stmt} for stmt in self.statements],
            "confidence": self.confidence,
            "prompt": self.prompt,
            "prompt_id": self.prompt_id,
            "data_safe": self.data_safe,
            "required_user_input": list(self.required_user_input),
        }


class MigrationState(NamedTuple):
    parent_migration: Optional[s_migrations.Migration]
    initial_schema: s_schema.Schema
    initial_savepoint: Optional[str]
    target_schema: s_schema.Schema
    guidance: s_obj.DeltaGuidance
    accepted_cmds: tuple[qlast.Base, ...]
    last_proposed: Optional[tuple[ProposedMigrationStep, ...]]


class MigrationRewriteState(NamedTuple):
    initial_savepoint: Optional[str]
    target_schema: s_schema.Schema
    accepted_migrations: tuple[qlast.CreateMigration, ...]


class TransactionState(NamedTuple):
    id: int
    name: Optional[str]
    local_user_schema: s_schema.FlatSchema | None
    global_schema: s_schema.FlatSchema
    modaliases: immutables.Map[Optional[str], str]
    session_config: immutables.Map[str, config.SettingValue]
    database_config: immutables.Map[str, config.SettingValue]
    system_config: immutables.Map[str, config.SettingValue]
    cached_reflection: immutables.Map[str, tuple[str, ...]]
    tx: Transaction
    migration_state: Optional[MigrationState] = None
    migration_rewrite_state: Optional[MigrationRewriteState] = None

    @property
    def user_schema(self) -> s_schema.FlatSchema:
        if self.local_user_schema is None:
            return self.tx.root_user_schema
        else:
            return self.local_user_schema


class Transaction:

    # Fields that affects the state key are listed here. The key is used
    # to determine if we can reuse a previously-pickled state, so remember
    # to update get_state_key() below when adding new fields affecting the
    # state key.  See also edb/server/compiler_pool/worker.py
    _id: int
    _savepoints: dict[int, TransactionState]
    _current: TransactionState

    # backref to the owning state object
    _constate: CompilerConnectionState

    def __init__(
        self,
        constate: CompilerConnectionState,
        *,
        user_schema: s_schema.FlatSchema,
        global_schema: s_schema.FlatSchema,
        modaliases: immutables.Map[Optional[str], str],
        session_config: immutables.Map[str, config.SettingValue],
        database_config: immutables.Map[str, config.SettingValue],
        system_config: immutables.Map[str, config.SettingValue],
        cached_reflection: immutables.Map[str, tuple[str, ...]],
        implicit: bool = True,
    ) -> None:
        assert not isinstance(user_schema, s_schema.ChainedSchema)

        self._constate = constate

        self._id = constate._new_txid()
        self._implicit = implicit

        self._current = TransactionState(
            id=self._id,
            name=None,
            local_user_schema=(
                None if user_schema is self.root_user_schema else user_schema
            ),
            global_schema=global_schema,
            modaliases=modaliases,
            session_config=session_config,
            database_config=database_config,
            system_config=system_config,
            cached_reflection=cached_reflection,
            tx=self,
        )

        self._state0 = self._current
        self._savepoints = {}

    def get_state_key(self) -> tuple[int, tuple[int, ...], TransactionState]:
        return (
            self._id,
            tuple(self._savepoints.keys()),
            self._current,  # TransactionState is immutable
        )

    @property
    def id(self) -> int:
        return self._id

    @property
    def root_user_schema(self) -> s_schema.FlatSchema:
        return self._constate.root_user_schema

    def is_implicit(self) -> bool:
        return self._implicit

    def make_explicit(self) -> None:
        if self._implicit:
            self._implicit = False
        else:
            raise errors.TransactionError("already in explicit transaction")

    def declare_savepoint(self, name: str) -> int:
        if self.is_implicit():
            raise errors.TransactionError(
                "savepoints can only be used in transaction blocks"
            )

        return self._declare_savepoint(name)

    def start_migration(self) -> str:
        name = str(uuid.uuid4())
        self._declare_savepoint(name)
        return name

    def _declare_savepoint(self, name: str) -> int:
        sp_id = self._constate._new_txid()
        sp_state = self._current._replace(id=sp_id, name=name)
        self._savepoints[sp_id] = sp_state
        self._constate._savepoints_log[sp_id] = sp_state
        return sp_id

    def rollback_to_savepoint(self, name: str) -> TransactionState:
        if self.is_implicit():
            raise errors.TransactionError(
                "savepoints can only be used in transaction blocks"
            )

        return self._rollback_to_savepoint(name)

    def abort_migration(self, name: str) -> None:
        self._rollback_to_savepoint(name)

    def _rollback_to_savepoint(self, name: str) -> TransactionState:
        sp_ids_to_erase = []
        for sp in reversed(self._savepoints.values()):
            if sp.name == name:
                self._current = sp
                break

            sp_ids_to_erase.append(sp.id)
        else:
            raise errors.TransactionError(f"there is no {name!r} savepoint")

        for sp_id in sp_ids_to_erase:
            self._savepoints.pop(sp_id)

        return sp

    def release_savepoint(self, name: str) -> None:
        if self.is_implicit():
            raise errors.TransactionError(
                "savepoints can only be used in transaction blocks"
            )

        self._release_savepoint(name)

    def commit_migration(self, name: str) -> None:
        self._release_savepoint(name)

    def _release_savepoint(self, name: str) -> None:
        sp_ids_to_erase = []
        for sp in reversed(self._savepoints.values()):
            sp_ids_to_erase.append(sp.id)

            if sp.name == name:
                break
        else:
            raise errors.TransactionError(f"there is no {name!r} savepoint")

        for sp_id in sp_ids_to_erase:
            self._savepoints.pop(sp_id)

    def get_schema(self, std_schema: s_schema.FlatSchema) -> s_schema.Schema:
        assert isinstance(std_schema, s_schema.FlatSchema)
        return s_schema.ChainedSchema(
            std_schema,
            self._current.user_schema,
            self._current.global_schema,
        )

    def get_user_schema(self) -> s_schema.FlatSchema:
        return self._current.user_schema

    def get_user_schema_if_updated(self) -> Optional[s_schema.FlatSchema]:
        if self._current.user_schema is self._state0.user_schema:
            return None
        else:
            return self._current.user_schema

    def get_global_schema(self) -> s_schema.FlatSchema:
        return self._current.global_schema

    def get_global_schema_if_updated(self) -> Optional[s_schema.FlatSchema]:
        if self._current.global_schema is self._state0.global_schema:
            return None
        else:
            return self._current.global_schema

    def get_modaliases(self) -> immutables.Map[Optional[str], str]:
        return self._current.modaliases

    def get_session_config(self) -> immutables.Map[str, config.SettingValue]:
        return self._current.session_config

    def get_database_config(self) -> immutables.Map[str, config.SettingValue]:
        return self._current.database_config

    def get_system_config(self) -> immutables.Map[str, config.SettingValue]:
        return self._current.system_config

    def get_cached_reflection_if_updated(
        self,
    ) -> Optional[immutables.Map[str, tuple[str, ...]]]:
        if self._current.cached_reflection == self._state0.cached_reflection:
            return None
        else:
            return self._current.cached_reflection

    def get_cached_reflection(self) -> immutables.Map[str, tuple[str, ...]]:
        return self._current.cached_reflection

    def get_migration_state(self) -> Optional[MigrationState]:
        return self._current.migration_state

    def get_migration_rewrite_state(self) -> Optional[MigrationRewriteState]:
        return self._current.migration_rewrite_state

    def update_schema(self, new_schema: s_schema.Schema) -> None:
        assert isinstance(new_schema, s_schema.ChainedSchema)
        user_schema = new_schema.get_top_schema()
        assert isinstance(user_schema, s_schema.FlatSchema)
        global_schema = new_schema.get_global_schema()
        assert isinstance(global_schema, s_schema.FlatSchema)
        self._current = self._current._replace(
            local_user_schema=user_schema,
            global_schema=global_schema,
        )

    def update_modaliases(
        self, new_modaliases: immutables.Map[Optional[str], str]
    ) -> None:
        self._current = self._current._replace(modaliases=new_modaliases)

    def update_session_config(
        self, new_config: immutables.Map[str, config.SettingValue]
    ) -> None:
        self._current = self._current._replace(session_config=new_config)

    def update_database_config(
        self, new_config: immutables.Map[str, config.SettingValue]
    ) -> None:
        self._current = self._current._replace(database_config=new_config)

    def update_cached_reflection(
        self,
        new: immutables.Map[str, tuple[str, ...]],
    ) -> None:
        self._current = self._current._replace(cached_reflection=new)

    def update_migration_state(self, mstate: Optional[MigrationState]) -> None:
        self._current = self._current._replace(migration_state=mstate)

    def update_migration_rewrite_state(
        self, mrstate: Optional[MigrationRewriteState]
    ) -> None:
        self._current = self._current._replace(migration_rewrite_state=mrstate)


CStateStateType = tuple[dict[int, TransactionState], Transaction, int]


class CompilerConnectionState:
    __slots__ = ("_savepoints_log", "_current_tx", "_tx_count", "_user_schema")

    # Fields that affects the state key are listed here. The key is used
    # to determine if we can reuse a previously-pickled state, so remember
    # to update get_state_key() below when adding new fields affecting the
    # state key.  See also edb/server/compiler_pool/worker.py
    _tx_count: int
    _savepoints_log: dict[int, TransactionState]
    _current_tx: Transaction

    _user_schema: Optional[s_schema.FlatSchema]

    def __init__(
        self,
        *,
        user_schema: s_schema.Schema,
        global_schema: s_schema.Schema,
        modaliases: immutables.Map[Optional[str], str],
        session_config: immutables.Map[str, config.SettingValue],
        database_config: immutables.Map[str, config.SettingValue],
        system_config: immutables.Map[str, config.SettingValue],
        cached_reflection: immutables.Map[str, tuple[str, ...]],
    ):
        assert isinstance(user_schema, s_schema.FlatSchema)
        self._user_schema = user_schema
        self._tx_count = time.monotonic_ns()
        self._init_current_tx(
            user_schema=user_schema,
            global_schema=global_schema,
            modaliases=modaliases,
            session_config=session_config,
            database_config=database_config,
            system_config=system_config,
            cached_reflection=cached_reflection,
        )
        self._savepoints_log = {}

    def get_state_key(self) -> tuple[tuple[int, ...], int, tuple[Any, ...]]:
        # This would be much more efficient if CompilerConnectionState
        # and TransactionState objects were immutable. But they are not,
        # so we have
        return (
            tuple(self._savepoints_log.keys()),
            self._tx_count,
            self._current_tx.get_state_key(),
        )

    def __getstate__(self) -> CStateStateType:
        return self._savepoints_log, self._current_tx, self._tx_count

    def __setstate__(self, state: CStateStateType) -> None:
        self._savepoints_log, self._current_tx, self._tx_count = state
        self._user_schema = None

    @property
    def root_user_schema(self) -> s_schema.FlatSchema:
        assert self._user_schema is not None
        return self._user_schema

    def set_root_user_schema(self, user_schema: s_schema.FlatSchema) -> None:
        self._user_schema = user_schema

    def _new_txid(self) -> int:
        self._tx_count += 1
        return self._tx_count

    def _init_current_tx(
        self,
        *,
        user_schema: s_schema.Schema,
        global_schema: s_schema.Schema,
        modaliases: immutables.Map[Optional[str], str],
        session_config: immutables.Map[str, config.SettingValue],
        database_config: immutables.Map[str, config.SettingValue],
        system_config: immutables.Map[str, config.SettingValue],
        cached_reflection: immutables.Map[str, tuple[str, ...]],
    ) -> None:
        assert isinstance(user_schema, s_schema.FlatSchema)
        assert isinstance(global_schema, s_schema.FlatSchema)
        self._current_tx = Transaction(
            self,
            user_schema=user_schema,
            global_schema=global_schema,
            modaliases=modaliases,
            session_config=session_config,
            database_config=database_config,
            system_config=system_config,
            cached_reflection=cached_reflection,
        )

    def can_sync_to_savepoint(self, spid: int) -> bool:
        return spid in self._savepoints_log

    def sync_to_savepoint(self, spid: int) -> None:
        """Synchronize the compiler state with the current DB state."""

        if not self.can_sync_to_savepoint(spid):
            raise RuntimeError(f"failed to lookup savepoint with id={spid}")

        sp = self._savepoints_log[spid]
        self._current_tx = sp.tx
        self._current_tx._current = sp
        self._current_tx._id = spid

        # Cleanup all savepoints declared after the one we rolled back to
        # in the transaction we have now set as current.
        for id in tuple(self._current_tx._savepoints):
            if id > spid:
                self._current_tx._savepoints.pop(id)

        # Cleanup all savepoints declared after the one we rolled back to
        # in the global savepoints log.
        for id in tuple(self._savepoints_log):
            if id > spid:
                self._savepoints_log.pop(id)

    def current_tx(self) -> Transaction:
        return self._current_tx

    def start_tx(self) -> None:
        if self._current_tx.is_implicit():
            self._current_tx.make_explicit()
        else:
            raise errors.TransactionError("already in transaction")

    def rollback_tx(self) -> TransactionState:
        # Note that we might not be in a transaction as we allow
        # ROLLBACKs outside of transaction blocks (just like Postgres).

        prior_state = self._current_tx._state0

        self._init_current_tx(
            user_schema=prior_state.user_schema,
            global_schema=prior_state.global_schema,
            modaliases=prior_state.modaliases,
            session_config=prior_state.session_config,
            database_config=prior_state.database_config,
            system_config=prior_state.system_config,
            cached_reflection=prior_state.cached_reflection,
        )

        return prior_state

    def commit_tx(self) -> TransactionState:
        if self._current_tx.is_implicit():
            raise errors.TransactionError("cannot commit: not in transaction")

        latest_state = self._current_tx._current

        self._init_current_tx(
            user_schema=latest_state.user_schema,
            global_schema=latest_state.global_schema,
            modaliases=latest_state.modaliases,
            session_config=latest_state.session_config,
            database_config=latest_state.database_config,
            system_config=latest_state.system_config,
            cached_reflection=latest_state.cached_reflection,
        )

        return latest_state

    def sync_tx(self, txid: int) -> None:
        if self._current_tx.id == txid:
            return

        if self.can_sync_to_savepoint(txid):
            self.sync_to_savepoint(txid)
            return

        raise errors.InternalServerError(
            f"failed to lookup transaction or savepoint with id={txid}"
        )  # pragma: no cover
