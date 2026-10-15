"""Uniform result envelope for every graph tool (KTD3, R15)."""

from __future__ import annotations

import time
from textwrap import dedent
from typing import Any

from pydantic import BaseModel, Field

from ic.graph.client import query


class ToolResult(BaseModel):
    """What every tool returns: the answer plus the evidence of how it was computed."""

    tool: str
    result: Any
    cypher: list[str] = Field(description="Every query the tool ran, in order")
    params: list[dict[str, Any]] = Field(description="Parameters for each query, same order")
    nodes_touched: list[str] = Field(default_factory=list, description="Domain ids only")
    ms: float = 0.0
    graph: str = Field(description="Graph the tool ran against (server-injected)")


class ToolError(Exception):
    """A tool could not answer (bad arguments, missing incident, out-of-scope graph...)."""


class QueryLog:
    """Runs read-only Cypher on one graph and keeps every query and its parameters.

    Tools never format values into Cypher: everything goes through parameters, so the
    recorded (cypher, params) pairs are exactly what a judge can paste and re-run.
    """

    def __init__(self, graph: str):
        self.graph = graph
        self.cypher: list[str] = []
        self.params: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()

    async def rows(self, cypher: str, params: dict[str, Any] | None = None) -> list[list]:
        """Run ``cypher`` and return its rows; the query index is ``len(self.cypher) - 1``."""
        self.cypher.append(dedent(cypher).strip())
        self.params.append(dict(params or {}))
        res = await query(self.graph, self.cypher[-1], self.params[-1], read_only=True)
        return res.result_set

    @property
    def last_index(self) -> int:
        return len(self.cypher) - 1

    def result(self, tool: str, result: Any, nodes_touched: list[str | None]) -> ToolResult:
        seen = list(dict.fromkeys(n for n in nodes_touched if n))
        return ToolResult(
            tool=tool,
            result=result,
            cypher=self.cypher,
            params=self.params,
            nodes_touched=seen,
            ms=round((time.perf_counter() - self._t0) * 1000, 2),
            graph=self.graph,
        )
