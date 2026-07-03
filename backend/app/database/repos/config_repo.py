"""
Repository for the project_config table (key-value settings per project).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import ProjectConfig


class ProjectConfigRepository:
    """CRUD for project-level key-value configuration."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, key: str, default: str = "") -> str:
        result = await self._session.execute(
            select(ProjectConfig).where(ProjectConfig.key == key)
        )
        row = result.scalar_one_or_none()
        return row.value if row else default

    async def get_all(self) -> dict:
        result = await self._session.execute(select(ProjectConfig))
        rows = result.scalars().all()
        return {r.key: r.value for r in rows}

    async def set(self, key: str, value: str) -> None:
        result = await self._session.execute(
            select(ProjectConfig).where(ProjectConfig.key == key)
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            self._session.add(ProjectConfig(key=key, value=value))
        await self._session.commit()
