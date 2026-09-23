from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BaseAppSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PaginatedResponse(BaseAppSchema, Generic[T]):
    items: list[T]
    total: int = Field(description="Total records")
    page: int = Field(description="Current page")
    size: int = Field(description="Page size")
    pages: int = Field(description="Total pages")
