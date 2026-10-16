"""Shared payroll route dependency aliases."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.dependencies import get_current_user, get_db, get_engine

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep = Annotated[AsyncConnection, Depends(get_db)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
