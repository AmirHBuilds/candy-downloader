"""Import-only stand-in for httpx: the dispatcher imports handlers that use it,
but no test performs real HTTP."""


class HTTPError(Exception):
    pass


class AsyncClient:
    def __init__(self, *a, **k):
        raise RuntimeError("stub httpx: tests must not make HTTP requests")
