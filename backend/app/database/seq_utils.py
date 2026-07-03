"""
Seq allocation utility — retry-on-conflict helper for unique-sequence inserts.

Used by repos that need concurrent-safe seq assignment under a UNIQUE
constraint on (group_column, seq).
"""

import asyncio
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 8
RETRY_DELAY = 0.01  # base delay in seconds, scaled by attempt number


async def allocate_seq_with_retry(
    session: AsyncSession,
    model_class,
    group_column,
    group_value,
    factory,
    *,
    max_retries: int = MAX_RETRIES,
) -> object:
    """Allocate a unique ``seq`` value and insert a new row, retrying on conflict.

    Under concurrent writers the ``(group_column, seq)`` UNIQUE constraint may
    be violated.  This helper retries the read-max+1 → insert → commit cycle
    up to *max_retries* times with a short back-off.

    Args:
        session: SQLAlchemy async session (commits and rolls back internally).
        model_class: The ORM model (e.g. ``Message``, ``Task``).
        group_column: The column that defines the seq namespace
            (e.g. ``Message.session_id``, ``Message.annotation_id``).
        group_value: The value of *group_column* for this insert.
        factory: ``Callable[[int], object]`` that receives the allocated
            *seq* value and returns a new (unsaved) ORM instance.
        max_retries: Maximum attempts before raising ``RuntimeError``.

    Returns:
        The ORM instance (already committed).

    Raises:
        RuntimeError: When all retries are exhausted.
    """
    for attempt in range(max_retries):
        max_seq_result = await session.execute(
            select(func.coalesce(func.max(model_class.seq), -1))
            .where(group_column == group_value)
        )
        next_seq = max_seq_result.scalar_one() + 1

        obj = factory(next_seq)
        session.add(obj)
        try:
            await session.commit()
            return obj
        except (IntegrityError, OperationalError):
            await session.rollback()
            if attempt < max_retries - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            logger.debug(
                "seq conflict on %s=%s attempt %d, retrying",
                group_column.key, group_value, attempt,
            )
    raise RuntimeError(
        f"Failed to allocate seq for {model_class.__name__} "
        f"({group_column.key}={group_value}) after {max_retries} retries"
    )


async def stage_seq_object(
    session: AsyncSession,
    model_class,
    group_column,
    group_value,
    factory,
) -> object:
    """Stage a row with the next sequence value without committing.

    The caller owns the transaction and should commit through a retry-aware
    UnitOfWork boundary. SQLAlchemy autoflushes pending rows before the max(seq)
    query, so multiple staged rows for the same group in one transaction receive
    increasing seq values.
    """
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(model_class.seq), -1))
        .where(group_column == group_value)
    )
    next_seq = max_seq_result.scalar_one() + 1
    obj = factory(next_seq)
    session.add(obj)
    return obj
