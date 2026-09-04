"""Background task handler registration wiring.

The library executors register their queue handlers at module import time;
every dispatcher test fakes the handler lookup, so a deleted or renamed
registration would otherwise only surface at runtime as an unknown task
kind. Importing the executor modules here pins the real registrations.
"""

from app.services import (
    document_processing_service,
    index_builder,
    library_task_protocol,
)


def test_importing_executors_registers_background_handlers():
    assert (
        library_task_protocol.get_task_handler(
            library_task_protocol.KIND_DOCUMENT_PROCESS,
        )
        is document_processing_service._handle_document_process_task
    )
    assert (
        library_task_protocol.get_task_handler(
            library_task_protocol.KIND_RAG_INDEX,
        )
        is index_builder._handle_rag_index_task
    )
