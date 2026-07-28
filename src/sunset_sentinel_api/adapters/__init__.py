"""External adapters for files, HTTP, Git, fixtures, and SQLite."""

from sunset_sentinel_api.adapters.file_sources import (
    FileSourceError,
    SourceBatch,
    load_consumers_file,
    load_file_sources,
    load_manual_feed_file,
    load_openapi_file,
    merge_source_batches,
)
from sunset_sentinel_api.adapters.git_provenance import (
    GitFileProvenance,
    GitProvenance,
    GitProvenanceError,
)
from sunset_sentinel_api.adapters.http_client import (
    FetchResult,
    FetchStatus,
    HttpLifecycleClient,
    InMemoryCache,
    redact_query_values,
)
from sunset_sentinel_api.adapters.sqlite_http_cache import (
    SQLiteHttpCache,
    SQLiteRequestPacingStore,
)
from sunset_sentinel_api.adapters.sqlite_repository import (
    LifecycleChange,
    SQLiteRepository,
    StoredLifecycleSignal,
)

__all__ = [
    "FetchResult",
    "FetchStatus",
    "FileSourceError",
    "GitFileProvenance",
    "GitProvenance",
    "GitProvenanceError",
    "HttpLifecycleClient",
    "InMemoryCache",
    "LifecycleChange",
    "SQLiteHttpCache",
    "SQLiteRepository",
    "SQLiteRequestPacingStore",
    "SourceBatch",
    "StoredLifecycleSignal",
    "load_consumers_file",
    "load_file_sources",
    "load_manual_feed_file",
    "load_openapi_file",
    "merge_source_batches",
    "redact_query_values",
]
