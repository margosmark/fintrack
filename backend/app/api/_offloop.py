"""Off-loop hops for routes that must stay ``async def`` (M149, docs/01).

Rule (docs/01 § "Sessions and the event loop"): the event loop never waits on
the database, and it never does blocking work of its own. A route has to be
``async def`` when it needs ``await file.read()``, but everything AFTER that
read — CSV parse, DB reads/writes, an LLM call, base64 work — is ordinary
blocking Python. Left on the loop it freezes *every* request in the process,
not just this one: the container runs a single uvicorn worker and one loop, so
one 20 MB receipt upload stalls the dashboard, /health and every other tab.

M148 fixed the two places that were already biting (the auth query and the SSE
stream's held session) by hand. This is the shared form of that hop: the six
upload/parse routes keep their ``async`` signature and push the blocking half
through a worker thread.

    def core(...) -> Model:          # sync, may block for as long as it likes
        ...

    async def route(...) -> Model:
        raw = await file.read()      # the only part that needs `async`
        return await blocking(core)(...)

Wrap a **plain sync function**. Passing a coroutine function would be silently
wrong (the hop would return an un-awaited coroutine), so the wrapper refuses
one instead of shipping that bug.
"""

import inspect
from collections.abc import Awaitable, Callable

from starlette.concurrency import run_in_threadpool


def blocking[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """Return ``fn`` as an awaitable that runs in a worker thread.

    The same hop ``get_current_user`` uses for its one query (M148): the
    threadpool does the blocking, the loop stays free, and exceptions raised
    inside ``fn`` (including the ``api_error`` envelope) propagate to the
    caller's ``await`` exactly as they would have inline.
    """
    if inspect.iscoroutinefunction(fn):
        raise TypeError(
            "blocking() wraps SYNC work; an async function is already off-loop "
            "only if it never blocks — awaiting it here would drop its result"
        )

    async def call(*args: P.args, **kwargs: P.kwargs) -> R:
        return await run_in_threadpool(fn, *args, **kwargs)

    return call
