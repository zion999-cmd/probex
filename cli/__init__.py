"""Probex 产品 CLI（P0001.10.3）：只读、机器优先、稳定退出码、stdout/stderr 分离。"""

from cli.main import (
    EXIT_BLOCKED,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_UNKNOWN,
    EXIT_USAGE,
    CliUnavailable,
    build_parser,
    main,
)

__all__ = [
    "EXIT_BLOCKED",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_UNKNOWN",
    "EXIT_USAGE",
    "CliUnavailable",
    "build_parser",
    "main",
]
