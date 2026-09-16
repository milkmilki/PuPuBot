"""Vector serialization and scoring for the built-in semantic index."""

from __future__ import annotations

import array
import math
from collections.abc import Iterable


def normalize_vector(values: Iterable[float]) -> list[float]:
    vector = [float(item) for item in values]
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return vector
    return [item / norm for item in vector]


def pack_vector(values: Iterable[float]) -> bytes:
    arr = array.array("f", normalize_vector(values))
    return arr.tobytes()


def unpack_vector(data: bytes | memoryview | None) -> list[float]:
    if not data:
        return []
    arr = array.array("f")
    arr.frombytes(bytes(data))
    return list(arr)


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left)
    b = list(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


__all__ = ["cosine_similarity", "normalize_vector", "pack_vector", "unpack_vector"]
