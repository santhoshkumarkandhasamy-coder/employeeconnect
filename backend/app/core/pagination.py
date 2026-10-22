from __future__ import annotations

from typing import Any

from fastapi import Query


class PageParams:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def make_page(items: list[Any], params: PageParams, total: int) -> dict[str, Any]:
    """Contract envelope: {items, page, page_size, total}."""
    return {"items": items, "page": params.page, "page_size": params.page_size, "total": total}
