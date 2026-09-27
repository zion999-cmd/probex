"""预测运行时领域的错误类型。

失败必须显式：绝不允许在失败时伪造 probability = 0 或返回空 Prediction。
"""

from __future__ import annotations


class PredictionError(Exception):
    """预测运行时领域错误基类。"""


class PredictionTimeoutError(PredictionError):
    """provider 在 timeout 内没有返回。"""


class PredictionTransportError(PredictionError):
    """传输层失败（连接、DNS、HTTP 状态等）。"""


class PredictionProviderError(PredictionError):
    """provider 自身失败，或抛出未预期异常。"""


class PredictionParseError(PredictionError):
    """响应不是可解析的 JSON（结构层面无法解析）。"""


class PredictionInvalidResponseError(PredictionError):
    """响应可解析但语义非法（概率越界、缺分类、NaN、未知字段等）。"""
