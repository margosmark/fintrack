"""Privacy lockdown for the securities price-stream SSE endpoint (docs/11 step 6).

Since M47 (docs/29 F5) the stream is cache-only: it reads the ``prices`` table
directly and never touches ``market_data``'s network seam at all. Under
``privacy_mode=True`` the stream must close before any DB or network access.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.api._price_stream import price_stream_generator
from app.services import market_data


async def _collect(gen, max_events=None):
    out = []
    try:
        while max_events is None or len(out) < max_events:
            out.append(await gen.__anext__())
    except StopAsyncIteration:
        return out
    return out


def test_privacy_mode_closes_stream_without_external_fetch():
    """With privacy_mode on, no market_data fetch is ever reached."""
    market_data.bind_privacy_lock(True)
    try:
        with (
            patch("app.services.market_data.fetch_latest_close") as mock_fetch,
            patch("app.services.market_data.fetch_history") as mock_history,
        ):
            gen = price_stream_generator([1, 2, 3], user_id=42)
            events = asyncio.run(_collect(gen))
        assert events == []
        mock_fetch.assert_not_called()
        mock_history.assert_not_called()
    finally:
        market_data.bind_privacy_lock(False)


def test_privacy_mode_does_not_open_a_session():
    """The privacy close happens before the generator touches DB or network."""
    market_data.bind_privacy_lock(True)
    try:
        with (
            patch("app.api._price_stream.SQLModelSession") as mock_session,
            patch("app.api._price_stream.get_engine") as mock_engine,
            patch("app.services.market_data.fetch_latest_close") as mock_fetch,
            patch("app.services.market_data.fetch_history") as mock_history,
        ):
            gen = price_stream_generator([99], user_id=7)
            events = asyncio.run(_collect(gen))
        assert events == []
        mock_session.assert_not_called()
        mock_engine.assert_not_called()
        mock_fetch.assert_not_called()
        mock_history.assert_not_called()
    finally:
        market_data.bind_privacy_lock(False)
