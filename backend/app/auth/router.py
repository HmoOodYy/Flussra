"""
Auth router — POST /auth/login and GET /auth/me.

All other routes in the app require a valid JWT (via get_current_user).
These two are the entry points that create or verify tokens.
"""
from sqlalchemy.ext.asyncio import AsyncConnection
from fastapi import APIRouter, Depends

from app.auth.schemas import LoginRequest, LoginResponse, UserInfo
from app.auth.service import login, get_me
from app.dependencies import get_db, get_current_user

router = APIRouter()


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate and receive a JWT access token",
    responses={
        401: {"description": "Invalid username, company code, or password"},
        403: {"description": "Account inactive, locked, or company suspended"},
        422: {"description": "Request body validation failed (blank fields, etc.)"},
    },
)
async def auth_login(
    request: LoginRequest,
    db: AsyncConnection = Depends(get_db),
) -> LoginResponse:
    return await login(request, db)


@router.get(
    "/me",
    response_model=UserInfo,
    summary="Return the current user's identity (re-validates JWT against DB)",
    responses={
        401: {"description": "Missing, expired, or invalid token"},
    },
)
async def auth_me(
    token_payload: dict = Depends(get_current_user),
    db: AsyncConnection = Depends(get_db),
) -> UserInfo:
    # "sub" is stored as a string (RFC 7519 requirement); cast back to int for DB queries.
    return await get_me(
        user_id=int(token_payload["sub"]),
        company_id=int(token_payload["cid"]),
        db=db,
    )
