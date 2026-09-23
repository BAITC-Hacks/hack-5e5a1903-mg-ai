from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base

# BIGINT в SQLite не является алиасом rowid, поэтому автоинкремент там не работает.
# Вариант для SQLite нужен, чтобы те же модели поднимались в тестах на sqlite+aiosqlite.
PrimaryKeyType = BigInteger().with_variant(Integer, "sqlite")


class BaseModel(AsyncAttrs, Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(PrimaryKeyType, primary_key=True, autoincrement=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
