#
# This source file is part of the EdgeDB open source project.
#
# Copyright MagicStack Inc. and the EdgeDB authors.
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
from dataclasses import dataclass, field
from typing import (
    cast,
    Any,
    AsyncIterator,
    ClassVar,
    Literal,
    NoReturn,
    Optional,
    Sequence,
    TYPE_CHECKING,
)

import abc
import asyncio
import contextlib
import contextvars
import itertools
import json
import logging
import uuid

import tiktoken
from mistral_common.tokens.tokenizers import mistral as mistral_tokenizer

from edb import errors
from edb.common import asyncutil
from edb.common import debug
from edb.common import enum as s_enum
from edb.common import markup
from edb.common import uuidgen

from edb.server import compiler, http
from edb.server import defines as edbdef
from edb.server.compiler import sertypes
from edb.server.protocol import execute
from edb.server.protocol import request_scheduler as rs

if TYPE_CHECKING:
    from edb.server import dbview
    from edb.server import tenant as srv_tenant
    from edb.server import pgcon
    from edb.server.protocol import protocol


logger = logging.getLogger("edb.server.ai_ext")
keepalive_token = "ai-index-builder"


class AIExtError(Exception):
    http_status: ClassVar[http.HTTPStatus] = (
        http.HTTPStatus.INTERNAL_SERVER_ERROR)

    def __init__(
        self,
        *args: object,
        json: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(*args)
        self._json = json

    def get_http_status(self) -> http.HTTPStatus:
        return self.__class__.http_status

    def json(self) -> dict[str, Any]:
        if self._json is not None:
            return self._json
        else:
            return {
                "message": str(self.args[0]),
                "type": self.__class__.__name__,
            }


class AIProviderError(AIExtError):
    pass


class ConfigurationError(AIExtError):
    pass


class InternalError(AIExtError):
    pass


class BadRequestError(AIExtError):
    http_status = http.HTTPStatus.BAD_REQUEST


class ApiStyle(s_enum.StrEnum):
    OpenAI = 'OpenAI'
    Anthropic = 'Anthropic'
    VoyageAI = 'VoyageAI'
    Ollama = 'Ollama'


class Tokenizer(abc.ABC):

    @abc.abstractmethod
    def encode(self, text: str) -> list[int]:
        """Encode text into tokens."""
        raise NotImplementedError

    @abc.abstractmethod
    def encode_padding(self) -> int:
        """How many special characters are added to encodings?"""
        raise NotImplementedError

    @abc.abstractmethod
    def decode(self, tokens: list[int]) -> str:
        """Decode tokens into text."""
        raise NotImplementedError

    def shorten_to_token_length(
        self, text: str, token_length: int
    ) -> tuple[str, int]:
        """Truncate text to a maximum token length."""
        encoded = self.encode(text)
        if len(encoded) > token_length:
            encoded = encoded[:token_length]
        return self.decode(encoded), len(encoded)


class OpenAITokenizer(Tokenizer):

    _instances: dict[str, OpenAITokenizer] = {}

    encoding: Any

    @classmethod
    def for_model(cls, model_name: str) -> OpenAITokenizer:
        if model_name in cls._instances:
            return cls._instances[model_name]

        tokenizer = OpenAITokenizer()
        tokenizer.encoding = tiktoken.encoding_for_model(model_name)
        cls._instances[model_name] = tokenizer

        return tokenizer

    def encode(self, text: str) -> list[int]:
        return cast(list[int], self.encoding.encode(text))

    def encode_padding(self) -> int:
        return 0

    def decode(self, tokens: list[int]) -> str:
        return cast(str, self.encoding.decode(tokens))


class MistralTokenizer(Tokenizer):

    _instances: dict[str, MistralTokenizer] = {}

    tokenizer: Any

    @classmethod
    def for_model(cls, model_name: str) -> MistralTokenizer:
        if model_name in cls._instances:
            return cls._instances[model_name]

        assert model_name == 'mistral-embed'

        tokenizer = MistralTokenizer()
        tokenizer.tokenizer = mistral_tokenizer.MistralTokenizer.v1()
        cls._instances[model_name] = tokenizer

        return tokenizer

    def encode(self, text: str) -> list[int]:
        # V1 tokenizer wraps input text with control tokens [INST] [/INST].
        #
        # While these count towards the overal token limit, how special tokens
        # are applied to embedding requests is not documented. For now, directly
        # pass the text into the inner tokenizer.
        tokenized = self.tokenizer.instruct_tokenizer.tokenizer.encode(
            text, bos=False, eos=False
        )
        return cast(list[int], tokenized)

    def encode_padding(self) -> int:
        # V1 tokenizer wraps input text with control tokens [INST] [/INST].
        #
        # This is only 2 tokens, and testing shows that mistral-embed does add
        # two tokens to embeddings inputs. However, this is not documented, so
        # add some extra leeway in case things change.
        #
        # Note, other models may use significantly more control tokens.
        return 16

    def decode(self, tokens: list[int]) -> str:
        return cast(str, self.tokenizer.decode(tokens))


class OllamaTokenizer(Tokenizer):

    """
    Simply counts the number of characters.
    A tokenizer API is in progress, but unlikely to be released soon.
    """

    _instances: dict[str, OllamaTokenizer] = {}

    @classmethod
    def for_model(cls, model_name: str) -> OllamaTokenizer:
        if model_name in cls._instances:
            return cls._instances[model_name]

        tokenizer = OllamaTokenizer()
        cls._instances[model_name] = tokenizer

        return tokenizer

    def encode(self, text: str) -> list[int]:
        return [ord(c) for c in text]

    def encode_padding(self) -> int:
        return 0

    def decode(self, tokens: list[int]) -> str:
        return ''.join(chr(c) for c in tokens)


class TestTokenizer(Tokenizer):

    _instances: dict[str, TestTokenizer] = {}

    @classmethod
    def for_model(cls, model_name: str) -> TestTokenizer:
        if model_name in cls._instances:
            return cls._instances[model_name]

        tokenizer = TestTokenizer()
        cls._instances[model_name] = tokenizer

        return tokenizer

    def encode(self, text: str) -> list[int]:
        return [ord(c) for c in text]

    def encode_padding(self) -> int:
        return 0

    def decode(self, tokens: list[int]) -> str:
        return ''.join(chr(c) for c in tokens)


def get_model_tokenizer(
    provider_name: str,
    model_name: str,
) -> Optional[Tokenizer]:
    """Get the tokenizer for a given provider and model"""
    if provider_name == 'builtin::openai':
        return OpenAITokenizer.for_model(model_name)
    elif provider_name == 'builtin::mistral':
        return MistralTokenizer.for_model(model_name)
    if provider_name == 'builtin::ollama':
        return OllamaTokenizer.for_model(model_name)
    elif provider_name == 'custom::test':
        return TestTokenizer.for_model(model_name)
    else:
        return None


@dataclass
class ProviderConfig:
    name: str
    display_name: str
    api_url: str
    client_id: str
    secret: str
    api_style: ApiStyle

    def get_embeddings_from_result(
        self, embeddings_result: bytes
    ) -> list[list[float]]:
        """Decode and extract the embeddings from an embeddings request."""

        decoded_result = json.loads(
            embeddings_result.decode("utf-8")
        )

        if self.api_style == ApiStyle.Ollama:
            return cast(
                list[list[float]],
                decoded_result["embeddings"],
            )
        else:
            return cast(
                list[list[float]],
                [
                    entry_result["embedding"]
                    for entry_result in decoded_result["data"]
                ],
            )


def start_extension(
    tenant: srv_tenant.Tenant,
    dbname: str,
) -> None:
    task_name = _get_builder_task_name(dbname)
    task = tenant.get_task(task_name)
    if task is None:
        logger.info(f"starting AI extension tasks on database {dbname!r}")
        tenant.create_task(
            _ext_ai_index_builder_controller_loop(tenant, dbname),
            interruptable=True,
            name=task_name,
        )


def stop_extension(
    tenant: srv_tenant.Tenant,
    dbname: str,
) -> None:
    task_name = _get_builder_task_name(dbname)
    task = tenant.get_task(task_name)
    if task is not None:
        logger.info(f"stopping AI extension tasks on database {dbname!r}")
        task.cancel()


def _get_builder_task_name(dbname: str) -> str:
    return f"ext::ai::index builder on database {dbname!r}"


_task_name = contextvars.ContextVar(
    "ext_ai_index_builder_task_name", default="-")


async def _ext_ai_index_builder_controller_loop(
    tenant: srv_tenant.Tenant,
    dbname: str,
) -> None:
    task_name = _get_builder_task_name(dbname)
    _task_name.set(task_name)
    logger.info(f"started {task_name}")
    db = tenant.get_db(dbname=dbname)
    holding_lock = False

    await db.introspection()
    naptime_cfg = db.lookup_config("ext::ai::Config::indexer_naptime")
    naptime = naptime_cfg.to_microseconds() / 1000000

    provider_schedulers: dict[str, ProviderScheduler] = {}

    try:
        while tenant.accept_new_tasks:
            if not db.tenant.is_database_connectable(dbname):
                # Don't do work if the database is not connectable,
                # e.g. being dropped
                await asyncio.sleep(naptime)
                continue

            models = []
            sleep_timer: rs.Timer = rs.Timer(None, False)
            try:
                async with tenant.with_pgcon(dbname) as pgconn:
                    models = await _ext_ai_fetch_active_models(pgconn)
                    if models:
                        if not holding_lock:
                            holding_lock = await _ext_ai_lock(tenant, pgconn)
                        if holding_lock:
                            provider_contexts = _prepare_provider_contexts(
                                db,
                                pgconn,
                                tenant.get_http_client(originator="ai/index"),
                                models,
                                provider_schedulers,
                                naptime,
                            )
                            try:
                                sleep_timer = (
                                    await _ext_ai_index_builder_work(
                                        provider_schedulers,
                                        provider_contexts,
                                    )
                                )
                            finally:
                                if not sleep_timer.is_ready_and_urgent():
                                    await asyncutil.deferred_shield(
                                        _ext_ai_unlock(tenant))
                                    tenant.server.remove_keepalive_token(
                                        (
                                            keepalive_token,
                                            tenant.get_instance_name(),
                                            dbname,
                                        )
                                    )
                                    holding_lock = False
            except Exception:
                logger.error(
                    f"caught error in {task_name}",
                    exc_info=tenant.accept_new_tasks,
                )

            if not tenant.accept_new_tasks:
                break

            if not sleep_timer.is_ready_and_urgent():
                delay = sleep_timer.remaining_time(naptime)
                if delay == naptime:
                    logger.debug(
                        f"{task_name}: "
                        f"No work. Napping for {naptime:.2f} seconds."
                    )
                await asyncio.sleep(delay)

    finally:
        logger.info(f"stopped {task_name}")


async def _ext_ai_fetch_active_models(
    pgconn: pgcon.PGConnection,
) -> list[tuple[int, str, str]]:
    models = await pgconn.sql_fetch(
        b"""
            SELECT
                id,
                name,
                provider
            FROM
                edgedbext.ai_active_embedding_models
        """,
    )

    result = []
    if models:
        for model in models:
            result.append((
                int.from_bytes(model[0], byteorder="big", signed=True),
                model[1].decode("utf-8"),
                model[2].decode("utf-8"),
            ))

    return result


# The _ext_ai_lock() is a long-term lock held in the system pgcon. It is used
# in the index builder job above guarding multiple alternating database pgcons
# and outgoing HTTP requests (free up pgcons while waiting for a response from
# external services), so that different Gel tenants on the same backend
# run this job exclusively.
#
# The following implementation is also safe to be used by multiple tasks within
# the same tenant (though at the time of writing, there is only one such task
# per tenant). To achieve this, we added an extra query on pg_locks to check if
# it's already held by another task, because advisory locks allow reentrancy
# from the same session (the same sys_pgcon). And to avoid racing conditions,
# we use another advisory lock over the 2 queries of check-and-lock in the
# local session. This also means, one must use _ext_ai_lock() instead of an
# individual lock of the 2 locks here to avoid misuse.
#
# If you are editing the magic numbers here: make sure it fits in a Postgres
# Oid type (uint32), or you'll need to change the `classid` query below.
_EXT_AI_ADVISORY_LOCK = b"3987734540"
_EXT_AI_ADVISORY_LOCK_LOCK = b"3987734541"


async def _ext_ai_lock(
    tenant: srv_tenant.Tenant,
    pgconn: pgcon.PGConnection,
) -> bool:
    # We use transaction-level advisory locks to ensure releasing
    await pgconn.sql_execute(b"START TRANSACTION")
    try:
        b = await pgconn.sql_fetch_val(
            b"SELECT pg_try_advisory_xact_lock("
            + _EXT_AI_ADVISORY_LOCK_LOCK
            + b")"
        )
        if b == b'\x01':
            lock_free = await pgconn.sql_fetch_val(
                b'''
                SELECT NOT EXISTS (
                    SELECT
                        1
                    FROM
                        pg_locks
                    WHERE
                        locktype = 'advisory'
                        AND classid = 0
                        AND objid = \
                ''' + _EXT_AI_ADVISORY_LOCK + b')')
            if lock_free == b'\x01':
                async with tenant.use_sys_pgcon() as syscon:
                    # The long-term holding lock must be on session-level
                    b = await syscon.sql_fetch_val(
                        b"SELECT pg_try_advisory_lock("
                        + _EXT_AI_ADVISORY_LOCK
                        + b")"
                    )
                    return b == b'\x01'
    finally:
        await pgconn.sql_execute(b"ROLLBACK")

    return False


async def _ext_ai_unlock(
    tenant: srv_tenant.Tenant,
) -> None:
    async with tenant.use_sys_pgcon() as syscon:
        await syscon.sql_fetch_val(
            b"SELECT pg_advisory_unlock(" + _EXT_AI_ADVISORY_LOCK + b")")


def _prepare_provider_contexts(
    db: dbview.Database,
    pgconn: pgcon.PGConnection,
    http_client: http.HttpClient,
    models: list[tuple[int, str, str]],
    provider_schedulers: dict[str, ProviderScheduler],
    naptime: float,
) -> dict[str, ProviderContext]:

    models_by_provider: dict[str, list[str]] = {}
    for entry in models:
        model_name = entry[1]
        provider_name = entry[2]
        try:
            models_by_provider[provider_name].append(model_name)
        except KeyError:
            m = models_by_provider[provider_name] = []
            m.append(model_name)

    # Drop any extra providers, they were probably deleted.
    unused_provider_names = {
        provider_name
        for provider_name in provider_schedulers.keys()
        if provider_name not in models_by_provider
    }
    for unused_provider_name in unused_provider_names:
        provider_schedulers.pop(unused_provider_name, None)

    # Create contexts
    provider_contexts = {}

    for provider_name, provider_models in models_by_provider.items():
        if provider_name not in provider_schedulers:
            # Create new schedulers if necessary
            provider_schedulers[provider_name] = ProviderScheduler(
                service=rs.Service(
                    limits={'requests': None, 'tokens': None},
                ),
                provider_name=provider_name,
            )
        provider_scheduler = provider_schedulers[provider_name]

        if not provider_scheduler.timer.is_ready():
            continue

        provider_contexts[provider_name] = ProviderContext(
            naptime=naptime,
            db=db,
            pgconn=pgconn,
            http_client=http_client,
            provider_models=provider_models,
        )

    return provider_contexts


async def _ext_ai_index_builder_work(
    provider_schedulers: dict[str, ProviderScheduler],
    provider_contexts: dict[str, ProviderContext],
) -> rs.Timer:

    async with asyncio.TaskGroup() as g:
        for provider_name, provider_scheduler in provider_schedulers.items():
            if provider_name not in provider_contexts:
                continue

            provider_context = provider_contexts[provider_name]
            g.create_task(provider_scheduler.process(provider_context))

    sleep_timer = rs.Timer.combine(
        provider_scheduler.timer
        for provider_scheduler in provider_schedulers.values()
    )
    if sleep_timer is not None:
        return sleep_timer
    else:
        # Return any non-urgent timer
        return rs.Timer(None, False)


@dataclass(frozen=True)
class EmbeddingsData:
    embeddings: bytes


@dataclass
class ProviderContext(rs.Context):

    db: dbview.Database
    pgconn: pgcon.PGConnection
    http_client: http.HttpClient
    provider_models: list[str]


@dataclass
class ProviderScheduler(rs.Scheduler[EmbeddingsData]):

    provider_name: str = ''

    # If a text is too long for a model, it will be excluded from embeddings
    # to prevent pointlessly wasting requests and tokens.
    # An embedding index may have its `truncate_to_max` flag switched. If the
    # flag is on, previously excluded inputs will be truncated and processed.
    model_excluded_ids: dict[str, list[str]] = field(default_factory=dict)

    async def get_params(
        self, context: rs.Context,
    ) -> Optional[Sequence[EmbeddingsParams]]:
        assert isinstance(context, ProviderContext)
        rv = await _generate_embeddings_params(
            context.db,
            context.pgconn,
            context.http_client,
            self.provider_name,
            context.provider_models,
            self.model_excluded_ids,
            tokens_rate_limit=(
                self.service.limits['tokens'].total
                if self.service.limits['tokens'] is not None else
                None
            ),
        )
        if rv:
            context.db.server.add_keepalive_token(
                (
                    keepalive_token,
                    context.db.tenant.get_instance_name(),
                    context.db.name,
                )
            )
        return rv

    def finalize(self, execution_report: rs.ExecutionReport) -> None:
        task_name = _task_name.get()

        for message in execution_report.known_error_messages:
            logger.error(
                f"{task_name}: "
                f"Could not generate embeddings for {self.provider_name} "
                f"due to an internal error: {message}"
            )


@dataclass(frozen=True, kw_only=True)
class EmbeddingsParams(rs.Params[EmbeddingsData]):
    pgconn: pgcon.PGConnection
    http_client: http.HttpClient
    provider: ProviderConfig
    model_name: str
    inputs: list[tuple[PendingEmbedding, str]]
    token_count: int
    shortening: Optional[int]
    user: Optional[str]

    def costs(self) -> dict[str, int]:
        return {
            'requests': 1,
            'tokens': self.token_count,
        }

    def create_request(self) -> EmbeddingsRequest:
        return EmbeddingsRequest(self)


class EmbeddingsRequest(rs.Request[EmbeddingsData]):

    async def run(self) -> Optional[rs.Result[EmbeddingsData]]:
        task_name = _task_name.get()

        try:
            assert isinstance(self.params, EmbeddingsParams)
            result = await _generate_embeddings(
                self.params.provider,
                self.params.model_name,
                [input[1] for input in self.params.inputs],
                self.params.shortening,
                self.params.user,
                self.params.http_client,
            )
            result.pgconn = self.params.pgconn
            result.pending_entries = [
                input[0] for input in self.params.inputs
            ]
            return result
        except AIExtError as e:
            logger.error(f"{task_name}: {e}")
            return None
        except Exception as e:
            logger.error(
                f"{task_name}: could not generate embeddings "
                f"due to an internal error: {e}"
            )
            return None


class EmbeddingsResult(rs.Result[EmbeddingsData]):

    provider_cfg: ProviderConfig
    pgconn: Optional[Any] = None
    pending_entries: Optional[list[PendingEmbedding]] = None

    async def finalize(self) -> None:
        if isinstance(self.data, rs.Error):
            return
        if self.pgconn is None or self.pending_entries is None:
            return

        # Entries must line up with the embeddings data:
        # - `_generate_embeddings` produces produces embeddings data matching
        #   the order of its inputs
        #
        # Entries must be grouped by target rel:
        # - `_generate_embeddings_params` sorts inputs by target rel before
        groups = itertools.groupby(
            self.pending_entries, key=lambda e: (e.target_rel, e.target_attr),
        )
        offset = 0
        for (rel, attr), items in groups:
            ids = [item.id for item in items]
            await _update_embeddings_in_db(
                self.pgconn,
                self.provider_cfg,
                rel,
                attr,
                ids,
                self.data.embeddings,
                offset,
            )
            offset += len(ids)


async def _generate_embeddings_params(
    db: dbview.Database,
    pgconn: pgcon.PGConnection,
    http_client: http.HttpClient,
    provider_name: str,
    provider_models: list[str],
    model_excluded_ids: dict[str, list[str]],
    *,
    tokens_rate_limit: Optional[int | Literal['unlimited']],
) -> Optional[list[EmbeddingsParams]]:
    task_name = _task_name.get()

    try:
        provider_cfg = _get_provider_config(
            db=db, provider_name=provider_name)
    except LookupError as e:
        logger.error(f"{task_name}: {e}")
        return None

    model_max_input_tokens: dict[str, int] = {
        model_name: await _get_model_annotation_as_int(
            db,
            base_model_type="ext::ai::EmbeddingModel",
            model_name=model_name,
            annotation_name="ext::ai::embedding_model_max_input_tokens",
        )
        for model_name in provider_models
    }

    model_max_batch_tokens: dict[str, int] = {
        model_name: await _get_model_annotation_as_int(
            db,
            base_model_type="ext::ai::EmbeddingModel",
            model_name=model_name,
            annotation_name="ext::ai::embedding_model_max_batch_tokens",
        )
        for model_name in provider_models
    }

    model_pending_entries: dict[str, list[PendingEmbedding]] = {}

    for model_name in provider_models:
        logger.debug(
            f"{task_name} considering {model_name!r} "
            f"indexes via {provider_name!r}"
        )

        pending_entries = await _get_pending_embeddings(
            pgconn, model_name, model_excluded_ids
        )

        if not pending_entries:
            continue

        logger.debug(
            f"{task_name} found {len(pending_entries)} entries to index"
        )

        try:
            model_list = model_pending_entries[model_name]
        except KeyError:
            model_list = model_pending_entries[model_name] = []

        model_list.extend(pending_entries)

    embeddings_params: list[EmbeddingsParams] = []

    for model_name, pending_entries in model_pending_entries.items():
        groups = itertools.groupby(
            pending_entries, key=lambda e: e.target_dims_shortening
        )
        for shortening, part_iter in groups:
            part = list(part_iter)
            part_texts = [(p.text, p.truncate_to_max) for p in part]

            batches, excluded_indexes = batch_texts(
                part_texts,
                get_model_tokenizer(provider_name, model_name),
                model_max_input_tokens[model_name],
                model_max_batch_tokens[model_name]
            )

            if excluded_indexes:
                if model_name not in model_excluded_ids:
                    model_excluded_ids[model_name] = []
                model_excluded_ids[model_name].extend(
                    part[excluded_index].id.hex
                    for excluded_index in excluded_indexes
                )

            for batch in batches:
                inputs = [
                    (part[entry.input_index], entry.input_text)
                    for entry in batch.entries
                ]

                # Sort the batches by target_rel. This groups embeddings
                # for each table together.
                # This is necessary for `EmbeddingsResult.finalize()`
                inputs.sort(key=lambda e: e[0].target_rel)

                embeddings_params.append(EmbeddingsParams(
                    pgconn=pgconn,
                    provider=provider_cfg,
                    model_name=model_name,
                    inputs=inputs,
                    token_count=batch.token_count,
                    shortening=shortening,
                    user=None,
                    http_client=http_client,
                ))

    return embeddings_params


@dataclass(frozen=True, kw_only=True)
class TextBatchEntry:
    input_index: int
    input_text: str


@dataclass(frozen=True, kw_only=True)
class TextBatch:
    entries: list[TextBatchEntry]
    token_count: int


def batch_texts(
    texts: list[tuple[str, bool]],
    tokenizer: Optional[Tokenizer],
    max_input_tokens: int,
    max_batch_tokens: int,
) -> tuple[list[TextBatch], list[int]]:
    """Given a list of texts and whether each can be truncated, produce a list
    of valid texts to batch.

    Additionally, returns a list of indexes of texts which are too long and
    should be excluded from future embeddings requests.
    """
    excluded_indexes: list[int] = []

    if tokenizer:
        input_indexes: list[int] = []
        input_texts: list[str] = []

        for index, (text, allowed_to_truncate) in enumerate(texts):
            ensured = _ensure_text_token_length(
                text,
                allowed_to_truncate,
                tokenizer,
                max_input_tokens
            )

            if ensured is None:
                # If the text is too long, mark it as excluded and
                # skip.
                excluded_indexes.append(index)
                continue

            input_indexes.append(index)
            input_texts.append(ensured)

        # Group the valid texts into batches based on token count
        batched_inputs = _batch_embeddings_inputs(
            tokenizer, input_texts, max_batch_tokens
        )

        # Gather results
        batches = [
            TextBatch(
                entries=[
                    TextBatchEntry(
                        input_index=input_indexes[index],
                        input_text=input_texts[index],
                    )
                    for index in batch_input_indexes
                ],
                token_count=token_count
            )
            for batch_input_indexes, token_count in batched_inputs
        ]

    else:
        batches = [
            TextBatch(
                entries=[
                    TextBatchEntry(
                        input_index=index,
                        input_text=texts[index][0],
                    )
                    for index in range(len(texts))
                ],
                token_count=0,
            )
        ]

    return (batches, excluded_indexes)


def _ensure_text_token_length(
    text: str,
    allowed_to_truncate: bool,
    tokenizer: Tokenizer,
    max_token_count: int,
) -> Optional[str]:
    """Ensure text does not exceed the allowed token length.

    If the text is ok, return the text unchanged.

    If the text is too long and `allowed_to_truncate` is true, return the
    text truncated.

    Otherwise return None.
    """

    truncate_length = max_token_count - tokenizer.encode_padding()

    if allowed_to_truncate:
        text, current_token_count = (
            tokenizer.shorten_to_token_length(text, truncate_length)
        )

    else:
        current_token_count = len(tokenizer.encode(text))

        if current_token_count > truncate_length:
            return None

    return text


@dataclass(frozen=True, kw_only=True)
class PendingEmbedding:
    id: uuid.UUID
    text: str
    target_rel: str
    target_attr: str
    target_dims_shortening: Optional[int]
    truncate_to_max: bool


async def _get_pending_embeddings(
    pgconn: pgcon.PGConnection,
    model_name: str,
    model_excluded_ids: dict[str, list[str]],
) -> list[PendingEmbedding]:
    task_name = _task_name.get()

    where_clause = ""
    if (
        model_name in model_excluded_ids
        and (excluded_ids := model_excluded_ids[model_name])
    ):
        # Only exclude long text if it won't be auto-truncated.
        logger.debug(
            f"{task_name} skipping {len(excluded_ids)} indexes "
            f"for {model_name!r}"
        )
        where_clause = (f"""
            WHERE
                q."id" not in ({','.join(
                    "'" + excluded_id + "'"
                    for excluded_id in excluded_ids
                )})
                OR q."truncate_to_max"
        """)

    entries = await pgconn.sql_fetch(
        f"""
        SELECT
            *
        FROM
            (
                SELECT
                    "id",
                    "text",
                    "target_rel",
                    "target_attr",
                    "target_dims_shortening",
                    "truncate_to_max"
                FROM
                    edgedbext."ai_pending_embeddings_{model_name}"
                LIMIT
                    500
            ) AS q
        {where_clause}
        ORDER BY
            q."target_dims_shortening"
        """.encode(),
        tx_isolation=edbdef.TxIsolationLevel.RepeatableRead,
    )

    if not entries:
        return []

    result = []
    for entry in entries:
        result.append(PendingEmbedding(
            id=uuidgen.from_bytes(entry[0]),
            text=entry[1].decode("utf-8"),
            target_rel=entry[2].decode(),
            target_attr=entry[3].decode(),
            target_dims_shortening=(
                int.from_bytes(
                    entry[4],
                    byteorder="big",
                    signed=False,
                )
                if entry[4] is not None else
                None
            ),
            truncate_to_max=bool.from_bytes(entry[5]),
        ))

    return result


def _batch_embeddings_inputs(
    tokenizer: Tokenizer,
    inputs: list[str],
    max_batch_tokens: int,
) -> list[tuple[list[int], int]]:
    """Create batches of embeddings inputs.

    Returns batches which are a tuple of:
    - Indexes of input strings grouped to avoid exceeding the max_batch_token
    - The batch's token count
    """

    # Get token counts
    input_token_counts = [
        len(tokenizer.encode(input))
        for input in inputs
    ]

    # Get indexes of inputs, sorted from shortest to longest by token count
    # Use the text itself as a tie breaker for consistency
    unbatched_input_indexes = list(range(len(inputs)))
    unbatched_input_indexes.sort(
        key=lambda index: (input_token_counts[index], inputs[index]),
        reverse=False,
    )

    def unbatched_token_count(unbatched_index: int) -> int:
        return input_token_counts[unbatched_input_indexes[unbatched_index]]

    # Remove any inputs that are larger than the maximum
    while (
        unbatched_input_indexes
        and unbatched_token_count(-1) > max_batch_tokens
    ):
        unbatched_input_indexes.pop()

    batches: list[tuple[list[int], int]] = []
    while unbatched_input_indexes:
        # Start with the largest available input
        batch_input_indexes = [unbatched_input_indexes[-1]]
        batch_token_count = unbatched_token_count(-1)
        unbatched_input_indexes.pop()

        if batch_token_count < max_batch_tokens:
            # Then add the smallest available input as long as long as the
            # max batch token count isn't exceeded
            unbatched_index = 0
            while unbatched_index < len(unbatched_input_indexes):
                if (
                    batch_token_count + unbatched_token_count(unbatched_index)
                    <= max_batch_tokens
                ):
                    batch_input_indexes.append(
                        unbatched_input_indexes[unbatched_index]
                    )
                    batch_token_count += unbatched_token_count(unbatched_index)
                    unbatched_input_indexes.pop(unbatched_index)
                else:
                    unbatched_index += 1

        batches.append((batch_input_indexes, batch_token_count))

    return batches


async def _update_embeddings_in_db(
    pgconn: pgcon.PGConnection,
    provider_cfg: ProviderConfig,
    rel: str,
    attr: str,
    ids: list[uuid.UUID],
    embeddings: bytes,
    offset: int,
) -> int:

    id_array = '{' + ', '.join(f'"{id.hex}"' for id in ids) + '}'
    entries = await pgconn.sql_fetch_val(
        f"""
        WITH upd AS (
            UPDATE {rel} AS target
            SET
                {attr} = (
                    (embeddings.data)::text::edgedb.vector)
            FROM
                (
                    SELECT
                        row_number() over () AS n,
                        j.data
                    FROM
                        (SELECT
                            data
                        FROM
                            json_array_elements($1::json) AS data
                        OFFSET
                            $3::text::int
                        ) AS j
                ) AS embeddings,
                unnest($2::text::text[]) WITH ORDINALITY AS ids(id, n)
            WHERE
                embeddings."n" = ids."n"
                AND target."id" = ids."id"::uuid
            RETURNING
                target."id"
        )
        SELECT count(*)::text FROM upd
        """.encode(),
        args=(
            str(provider_cfg.get_embeddings_from_result(embeddings)).encode(),
            id_array.encode(),
            str(offset).encode(),
        ),
        tx_isolation=edbdef.TxIsolationLevel.RepeatableRead,
    )

    return int(entries.decode())


async def _generate_embeddings(
    provider: ProviderConfig,
    model_name: str,
    inputs: list[str],
    shortening: Optional[int],
    user: Optional[str],
    http_client: http.HttpClient,
) -> EmbeddingsResult:
    task_name = _task_name.get()
    count = len(inputs)
    suf = "s" if count > 1 else ""
    logger.debug(
        f"{task_name} generating embeddings via {model_name!r} "
        f"of {provider.name!r} for {len(inputs)} object{suf}"
    )

    if provider.api_style == ApiStyle.OpenAI:
        result = await _generate_openai_embeddings(
            provider, model_name, inputs, shortening, user, http_client
        )
    elif provider.api_style == ApiStyle.VoyageAI:
        return await _generate_voyageai_embeddings(
            provider, model_name, inputs, http_client
    elif provider.api_style == ApiStyle.Ollama:
        result = await _generate_ollama_embeddings(
            provider, model_name, inputs, shortening, http_client
        )
    else:
        raise RuntimeError(
            f"unsupported model provider API style: {provider.api_style}, "
            f"provider: {provider.name}"
        )

    result.provider_cfg = provider
    return result


async def _generate_openai_embeddings(
    provider: ProviderConfig,
    model_name: str,
    inputs: list[str],
    shortening: Optional[int],
    user: Optional[str],
    http_client: http.HttpClient,
) -> EmbeddingsResult:

    headers = {
        "Authorization": f"Bearer {provider.secret}",
    }
    if provider.name == "builtin::openai" and provider.client_id:
        headers["OpenAI-Organization"] = provider.client_id
    client = http_client.with_context(
        headers=headers,
        base_url=provider.api_url,
    )

    params: dict[str, Any] = {
        "model": model_name,
        "encoding_format": "float",
        "input": inputs,
    }
    if shortening is not None:
        params["dimensions"] = shortening

    if user is not None:
        params["user"] = user

    result = await client.post(
        "/embeddings",
        json=params,
    )

    error = None
    if result.status_code >= 400:
        error = rs.Error(
            message=(
                f"API call to generate embeddings failed with status "
                f"{result.status_code}: {result.text}"
            ),
            retry=(
                # If the request fails with 429 - too many requests, it can be
                # retried
                result.status_code == 429
            ),
        )

    return EmbeddingsResult(
        data=(error if error else EmbeddingsData(result.bytes())),
        limits=_read_openai_limits(result),
    )


async def _generate_voyageai_embeddings(
    provider: ProviderConfig,
    model_name: str,
    inputs: list[str],
    http_client: http.HttpClient,
) -> EmbeddingsResult:

    headers = {
        "Authorization": f"Bearer {provider.secret}",
    }
    client = http_client.with_context(
        headers=headers,
        base_url=provider.api_url,
    )

    params: dict[str, Any] = {
        "input": inputs,
        "model": model_name,
        "output_format": "float",
        "truncation": False
    }

    result = await client.post(
        "/embeddings",
        json=params,
    )

    error = None
    if result.status_code >= 400:
        error = rs.Error(
            message=(
                f"API call to generate embeddings failed with status "
                f"{result.status_code}: {result.text}"
            ),
            retry=(
                # If the request fails with 429 - too many requests, it can be
                # retried
                result.status_code == 429
            ),
        )

    return EmbeddingsResult(
        data=(error if error else EmbeddingsData(result.bytes())),
    )


def _read_openai_header_field(
    result: Any,
    field_names: list[str],
) -> Optional[int]:
    # Return the value of the first requested field available
    try:
        for field_name in field_names:
            if field_name in result.headers:
                header_value = result.headers[field_name]
                return int(header_value) if header_value is not None else None

    except (ValueError, TypeError):
        pass

    return None


def _read_openai_limits(
    result: Any,
) -> dict[str, rs.Limits]:
    request_limit = _read_openai_header_field(
        result,
        [
            'x-ratelimit-limit-project-requests',
            'x-ratelimit-limit-requests',
        ],
    )
    request_remaining = _read_openai_header_field(
        result,
        [
            'x-ratelimit-remaining-project-requests',
            'x-ratelimit-remaining-requests',
        ],
    )

    token_limit = _read_openai_header_field(
        result,
        [
            'x-ratelimit-limit-project-tokens',
            'x-ratelimit-limit-tokens',
        ],
    )

    token_remaining = _read_openai_header_field(
        result,
        [
            'x-ratelimit-remaining-project-tokens',
            'x-ratelimit-remaining-tokens',
        ],
    )

    return {
        'requests': rs.Limits(
            total=request_limit,
            remaining=request_remaining,
        ),
        'tokens': rs.Limits(
            total=token_limit,
            remaining=token_remaining,
        ),
    }


async def _generate_ollama_embeddings(
    provider: ProviderConfig,
    model_name: str,
    inputs: list[str],
    shortening: Optional[int],
    http_client: http.HttpClient,
) -> EmbeddingsResult:

    headers: dict[str, str] = {}
    client = http_client.with_context(
        headers=headers,
        base_url=provider.api_url,
    )

    params: dict[str, Any] = {
        "model": model_name,
        "input": inputs,
    }
    if shortening is not None:
        params["dimensions"] = shortening

    result = await client.post(
        "/embed",
        json=params,
    )

    error = None
    if result.status_code >= 400:
        error = rs.Error(
            message=(
                f"API call to generate embeddings failed with status "
                f"{result.status_code}: {result.text}"
            ),
            retry=(
                # If the request fails with 429 - too many requests, it can be
                # retried
                result.status_code == 429
            ),
        )

    return EmbeddingsResult(
        data=(error if error else EmbeddingsData(result.bytes())),
        limits=_ollama_limits(),
    )


def _ollama_limits() -> dict[str, rs.Limits]:
    return {
        'requests': rs.Limits(
            total='unlimited',
            remaining=None,
        ),
        'tokens': rs.Limits(
            total='unlimited',
            remaining=None,
        ),
    }


async def _start_chat(
    *,
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    provider: ProviderConfig,
    http_client: http.HttpClient,
    model_name: str,
    messages: list[dict[str, Any]],
    stream: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    max_tokens: Optional[int],
    seed: Optional[int],
    safe_prompt: Optional[bool],
    top_k: Optional[int],
    logit_bias: Optional[dict[int, int]],
    logprobs: Optional[bool],
    user: Optional[str],
    tools: Optional[list[dict[str, Any]]],
) -> None:
    if provider.api_style == ApiStyle.OpenAI:
        await _start_openai_chat(
            protocol=protocol,
            request=request,
            response=response,
            provider=provider,
            http_client=http_client,
            model_name=model_name,
            messages=messages,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            seed=seed,
            safe_prompt=safe_prompt,
            logit_bias=logit_bias,
            logprobs=logprobs,
            user=user,
            tools=tools,
        )
    elif provider.api_style == ApiStyle.Anthropic:
        await _start_anthropic_chat(
            protocol=protocol,
            request=request,
            response=response,
            provider=provider,
            http_client=http_client,
            model_name=model_name,
            messages=messages,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            tools=tools,
            max_tokens=max_tokens,
        )
    elif provider.api_style == ApiStyle.Ollama:
        await _start_ollama_chat(
            protocol=protocol,
            request=request,
            response=response,
            provider=provider,
            http_client=http_client,
            model_name=model_name,
            messages=messages,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            tools=tools,
        )
    else:
        raise RuntimeError(
            f"unsupported model provider API style: {provider.api_style}, "
            f"provider: {provider.name}"
        )


@contextlib.asynccontextmanager
async def aconnect_sse(
    client: http.HttpClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> AsyncIterator[http.ResponseSSE]:
    headers = kwargs.pop("headers", {})
    headers["Accept"] = "text/event-stream"
    headers["Cache-Control"] = "no-store"

    stm = await client.stream_sse(
        method=method,
        path=url,
        headers=headers,
        **kwargs
    )
    if isinstance(stm, http.Response):
        raise AIProviderError(
            f"API call to generate chat completions failed with status "
            f"{stm.status_code}: {stm.text}"
        )
    async with stm as response:
        if response.status_code >= 400:
            # Unlikely that we have a streaming response with a non-200 result
            raise AIProviderError(
                f"API call to generate chat completions failed with status "
                f"{response.status_code}"
            )
        yield response


async def _start_openai_like_chat(
    *,
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    provider_name: str,
    client: http.HttpClient,
    model_name: str,
    messages: list[dict[str, Any]],
    stream: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    max_tokens: Optional[int],
    seed: Optional[int],
    safe_prompt: Optional[bool],
    logit_bias: Optional[dict[int, int]],
    logprobs: Optional[bool],
    user: Optional[str],
    tools: Optional[list[dict[str, Any]]],
) -> None:
    isOpenAI = provider_name == "builtin::openai"

    params: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    if tools is not None:
        params["tools"] = tools
    if isOpenAI and logit_bias is not None:
        params["logit_bias"] = logit_bias
    if isOpenAI and logprobs is not None:
        params["logprobs"] = logprobs
    if isOpenAI and user is not None:
        params["user"] = user
    if not isOpenAI and safe_prompt is not None:
        params["safe_prompt"] = safe_prompt
    if max_tokens is not None:
        if isOpenAI:
            params["max_completion_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens
    if seed is not None:
        if isOpenAI:
            params["seed"] = seed
        else:
            params["random_seed"] = seed

    if stream:
        async with aconnect_sse(
            client,
            method="POST",
            url="/chat/completions",
            json={
                **params,
                "stream": True,
            }
        ) as event_source:
            # we need tool_index and finish_reason to correctly
            # send 'content_block_stop' chunk for tool call messages
            tool_index = 0
            finish_reason = "unknown"

            async for sse in event_source:
                if not response.sent:
                    response.status = http.HTTPStatus.OK
                    response.content_type = b'text/event-stream'
                    response.close_connection = False
                    response.custom_headers["Cache-Control"] = "no-cache"
                    protocol.write(request, response)

                if sse.event != "message":
                    continue

                if sse.data == "[DONE]":
                    # mistral doesn't send finish_reason for tool calls
                    if finish_reason == "unknown":
                        event = (
                            b'event: content_block_stop\n'
                            + b'data: {"type": "content_block_stop",'
                            + b'"index": ' + str(tool_index).encode() + b'}\n\n'
                        )
                        protocol.write_raw(event)
                    event = (
                        b'event: message_stop\n'
                        + b'data: {"type": "message_stop"}\n\n'
                    )
                    protocol.write_raw(event)
                    break

                message = sse.json()
                if message.get("object") == "chat.completion.chunk":
                    data = message.get("choices")[0]
                    delta = data.get("delta")
                    role = delta.get("role")
                    tool_calls = delta.get("tool_calls")

                    if role:
                        event_data = json.dumps({
                            "type": "message_start",
                            "message": {
                                "id": message["id"],
                                "role": role,
                                "model": message["model"],
                                "usage": message.get("usage")
                            },
                        }).encode("utf-8")
                        event = (
                            b'event: message_start\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

                        event_data = json.dumps({
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "text",
                                "text": ""
                            }
                        }).encode("utf-8")
                        event = (
                            b'event: content_block_start\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

                        # if there's only one openai tool call it shows up here
                        if tool_calls:
                            for tool_call in tool_calls:
                                tool_index = tool_call["index"]
                                event_data = json.dumps({
                                    "type": "content_block_start",
                                    "index": tool_call["index"] + 1,
                                    "content_block": {
                                        "id": tool_call["id"],
                                        "type": "tool_use",
                                        "name": tool_call["function"]["name"],
                                        "args":
                                        tool_call["function"]["arguments"],
                                    },
                                }).encode("utf-8")

                                event = (
                                    b'event: content_block_start\n'
                                    + b'data:' + event_data + b'\n\n'
                                )
                                protocol.write_raw(event)
                    # if there are few openai tool calls, they show up here
                    # mistral tool calls always show up here
                    elif tool_calls:
                        # OpenAI provides index, Mistral doesn't
                        for index, tool_call in enumerate(tool_calls):
                            currentIndex = tool_call.get("index") or index
                            if tool_call.get("type") == "function" or \
                            "id" in tool_call:
                                if currentIndex > 0:
                                    tool_index = currentIndex
                                    # send the stop chunk for the previous tool
                                    event = (
                                        b'event: content_block_stop\n'
                                        + b'data: { \
                                        "type": "content_block_stop",'
                                        + b'"index": '
                                        + str(currentIndex).encode()
                                        + b'}\n\n'
                                    )
                                    protocol.write_raw(event)

                                event_data = json.dumps({
                                    "type": "content_block_start",
                                    "index": currentIndex + 1,
                                    "content_block": {
                                        "id": tool_call.get("id"),
                                        "type": "tool_use",
                                        "name": tool_call["function"]["name"],
                                        "args":
                                        tool_call["function"]["arguments"],
                                    },
                                }).encode("utf-8")

                                event = (
                                    b'event: content_block_start\n'
                                    + b'data:' + event_data + b'\n\n'
                                )
                                protocol.write_raw(event)
                            else:
                                event_data = json.dumps({
                                        "type": "content_block_delta",
                                        "index": currentIndex + 1,
                                        "delta": {
                                            "type": "tool_call_delta",
                                            "args":
                                             tool_call["function"]["arguments"],
                                        },
                                    }).encode("utf-8")
                                event = (
                                    b'event: content_block_delta\n'
                                    + b'data:' + event_data + b'\n\n'
                                )
                                protocol.write_raw(event)
                    elif finish_reason := data.get("finish_reason"):
                        index = (
                            tool_index + 1
                            if finish_reason == "tool_calls"
                            else 0
                        )
                        event = (
                            b'event: content_block_stop\n'
                            + b'data: {"type": "content_block_stop",'
                            + b'"index": ' + str(index).encode() + b'}\n\n'
                        )
                        protocol.write_raw(event)

                        event_data = json.dumps({
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": finish_reason,
                            },
                            "usage": message.get("usage")
                        }).encode("utf-8")
                        event = (
                            b'event: message_delta\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

                    else:
                        event_data = json.dumps({
                            "type": "content_block_delta",
                            "index": 0,
                             "delta": {
                                "type": "text_delta",
                                "text": delta.get("content"),
                            },
                            "logprobs": data.get("logprobs"),
                        }).encode("utf-8")

                        event = (
                            b'event: content_block_delta\n'
                            + b'data:' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

            protocol.close()
    else:
        result = await client.post(
            "/chat/completions",
            json={
                **params
            }
        )

        if result.status_code >= 400:
            raise AIProviderError(
                f"API call to generate chat completions failed with status "
                f"{result.status_code}: {result.text}"
            )

        response.status = http.HTTPStatus.OK

        result_data = result.json()
        choice = result_data["choices"][0]
        tool_calls = choice["message"].get("tool_calls")
        tool_calls_formatted = [
            {
                "id": tool_call["id"],
                "type": tool_call["type"],
                "name": tool_call["function"]["name"],
                "args": json.loads(tool_call["function"]["arguments"]),
            }
            for tool_call in tool_calls or []
        ]

        body = {
            "id": result_data["id"],
            "model": result_data["model"],
            "text": choice["message"]["content"],
            "finish_reason": choice.get("finish_reason"),
            "usage": result_data.get("usage"),
            "logprobs": choice.get("logprobs"),
            "tool_calls": tool_calls_formatted,
        }
        response.content_type = b'application/json'
        response.body = json.dumps(body).encode("utf-8")


async def _start_openai_chat(
    *,
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    provider: ProviderConfig,
    http_client: http.HttpClient,
    model_name: str,
    messages: list[dict[str, Any]],
    stream: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    max_tokens: Optional[int],
    seed: Optional[int],
    safe_prompt: Optional[bool],
    logit_bias: Optional[dict[int, int]],
    logprobs: Optional[bool],
    user: Optional[str],
    tools: Optional[list[dict[str, Any]]],
) -> None:
    headers = {
        "Authorization": f"Bearer {provider.secret}",
    }

    if provider.name == "builtin::openai" and provider.client_id:
        headers["OpenAI-Organization"] = provider.client_id

    client = http_client.with_context(
        base_url=provider.api_url,
        headers=headers,
    )

    await _start_openai_like_chat(
        protocol=protocol,
        request=request,
        response=response,
        provider_name=provider.name,
        client=client,
        model_name=model_name,
        messages=messages,
        stream=stream,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        seed=seed,
        safe_prompt=safe_prompt,
        logit_bias=logit_bias,
        logprobs=logprobs,
        user=user,
        tools=tools,
    )


# Anthropic differs from OpenAI and Mistral as there's no tool chunk:
# tool_call(tool_use) is part of the assistant chunk, and
# tool_result is part of the user chunk.
async def _start_anthropic_chat(
    *,
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    provider: ProviderConfig,
    http_client: http.HttpClient,
    model_name: str,
    messages: list[dict[str, Any]],
    stream: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    tools: Optional[list[dict[str, Any]]],
    max_tokens: Optional[int],
) -> None:
    headers = {
        "x-api-key": f"{provider.secret}",
    }

    if provider.name == "builtin::anthropic":
        headers["anthropic-version"] = "2023-06-01"
        headers["anthropic-beta"] = "messages-2023-12-15"

    client = http_client.with_context(
        headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "messages-2023-12-15",
            "x-api-key": f"{provider.secret}",
        },
        base_url=provider.api_url,
    )

    anthropic_messages = []
    system_prompt_parts = []

    for message in messages:
        if message["role"] == "system":
            system_prompt_parts.append(message["content"])

        elif message["role"] == "assistant" and "tool_calls" in message:
            # Anthropic fails when an assistant chunk has multiple tool calls
            # and is followed by several tool_result chunks (or a user chunk
            # with multiple tool_results). Each assistant chunk should have
            # only 1 tool_use, followed by 1 tool_result chunk.
            for tool_call in message["tool_calls"]:
                msg = {
                    "role": "assistant",
                    "content": [
                        {
                            "id": tool_call["id"],
                            "type": "tool_use",
                            "name": tool_call["function"]["name"],
                            "input": json.loads(
                                tool_call["function"]["arguments"]),
                        }
                    ],
                }
                anthropic_messages.append(msg)
        # Check if message is a tool result
        elif message["role"] == "tool":
            tool_result = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"]
                    }
                ],
            }
            anthropic_messages.append(tool_result)
        else:
            anthropic_messages.append(message)

    system_prompt = "\n".join(system_prompt_parts)

    # Each tool_use chunk must be followed by a matching tool_result chunk
    reordered_messages = []

    # Separate tool_result messages by tool_use_id for faster access
    tool_result_map = {
        item["content"][0]["tool_use_id"]: item
        for item in anthropic_messages
        if item["role"] == "user" and isinstance(item["content"][0], dict)
          and item["content"][0]["type"] == "tool_result"
    }

    for message in anthropic_messages:
        if message["role"] == "assistant":
            reordered_messages.append(message)
            if isinstance(message["content"], list):
                for item in message["content"]:
                    if item["type"] == "tool_use":
                        # find the matching user tool_result message
                        tool_use_id = item["id"]
                        if tool_use_id in tool_result_map:
                            reordered_messages.append(
                                tool_result_map[tool_use_id])
        # append user message that is not tool_result
        elif not (message["role"] == "user"
                  and isinstance(message["content"][0], dict)
                  and message["content"][0]["type"] == "tool_result"):
            reordered_messages.append(message)

    params = {
        "model": model_name,
        "messages": reordered_messages,
        "system": system_prompt,
        **({"temperature": temperature} if temperature is not None else {}),
        **({"top_p": top_p} if top_p is not None else {}),
        **{"max_tokens": max_tokens if max_tokens is not None else 4096},
        **({"top_k": top_k} if top_k is not None else {}),
        **({"tools": tools} if tools is not None else {}),
    }

    if stream:
        async with aconnect_sse(
            client,
            method="POST",
            url="/messages",
            json={
                **params,
                "stream": True,
            }
        ) as event_source:
            tool_index = 0
            async for sse in event_source:
                if not response.sent:
                    response.status = http.HTTPStatus.OK
                    response.content_type = b'text/event-stream'
                    response.close_connection = False
                    response.custom_headers["Cache-Control"] = "no-cache"
                    protocol.write(request, response)

                if sse.event == "message_start":
                    message = sse.json()["message"]
                    for k in tuple(message):
                        if k not in {"id", "type", "role", "model", "usage"}:
                            del message[k]
                    message["usage"] = {
                        "prompt_tokens": message["usage"]["input_tokens"],
                        "completion_tokens": message["usage"]["output_tokens"]
                    }
                    message_data = json.dumps(message).encode("utf-8")
                    event = (
                        b'event: message_start\n'
                        + b'data: {"type": "message_start",'
                        + b'"message":' + message_data + b'}\n\n'
                    )
                    protocol.write_raw(event)

                elif sse.event == "content_block_start":
                    sse_data = json.loads(sse.data)
                    protocol.write_raw(
                        b'event: content_block_start\n'
                        + b'data: ' + json.dumps(sse_data).encode("utf-8")
                        + b'\n\n'
                    )
                    # we don't send content_block_stop event when text
                    # chunk ends, should be okay since we don't consume
                    # this event on the client side
                    data = sse.json()
                    if (
                        "content_block" in data
                        and data["content_block"].get("type") == "tool_use"
                    ):
                        currentIndex = data["index"]
                        if currentIndex > 0:
                            tool_index = currentIndex
                            event_data = json.dumps({
                                "type": "content_block_stop",
                                "index": currentIndex - 1})
                            protocol.write_raw(
                                b'event: content_block_stop\n'
                                + b'data: ' + event_data.encode("utf-8")
                                + b'\n\n'
                            )
                elif sse.event == "content_block_delta":
                    message = sse.json()
                    # it is always dict irl but test is failing
                    delta = message.get("delta")
                    if delta and delta.get("type") == "input_json_delta":
                        delta["type"] = "tool_call_delta"

                    if delta and "partial_json" in delta:
                        delta["args"] = delta.pop("partial_json")

                    event_data = json.dumps(message)
                    event = (
                        b'event: content_block_delta\n'
                        + b'data: ' + event_data.encode("utf-8") + b'\n\n'
                    )
                    protocol.write_raw(event)
                elif sse.event == "message_delta":
                    message = sse.json()
                    if message["delta"]["stop_reason"] == "tool_use":
                        event = (
                            b'event: content_block_stop\n'
                            + b'data: {"type": "content_block_stop",'
                            + b'"index": '
                            + str(tool_index).encode("utf-8")
                            + b'}\n\n'
                        )
                        protocol.write_raw(event)

                    event_data = json.dumps({
                            "type": "message_delta",
                            "delta": message["delta"],
                            "usage": {"completion_tokens":
                                      message["usage"]["output_tokens"]}
                    })
                    event = (
                            b'event: message_delta\n'
                            + b'data: ' + event_data.encode("utf-8") + b'\n\n'
                        )

                    protocol.write_raw(event)
                elif sse.event == "message_stop":
                    event = (
                        b'event: message_stop\n'
                        + b'data: {"type": "message_stop"}\n\n'
                    )
                    protocol.write_raw(event)
                    # needed because stream doesn't close itself
                    protocol.close()
            protocol.close()
    else:
        result = await client.post(
            "/messages",
            json={
                **params
            }
        )
        if result.status_code >= 400:
            raise AIProviderError(
                f"API call to generate chat completions failed with status "
                f"{result.status_code}: {result.text}"
            )

        response.status = http.HTTPStatus.OK
        response.content_type = b'application/json'

        result_data = result.json()
        tool_calls = [
            item
            for item in result_data["content"]
            if item.get("type") == "tool_use"
        ]
        tool_calls_formatted = [
            {
                "id": tool_call["id"],
                "type": "function",
                "name": tool_call["name"],
                "args": tool_call["input"],
            }
            for tool_call in tool_calls
        ]

        body = {
            "id": result_data["id"],
            "model": result_data["model"],
            "text": next((item["text"]
                          for item in result_data["content"]
                          if item.get("type") == "text"), ""),
            "finish_reason": result_data["stop_reason"],
             "usage": {
                "prompt_tokens": result_data["usage"]["input_tokens"],
                "completion_tokens": result_data["usage"]["output_tokens"]
            },
            "tool_calls": tool_calls_formatted,
        }

        response.body = json.dumps(
            body
        ).encode("utf-8")


async def _start_ollama_chat(
    *,
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    provider: ProviderConfig,
    http_client: http.HttpClient,
    model_name: str,
    messages: list[dict[str, Any]],
    stream: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    tools: Optional[list[dict[str, Any]]],
) -> None:

    # The default API doesn't produce a SSE stream. Use the experimental
    # OpenAI-like API if we need a stream.
    base_url = provider.api_url
    if stream:
        base_url = '/'.join(
            base_url.split('/')[:-1] + ['v1']
        )

    # Generate params differently for stream and non-stream modes since they
    # use different APIs.
    options = {
        **({"temperature": temperature} if temperature is not None else {}),
        **({"top_p": top_p} if top_p is not None else {}),
        **({"top_k": top_k} if top_k is not None else {}),
    }

    if stream:
        params = {
            "model": model_name,
            "messages": messages,
            "options": options,
        }

        # Only include tools in streaming params if no tool messages are
        # provided.
        if tools is not None and not any(
            message["role"] == "tool"
            for message in messages
        ):
            params["tools"] = tools

    else:
        converted_messages = []
        for message in messages:
            if message["role"] == "user":
                # Ollama can't handle content block messages.
                # Unpack them into separate messages.
                if isinstance(message["content"], str):
                    converted_messages.append(message)
                else:
                    # array of content blocks
                    for block in message["content"]:
                        if block["type"] != "text":
                            raise TypeError(
                                f"Unsupported content type: '{block["type"]}'. "
                                f"For non-text content, use streamed mode."
                            )
                        converted_messages.append({
                            "role": message["role"],
                            "content": block["text"],
                        })

            elif message["role"] == "assistant":
                # Gel http API packs arguments into a string, but ollama
                # requires plain json.
                converted_messages.append({
                    "role": message["role"],
                    "content": message["content"],
                    "tool_calls": [
                        {
                            "id": tool_call["id"],
                            "function": {
                                "name": tool_call["function"]["name"],
                                "arguments": json.loads(
                                    tool_call["function"]["arguments"]
                                ),
                            }
                        }
                        for tool_call in message["tool_calls"]
                    ]
                })

            else:
                converted_messages.append(message)

        params = {
            "model": model_name,
            "messages": converted_messages,
            "options": options,
            **({"tools": tools} if tools is not None else {}),
        }

    client = http_client.with_context(
        headers={},
        base_url=base_url,
    )

    if stream:
        async with aconnect_sse(
            client,
            method="POST",
            url="/chat/completions",
            json={
                **params,
                "stream": True,
            }
        ) as event_source:
            # we need tool_index and finish_reason to correctly
            # send 'content_block_stop' chunk for tool call messages
            tool_index = 0
            finish_reason: Optional[str] = "unknown"

            started = False

            async for sse in event_source:
                if not response.sent:
                    response.status = http.HTTPStatus.OK
                    response.content_type = b'text/event-stream'
                    response.close_connection = False
                    response.custom_headers["Cache-Control"] = "no-cache"
                    protocol.write(request, response)

                if sse.event != "message":
                    continue

                if sse.data == "[DONE]":
                    # mistral doesn't send finish_reason for tool calls
                    if finish_reason == "unknown":
                        event = (
                            b'event: content_block_stop\n'
                            + b'data: {"type": "content_block_stop",'
                            + b'"index": ' + str(tool_index).encode() + b'}\n\n'
                        )
                        protocol.write_raw(event)
                    event = (
                        b'event: message_stop\n'
                        + b'data: {"type": "message_stop"}\n\n'
                    )
                    protocol.write_raw(event)
                    break

                message = sse.json()
                if message.get("object") == "chat.completion.chunk":
                    choices = message.get("choices")
                    data = choices[0] if choices else {}
                    delta = data.get("delta") or {}
                    role = delta.get("role")
                    tool_calls = delta.get("tool_calls")

                    # Unlike OpenAI, Ollama includes the role in every event.
                    # Just create a new start event.
                    if not started:
                        event_data = json.dumps({
                            "type": "message_start",
                            "message": {
                                "id": message["id"],
                                "role": role,
                                "model": message["model"],
                                "usage": message.get("usage")
                            },
                        }).encode("utf-8")
                        event = (
                            b'event: message_start\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

                        event_data = json.dumps({
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "text",
                                "text": ""
                            }
                        }).encode("utf-8")
                        event = (
                            b'event: content_block_start\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)
                        started = True

                    if tool_calls:
                        for index, tool_call in enumerate(tool_calls):
                            currentIndex = tool_call.get("index") or index
                            if tool_call.get("type") == "function" or \
                            "id" in tool_call:
                                if currentIndex > 0:
                                    tool_index = currentIndex
                                    # send the stop chunk for the previous tool
                                    event = (
                                        b'event: content_block_stop\n'
                                        + b'data: { \
                                        "type": "content_block_stop",'
                                        + b'"index": '
                                        + str(currentIndex).encode()
                                        + b'}\n\n'
                                    )
                                    protocol.write_raw(event)

                                event_data = json.dumps({
                                    "type": "content_block_start",
                                    "index": currentIndex + 1,
                                    "content_block": {
                                        "id": tool_call.get("id"),
                                        "type": "tool_use",
                                        "name": tool_call["function"]["name"],
                                        "args":
                                        tool_call["function"]["arguments"],
                                    },
                                }).encode("utf-8")

                                event = (
                                    b'event: content_block_start\n'
                                    + b'data:' + event_data + b'\n\n'
                                )
                                protocol.write_raw(event)
                            else:
                                event_data = json.dumps({
                                        "type": "content_block_delta",
                                        "index": currentIndex + 1,
                                        "delta": {
                                            "type": "tool_call_delta",
                                            "args":
                                             tool_call["function"]["arguments"],
                                        },
                                    }).encode("utf-8")
                                event = (
                                    b'event: content_block_delta\n'
                                    + b'data:' + event_data + b'\n\n'
                                )
                                protocol.write_raw(event)
                    elif finish_reason := data.get("finish_reason"):
                        index = (
                            tool_index + 1
                            if finish_reason == "tool_calls"
                            else 0
                        )
                        event = (
                            b'event: content_block_stop\n'
                            + b'data: {"type": "content_block_stop",'
                            + b'"index": ' + str(index).encode() + b'}\n\n'
                        )
                        protocol.write_raw(event)

                        event_data = json.dumps({
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": finish_reason,
                            },
                            "usage": message.get("usage")
                        }).encode("utf-8")
                        event = (
                            b'event: message_delta\n'
                            + b'data: ' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

                    else:
                        event_data = json.dumps({
                            "type": "content_block_delta",
                            "index": 0,
                             "delta": {
                                "type": "text_delta",
                                "text": delta.get("content"),
                            },
                            "logprobs": data.get("logprobs"),
                        }).encode("utf-8")

                        event = (
                            b'event: content_block_delta\n'
                            + b'data:' + event_data + b'\n\n'
                        )
                        protocol.write_raw(event)

            protocol.close()
    else:
        result = await client.post(
            "/chat",
            json={
                **params,
                "stream": False
            }
        )
        if result.status_code >= 400:
            raise AIProviderError(
                f"API call to generate chat completions failed with status "
                f"{result.status_code}: {result.text}"
            )

        response.status = http.HTTPStatus.OK
        response.content_type = b'application/json'

        result_data = result.json()

        tool_calls = result_data["message"].get("tool_calls")
        tool_calls_formatted = [
            {
                # Ollama does not provide tool call ids for non-stream.
                # Use enumerate to generate a placeholder.
                "id": f"call_{tool_call_id}",
                # Ollama doesn't provide the tool call type but it should
                # always be 'function'.
                "type": "function",
                "name": tool_call["function"]["name"],
                "args": tool_call["function"]["arguments"],
            }
            for tool_call_id, tool_call in enumerate(tool_calls or [])
        ]

        body = {
            "model": result_data["model"],
            "text": result_data["message"]["content"],
            "finish_reason": result_data["done_reason"],
            "usage": {
                "prompt_tokens": result_data["prompt_eval_count"],
                "completion_tokens": result_data["eval_count"]
            },
            "tool_calls": tool_calls_formatted,
        }

        # Ollama has no documented tools response

        response.body = json.dumps(
            body
        ).encode("utf-8")


#
# HTTP API
#

async def handle_request(
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    db: dbview.Database,
    role_name: str,
    args: list[str],
    tenant: srv_tenant.Tenant,
) -> None:
    if len(args) != 1 or args[0] not in {"rag", "embeddings"}:
        response.body = b'Unknown path'
        response.status = http.HTTPStatus.NOT_FOUND
        response.close_connection = True
        return
    if request.method != b"POST":
        response.body = b"Invalid request method"
        response.status = http.HTTPStatus.METHOD_NOT_ALLOWED
        response.close_connection = True
        return
    if request.content_type != b"application/json":
        response.body = b"Expected application/json input"
        response.status = http.HTTPStatus.BAD_REQUEST
        response.close_connection = True
        return

    await db.introspection()

    try:
        if args[0] == "rag":
            await _handle_rag_request(
                protocol, request, response, db, role_name, tenant
            )
        elif args[0] == "embeddings":
            await _handle_embeddings_request(
                request, response, db, role_name, tenant
            )
        else:
            response.body = b'Unknown path'
            response.status = http.HTTPStatus.NOT_FOUND
            response.close_connection = True
            return
    except Exception as ex:
        if not isinstance(ex, AIExtError):
            ex = InternalError(str(ex))

        if not isinstance(ex, BadRequestError):
            logger.error(f"error while handling a /{args[0]} request: {ex}")

        response.status = ex.get_http_status()
        response.content_type = b'application/json'
        response.body = json.dumps(ex.json()).encode("utf-8")
        response.close_connection = True
        return


async def _handle_rag_request(
    protocol: protocol.HttpProtocol,
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    db: dbview.Database,
    role_name: str,
    tenant: srv_tenant.Tenant,
) -> None:
    try:
        http_client = tenant.get_http_client(originator="ai/rag")

        body = json.loads(request.body)

        if not isinstance(body, dict):
            raise TypeError(
                'the body of the request must be a JSON object')

        context = body.get('context')
        if context is None:
            raise TypeError(
                'missing required "context" object in request')
        if not isinstance(context, dict):
            raise TypeError(
                '"context" value in request is not a valid JSON object')

        ctx_query = context.get("query")
        ctx_variables = context.get("variables")
        ctx_globals = context.get("globals")
        ctx_max_obj_count = context.get("max_object_count")

        if not ctx_query:
            raise TypeError(
                'missing required "query" in request "context" object')

        if ctx_variables is not None and not isinstance(ctx_variables, dict):
            raise TypeError('"variables" must be a JSON object')

        if ctx_globals is not None and not isinstance(ctx_globals, dict):
            raise TypeError('"globals" must be a JSON object')

        model = cast(str, body.get('model'))
        if not model:
            raise TypeError(
                'missing required "model" in request')

        query = body.get('query')
        if not query:
            raise TypeError(
                'missing required "query" in request')

        stream = body.get('stream')
        if stream is None:
            stream = False
        elif not isinstance(stream, bool):
            raise TypeError('"stream" must be a boolean')

        if ctx_max_obj_count is None:
            ctx_max_obj_count = 5
        elif not isinstance(ctx_max_obj_count, int) or ctx_max_obj_count <= 0:
            raise TypeError(
                '"context.max_object_count" must be a positive integer')

        prompt_id = None
        prompt_name = None
        custom_prompt = None
        custom_prompt_messages: list[dict[str, Any]] = []

        prompt = body.get("prompt")

        if prompt is None:
            prompt_name = "builtin::rag-default"
        else:
            if not isinstance(prompt, dict):
                raise TypeError(
                    '"prompt" value in request must be a JSON object')
            prompt_name = prompt.get("name")
            prompt_id = prompt.get("id")
            custom_prompt = prompt.get("custom")

            if prompt_name and prompt_id:
                raise TypeError(
                    "prompt.id and prompt.name are mutually exclusive"
                )

            if custom_prompt:
                if not isinstance(custom_prompt, list):
                    raise TypeError(
                        (
                            "prompt.custom must be a list, where each element "
                            "is one of the following types:\n"
                            "{ role: 'system', content: str },\n"
                            "{ role: 'user', content: [{ type: 'text', "
                            "text: str }] },\n"
                            "{ role: 'assistant', content: str, "
                            "optional tool_calls: [{id: str, type: 'function',"
                            " function: { name: str, arguments: str }}] },\n"
                            "{ role: 'tool', content: str, tool_call_id: str }"
                        )
                    )
                for entry in custom_prompt:
                    if not isinstance(entry, dict) or not entry.get("role"):
                        raise TypeError(
                            (
                                "each prompt.custom entry must be a "
                                "dictionary of one of the following types:\n"
                                "{ role: 'system', content: str },\n"
                                "{ role: 'user', content: [{ type: 'text', "
                                "text: str }] },\n"
                                "{ role: 'assistant', content: str, "
                                "optional tool_calls: [{id: str, "
                                "type: 'function', function: { "
                                "name: str, arguments: str }}] },\n"
                                "{ role: 'tool', content: str, "
                                "tool_call_id: str }"
                            )
                        )

                    entry_role = entry.get('role')
                    if entry_role == 'system':
                        if not isinstance(entry.get("content"), str):
                            raise TypeError(
                                "System message content has to be string."
                            )
                    elif entry_role == 'user':
                        if not isinstance(entry.get("content"), list):
                            raise TypeError(
                                (
                                    "User message content has to be a list of "
                                    "{ type: 'text', text: str }"
                                )
                            )
                        for content_entry in entry["content"]:
                            if content_entry.get(
                                "type"
                            ) != "text" or not isinstance(
                                content_entry.get("text"), str
                            ):
                                raise TypeError(
                                    (
                                        "Element of user message content has to"
                                        "be of type { type: 'text', text: str }"
                                    )
                                )
                    elif entry_role == 'assistant':
                        if not isinstance(entry.get("content"), str):
                            raise TypeError(
                                "Assistant message content has to be string"
                            )

                        tool_calls = entry.get("tool_calls")
                        if tool_calls:
                            if not isinstance(tool_calls, list):
                                raise TypeError(
                                    (
                                        "Assistant tool calls must be"
                                        "a list of:\n"
                                        "{id: str, type: 'function', function:"
                                        " {name: str, arguments: str }}"
                                    )
                                )

                            for call in tool_calls:
                                if (
                                    not isinstance(call, dict)
                                    or not isinstance(call.get("id"), str)
                                    or call.get("type") != "function"
                                    or not isinstance(
                                        call.get("function"), dict
                                    )
                                    or not isinstance(
                                        call["function"].get("name"), str
                                    )
                                    or not isinstance(
                                        call["function"].get("arguments"),
                                        str,
                                    )
                                ):
                                    raise TypeError(
                                        (
                                            "A tool call must be of type:\n"
                                            "{id: str, type: 'function', "
                                            "function: { name: str, "
                                            "arguments: str }}"
                                        )
                                    )

                    elif entry_role == 'tool':
                        if not isinstance(entry.get("content"), str):
                            raise TypeError(
                                "Tool message content has to be string."
                            )
                        if not isinstance(entry.get("tool_call_id"), str):
                            raise TypeError(
                                "Tool message tool_call_id has to be string."
                            )
                    else:
                        raise TypeError(
                            (
                                "Message role must match one of these: "
                                "system, user, assistant, tool."
                            )
                        )
                    custom_prompt_messages.append(entry)
    except Exception as ex:
        raise BadRequestError(ex.args[0])

    provider_name: str
    model_name: str
    try_builtin = False
    if ':' in model:
        parts = model.split(':')
        if len(parts) > 2:
            raise BadRequestError(
                f"Invalid model uri, ':' used more than once: {model}"
            )
        provider_name = parts[0]
        model_name = parts[1]
        try_builtin = True

    else:
        provider_name = await _get_model_provider(
            db,
            base_model_type="ext::ai::TextGenerationModel",
            model_name=model,
        )
        model_name = model

    chat_provider = _get_provider_config(
        db, provider_name, try_builtin=try_builtin
    )

    vector_provider, vector_query = await _generate_embeddings_for_type(
        db,
        http_client,
        ctx_query,
        content=query,
        role_name=role_name,
    )

    ctx_query = f"""
        WITH
            __query := <array<float32>>std::to_json(<str>$input),
            search := ext::ai::search(({ctx_query}), __query),
        SELECT
            ext::ai::to_context(search.object)
        ORDER BY
            search.distance ASC EMPTY LAST
        LIMIT
            <int64>$limit
    """
    if ctx_variables is None:
        ctx_variables = {}

    ctx_variables["input"] = str(
        vector_provider.get_embeddings_from_result(vector_query)[0]
    )
    ctx_variables["limit"] = ctx_max_obj_count

    context = await _edgeql_query_json(
        db=db,
        query=ctx_query,
        variables=ctx_variables,
        globals_=ctx_globals,
        role_name=role_name,
    )
    if len(context) == 0:
        raise BadRequestError(
            'query did not match any data in specified context',
        )

    prompt_query = """
        SELECT
            ext::ai::ChatPrompt {
                messages: {
                    participant_role,
                    content,
                },
            }
        FILTER
    """

    if prompt_id or prompt_name:
        prompt_variables = {}
        if prompt_name:
            prompt_query += ".name = <str>$prompt_name"
            prompt_variables["prompt_name"] = prompt_name
        elif prompt_id:
            prompt_query += ".id = <uuid><str>$prompt_id"
            prompt_variables["prompt_id"] = prompt_id

        prompts = await _edgeql_query_json(
            db=db,
            role_name=role_name,
            query=prompt_query,
            variables=prompt_variables,
        )
        if len(prompts) == 0:
            raise BadRequestError("could not find the specified chat prompt")

        prompt = prompts[0]
    else:
        prompt = {
            "messages": [],
        }

    prompt_messages: list[dict[str, Any]] = []
    for message in prompt["messages"]:
        if message["participant_role"] == "User":
            content = message["content"].format(
                context="\n".join(context),
                query=query,
            )
        elif message["participant_role"] == "System":
            content = message["content"].format(
                context="\n".join(context),
            )
        else:
            content = message["content"]

        role = message["participant_role"].lower()

        prompt_messages.append(dict(role=role, content=content))

    # don't add here at the end the user query msg because Mistral and
    # Anthropic doesn't work if the user message shows after the tools
    messages = prompt_messages + custom_prompt_messages

    await _start_chat(
        protocol=protocol,
        request=request,
        response=response,
        provider=chat_provider,
        http_client=http_client,
        model_name=model_name,
        messages=messages,
        stream=stream,
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        max_tokens=body.get("max_tokens"),
        seed=body.get("seed"),
        safe_prompt=body.get("safe_prompt"),
        top_k=body.get("top_k"),
        logit_bias=body.get("logit_bias"),
        logprobs=body.get("logprobs"),
        user=body.get("user"),
        tools=body.get("tools"),
    )


async def _handle_embeddings_request(
    request: protocol.HttpRequest,
    response: protocol.HttpResponse,
    db: dbview.Database,
    role_name: str,
    tenant: srv_tenant.Tenant,
) -> None:
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise TypeError(
                'the body of the request must be a JSON object')

        inputs = body.get("inputs")
        input = body.get("input")

        if inputs is not None and input is not None:
            raise TypeError(
                "You cannot provide both 'inputs' and 'input'. "
                "Please provide 'inputs'; 'input' has been deprecated."
            )

        if input is not None:
            logger.warning("'input' is deprecated, use 'inputs' instead")
            inputs = input

        if not inputs:
            raise TypeError(
                'missing or empty required "inputs" value in request'
            )

        model_name = body.get("model")
        if not model_name:
            raise TypeError(
                'missing or empty required "model" value in request')

        shortening = body.get("dimensions")
        user = body.get("user")

    except Exception as ex:
        raise BadRequestError(str(ex)) from None

    provider_name = await _get_model_provider(
        db,
        base_model_type="ext::ai::EmbeddingModel",
        model_name=model_name,
    )
    if provider_name is None:
        # Error
        return

    provider = _get_provider_config(db, provider_name)

    if not isinstance(inputs, list):
        inputs = [inputs]

    result = await _generate_embeddings(
        provider,
        model_name,
        inputs,
        shortening,
        user,
        http_client=tenant.get_http_client(originator="ai/embeddings"),
    )
    if isinstance(result.data, rs.Error):
        raise AIProviderError(result.data.message)

    result_data: bytes
    if provider.api_style == ApiStyle.Ollama:
        # Ollama produces embeddings differently.
        # Repackage it to look like OpenAI.
        decoded_result = json.loads(
            result.data.embeddings.decode("utf-8")
        )
        embeddings = cast(list[list[float]], decoded_result["embeddings"])
        prompt_eval_count = cast(int, decoded_result['prompt_eval_count'])
        result_data = json.dumps({
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": embedding
                }
                for index, embedding in enumerate(embeddings)
            ],
            "model": model_name,
            "usage": {
                "prompt_tokens": prompt_eval_count,
                "total_tokens": prompt_eval_count
            }
        }).encode()

    else:
        result_data = result.data.embeddings

    response.status = http.HTTPStatus.OK
    response.content_type = b'application/json'
    response.body = result_data


async def _edgeql_query_json(
    *,
    db: dbview.Database,
    query: str,
    role_name: str | None,
    variables: Optional[dict[str, Any]] = None,
    globals_: Optional[dict[str, Any]] = None,
) -> list[Any]:
    try:
        result = await execute.parse_execute_json(
            db,
            query,
            variables=variables or {},
            globals_=globals_,
            query_tag='gel/ai',
        )

        content = json.loads(result)
    except Exception as ex:
        try:
            await _db_error(db, ex)
        except Exception as iex:
            raise iex from None
    else:
        return cast(list[Any], content)


async def _db_error(
    db: dbview.Database,
    ex: Exception,
    *,
    errcls: Optional[type[AIExtError]] = None,
    context: Optional[str] = None,
) -> NoReturn:
    if debug.flags.server:
        markup.dump(ex)

    iex = await execute.interpret_error(ex, db)

    if context:
        msg = f'{context}: {iex}'
    else:
        msg = str(iex)

    err_dct = {
        'message': msg,
        'type': str(type(iex).__name__),
        'code': iex.get_code(),
    }

    if errcls is None:
        if isinstance(iex, errors.QueryError):
            errcls = BadRequestError
        else:
            errcls = InternalError

    raise errcls(json=err_dct) from iex


def _get_provider_config(
    db: dbview.Database,
    provider_name: str,
    try_builtin: bool = False,
) -> ProviderConfig:
    """Try to return a provider config with a matching name.
    Otherwise, raise an error.

    Checks if there is a builtin provider with a matching name.

    eg. "openai" -> ProviderConfig(name="builtin::openai", ...)
    """

    cfg = db.lookup_config("ext::ai::Config::providers")

    def _create_provider_config(db_cfg: Any) -> ProviderConfig:
        cfg = cast(ProviderConfig, db_cfg)
        return ProviderConfig(
            name=cfg.name,
            display_name=cfg.display_name,
            api_url=cfg.api_url,
            client_id=cfg.client_id,
            secret=cfg.secret,
            api_style=cfg.api_style,
        )

    # try builtin prefix
    builtin_prefix = "builtin::"
    if try_builtin and not provider_name.startswith(builtin_prefix):
        builtin_name = builtin_prefix + provider_name

        for provider in cfg:
            if provider.name == builtin_name:
                return _create_provider_config(provider)

    # try unmodified name
    for provider in cfg:
        if provider.name == provider_name:
            return _create_provider_config(provider)

    raise ConfigurationError(
        f"provider {provider_name!r} has not been configured"
    )


async def _get_model_annotation_as_json(
    db: dbview.Database,
    base_model_type: str,
    model_name: str,
    annotation_name: str,
) -> Any:
    models = await _edgeql_query_json(
        db=db,
        role_name=None,
        query="""
        WITH
            base_model_type := <str>$base_model_type,
            model_name := <str>$model_name,
            Parent := (
                SELECT
                    schema::ObjectType
                FILTER
                    .name = <str>$base_model_type
            ),
            Models := Parent.<ancestors[IS schema::ObjectType],
        SELECT
            Models {
                value := (
                    SELECT
                        (FOR ann IN .annotations SELECT (ann@value, ann.name))
                    FILTER
                        .1 = <str>$annotation_name
                    LIMIT
                        1
                ).0,
            }
        FILTER
            (FOR ann in Models.annotations
            UNION (
                ann.name = "ext::ai::model_name"
                AND ann@value = <str>$model_name
            ))
        """,
        variables={
            "base_model_type": base_model_type,
            "model_name": model_name,
            "annotation_name": annotation_name,
        },
    )
    if len(models) == 0:
        raise BadRequestError("invalid model name")
    elif len(models) > 1:
        raise InternalError("multiple models defined as requested model")

    return models[0]['value']


async def _get_model_provider(
    db: dbview.Database,
    base_model_type: str,
    model_name: str,
) -> str:
    provider = await _get_model_annotation_as_json(
        db, base_model_type, model_name, "ext::ai::model_provider")
    return cast(str, provider)


async def _get_model_annotation_as_int(
    db: dbview.Database,
    base_model_type: str,
    model_name: str,
    annotation_name: str,
) -> int:
    value = await _get_model_annotation_as_json(
        db, base_model_type, model_name, annotation_name)
    return int(value)


async def _generate_embeddings_for_type(
    db: dbview.Database,
    http_client: http.HttpClient,
    type_query: str,
    content: str,
    role_name: str,
) -> tuple[ProviderConfig, bytes]:
    type_desc = await execute.describe(
        db,
        f"SELECT ({type_query})",
        allow_capabilities=compiler.Capability.NONE,
        query_tag='gel/ai',
        role_name=role_name,
    )
    if (
        not isinstance(type_desc, sertypes.ShapeDesc)
        or not isinstance(type_desc.type, sertypes.ObjectDesc)
    ):
        raise errors.InvalidReferenceError(
            'context query does not return an '
            'object type indexed with an `ext::ai::index`'
        )

    return await generate_embeddings_for_text(
        db, http_client, type_desc.type.tid, content
    )


async def generate_embeddings_for_text(
    db: dbview.Database,
    http_client: http.HttpClient,
    type_id: str | uuid.UUID,
    content: str,
) -> tuple[ProviderConfig, bytes]:

    index = await get_ai_index_for_type(db, type_id)
    provider = _get_provider_config(db=db, provider_name=index.provider)
    if (
        index.index_embedding_dimensions
        < index.model_embedding_dimensions
    ):
        shortening = index.index_embedding_dimensions
    else:
        shortening = None
    result = await _generate_embeddings(
        provider,
        index.model,
        [content],
        shortening,
        None,
        http_client,
    )
    if isinstance(result.data, rs.Error):
        raise AIProviderError(result.data.message)
    return provider, result.data.embeddings


@dataclass(frozen=True, kw_only=True)
class TextEmbeddingsResult:
    success: Optional[list[list[float]]] = None

    too_long: Optional[list[int]] = None


async def generate_embeddings_for_texts(
    db: dbview.Database,
    http_client: http.HttpClient,
    inputs: list[tuple[str | uuid.UUID, str]],
) -> TextEmbeddingsResult:
    """Generate embeddings for strings to search for ai indexed objects.

    Each input string may have a different object. The object is specified
    by the object type id as either a string or uuid.

    Produces embeddings for the input strings by:
    - grouping string by their index model and shortening
    - batching those groups
    - then doing embeddings requests in batches

    Input strings are truncated if allowed by their index.

    If any string is too long and truncating is not allowed, a "too_long"
    result is returned.

    If all embeddings requests are successful, the embeddings are returned
    as a "success" result in the same order as the inputs.
    """
    # Gather information about the indexes and embeddings
    # For each type, we will need:
    # - model name
    # - max input tokens
    # - max batch tokens
    # - provider config
    # - allowed to truncate
    # - shortening, if any
    type_ai_indexes: dict[str, AIIndex] = {}
    for type_id, _ in inputs:
        type_id = str(type_id)
        if type_id not in type_ai_indexes:
            type_ai_indexes[type_id] = await get_ai_index_for_type(db, type_id)

    model_providers = {
        ai_index.model: ai_index.provider
        for ai_index in type_ai_indexes.values()
    }
    model_max_input_tokens: dict[str, int] = {
        model_name: await _get_model_annotation_as_int(
            db,
            base_model_type="ext::ai::EmbeddingModel",
            model_name=model_name,
            annotation_name="ext::ai::embedding_model_max_input_tokens",
        )
        for model_name in model_providers.keys()
    }
    model_max_batch_tokens: dict[str, int] = {
        model_name: await _get_model_annotation_as_int(
            db,
            base_model_type="ext::ai::EmbeddingModel",
            model_name=model_name,
            annotation_name="ext::ai::embedding_model_max_batch_tokens",
        )
        for model_name in model_providers.keys()
    }

    provider_configs = {
        provider: _get_provider_config(db=db, provider_name=provider)
        for provider in set(model_providers.values())
    }

    # Group the inputs by model and shortening
    group_input_indexes: dict[tuple[str, Optional[int]], list[int]] = {}

    for input_index, (type_id, _) in enumerate(inputs):
        ai_index = type_ai_indexes[str(type_id)]

        model_name = ai_index.model
        shortening = (
            ai_index.index_embedding_dimensions
            if (
                ai_index.index_embedding_dimensions
                < ai_index.model_embedding_dimensions
            ) else
            None
        )

        group_key = (model_name, shortening)

        if group_key not in group_input_indexes:
            group_input_indexes[group_key] = []

        group_input_indexes[group_key].append(input_index)

    # Batch each group separately
    group_batch_texts_and_indexes: dict[
        tuple[str, Optional[int]],
        list[tuple[
            # texts, truncated if needed
            list[str],
            # the associated input index
            list[int],
        ]]
    ] = {}
    too_long: list[int] = []

    for group_key, input_indexes in group_input_indexes.items():
        model_name, shortening = group_key
        provider = model_providers[model_name]

        tokenizer = get_model_tokenizer(provider, model_name)
        max_input_tokens = model_max_input_tokens[model_name]
        max_batch_tokens = model_max_batch_tokens[model_name]

        texts = [
            (
                inputs[input_index][1],
                type_ai_indexes[str(inputs[input_index][0])].truncate_to_max,
            )
            for input_index in input_indexes
        ]

        text_batches, excluded_indexes = batch_texts(
            texts,
            tokenizer,
            max_input_tokens,
            max_batch_tokens,
        )

        if excluded_indexes or too_long:
            # If any input is too long, collect all inputs that are too long
            # and return them as a failure
            too_long.extend(
                input_indexes[excluded_index]
                for excluded_index in excluded_indexes
            )
            continue

        group_batch_texts_and_indexes[group_key] = []

        for text_batch in text_batches:
            batched_texts: list[str] = []
            batched_input_indexes: list[int] = []

            for entry in text_batch.entries:
                batched_texts.append(entry.input_text)
                batched_input_indexes.append(
                    input_indexes[entry.input_index]
                )

            group_batch_texts_and_indexes[group_key].append(
                (batched_texts, batched_input_indexes)
            )

    if too_long:
        return TextEmbeddingsResult(too_long=too_long)

    # Do the embeddings

    # We have been tracking the input indexes of the batch texts this whole
    # time. Use these indexes to fill in a result embeddings list
    embeddings: list[Optional[list[float]]] = [None] * len(inputs)

    for group_key, batched_texts_and_indexes in (
        group_batch_texts_and_indexes.items()
    ):
        model_name, shortening = group_key
        provider = model_providers[model_name]

        provider_config = provider_configs[provider]

        for batched_texts, batched_input_indexes in batched_texts_and_indexes:
            embeddings_result = await _generate_embeddings(
                provider_config,
                model_name,
                batched_texts,
                shortening,
                None,
                http_client,
            )
            if isinstance(embeddings_result.data, rs.Error):
                raise AIProviderError(embeddings_result.data.message)
            result_entries = provider_config.get_embeddings_from_result(
                embeddings_result.data.embeddings
            )
            for entry_index, result_entry in enumerate(result_entries):
                input_index = batched_input_indexes[entry_index]
                embeddings[input_index] = result_entry

    assert all(e is not None for e in embeddings)

    return TextEmbeddingsResult(
        success=cast(list[list[float]], embeddings),
    )


@dataclass(frozen=True, kw_only=True)
class AIIndex:
    model: str
    provider: str
    model_embedding_dimensions: int
    index_embedding_dimensions: int
    truncate_to_max: bool


async def get_ai_index_for_type(
    db: dbview.Database,
    type_id: str | uuid.UUID,
) -> AIIndex:
    try:
        indexes = await _edgeql_query_json(
            db=db,
            query="""
            WITH
                ObjectType := (
                    SELECT
                        schema::ObjectType
                    FILTER
                        .id = <uuid>$type_id
                ),
            SELECT
                ObjectType.indexes {
                    model := (
                        SELECT
                            (FOR a IN .annotations SELECT (a@value, a.name))
                        FILTER
                            .1 = "ext::ai::model_name"
                        LIMIT
                            1
                    ).0,
                    provider := (
                        SELECT
                            (FOR a IN .annotations SELECT (a@value, a.name))
                        FILTER
                            .1 = "ext::ai::model_provider"
                        LIMIT
                            1
                    ).0,
                    model_embedding_dimensions := <int64>(
                        SELECT
                            (FOR a IN .annotations SELECT (a@value, a.name))
                        FILTER
                            .1 =
                            "ext::ai::embedding_model_max_output_dimensions"
                        LIMIT
                            1
                    ).0,
                    index_embedding_dimensions := <int64>(
                        SELECT
                            (FOR a IN .annotations SELECT (a@value, a.name))
                        FILTER
                            .1 = "ext::ai::embedding_dimensions"
                        LIMIT
                            1
                    ).0,
                    truncate_to_max := any((
                        for kwarg in array_unpack(.kwargs) select (
                            kwarg.name = 'truncate_to_max'
                            and str_lower(kwarg.expr) = 'true'
                        )
                    ))
                }
            FILTER
                .ancestors.name = 'ext::ai::index'
            """,
            variables={"type_id": str(type_id)},
            role_name=None,
        )
        if len(indexes) == 0:
            raise errors.InvalidReferenceError(
                'context query does not return an '
                'object type indexed with an `ext::ai::index`'
            )
        elif len(indexes) > 1:
            raise errors.InvalidReferenceError(
                'context query returns an object '
                'indexed with multiple `ext::ai::index` indexes'
            )

    except Exception as ex:
        await _db_error(db, ex, context="context.query")

    index = indexes[0]
    return AIIndex(
        model=index["model"],
        provider=index["provider"],
        model_embedding_dimensions=index["model_embedding_dimensions"],
        index_embedding_dimensions=index["index_embedding_dimensions"],
        truncate_to_max=index["truncate_to_max"],
    )
