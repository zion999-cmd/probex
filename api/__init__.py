"""产品 API（P0001.10 §2）：只读、版本化、stdlib-only。"""

from api.server import (
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    ProductApiHandler,
    create_handler,
    create_server,
    serve,
)

__all__ = [
    "METHOD_NOT_ALLOWED",
    "NOT_FOUND",
    "ProductApiHandler",
    "create_handler",
    "create_server",
    "serve",
]
