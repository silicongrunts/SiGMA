"""
Document processing status constants.

Single source of truth for all document lifecycle states.  Import from
here instead of writing raw string literals.

DB values are plain strings — this module only provides named constants
to prevent typos and centralize the allowed set.
"""

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"   # docling conversion + AI field extraction
STATUS_INDEXING = "indexing"       # RAG vector indexing
STATUS_CANCELLING = "cancelling"   # document deleted, processing should stop
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

ALL_STATUSES = frozenset({
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
    STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
})

ACTIVE_STATUSES = frozenset({
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
})
