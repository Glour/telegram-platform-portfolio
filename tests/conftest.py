from __future__ import annotations

import asyncio

import pytest

try:
    import uvloop
except ImportError:
    uvloop = None


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if uvloop is not None:
        return uvloop.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()
