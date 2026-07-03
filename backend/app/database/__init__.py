"""
SiGMA Database Module

Provides SQLite-based storage for sessions, messages, and annotations.
"""

from .models import Base, Session, Message, Annotation
from .manager import DatabaseManager

__all__ = [
    "Base",
    "Session",
    "Message",
    "Annotation",
    "DatabaseManager",
]
