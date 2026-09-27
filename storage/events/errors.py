"""Event Store 的错误类型。

全部损坏输入必须 fail closed：抛出明确错误，不静默跳过、不部分成功。
"""

from __future__ import annotations


class EventStoreError(Exception):
    """Event Store 领域错误基类。"""


class EventStoreFormatError(EventStoreError):
    """记录结构非法：非法 JSON、缺字段、字段类型错误、取值非法。"""


class UnsupportedSchemaVersionError(EventStoreError):
    """记录声明了本版本不支持的 `schema_version`。"""


class EventOrdinalError(EventStoreError):
    """记录的 `ordinal` 重复或逆序，顺序契约被破坏。"""


class EventIntegrityError(EventStoreError):
    """记录的 `event_id` 与其内容不一致（历史被原地修改）。"""
