"""Именованные блокировки для операций, которые нельзя выполнять параллельно.

Бот и веб живут в одном процессе и одном event loop, поэтому asyncio-блокировки
надёжно сериализуют шаги «прочитать состояние → сходить в сеть → записать».
Там, где можно обойтись одним SQL-запросом с условием, блокировка не нужна —
такой запрос атомарен сам по себе.
"""
import asyncio
from collections import defaultdict

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def named(key: str) -> asyncio.Lock:
    """Блокировка по ключу, например publish:12345 или ad:77."""
    return _locks[key]
