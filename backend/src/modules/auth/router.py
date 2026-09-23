from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.modules.auth.dependencies import get_current_user
from src.modules.auth.models import User
from src.modules.auth.schemas import LoginRequest, TokenResponse, UserRead
from src.modules.auth.service import authenticate

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):  # noqa: B008
    return await authenticate(db, data)


@router.get("/me", response_model=UserRead)
async def me(current_user: User = Depends(get_current_user)):  # noqa: B008
    return current_user
