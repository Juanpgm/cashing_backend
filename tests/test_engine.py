"""Tests for app/agent/engine.py — custom async graph orchestration engine."""
from __future__ import annotations

import asyncio
import pytest

from app.agent.engine import CompiledGraph, END, HumanInterrupt, RunResult


def _make_simple_graph() -> CompiledGraph:
    """Helper: A → B → END."""
    g = CompiledGraph()
    g.add_node("A", lambda s: {**s, "a": 1})
    g.add_node("B", lambda s: {**s, "b": 2})
    g.add_edge("A", "B")
    g.add_edge("B", END)
    g.set_entry_point("A")
    return g.compile()


async def test_engine_sync_node():
    g = _make_simple_graph()
    result = await g.run({})
    assert result.state["a"] == 1
    assert result.state["b"] == 2
    assert result.status == "completed"


async def test_engine_async_node():
    async def async_node(s):
        await asyncio.sleep(0)
        return {**s, "async": True}

    g = CompiledGraph()
    g.add_node("A", async_node)
    g.add_edge("A", END)
    g.set_entry_point("A")
    g = g.compile()
    result = await g.run({})
    assert result.state["async"] is True
    assert result.status == "completed"


async def test_engine_conditional_edge():
    def router(s):
        return "left" if s.get("go_left") else "right"

    g = CompiledGraph()
    g.add_node("entry", lambda s: s)
    g.add_node("left", lambda s: {**s, "branch": "left"})
    g.add_node("right", lambda s: {**s, "branch": "right"})
    g.add_conditional_edges("entry", router, {"left": "left", "right": "right"})
    g.add_edge("left", END)
    g.add_edge("right", END)
    g.set_entry_point("entry")
    g = g.compile()

    r1 = await g.run({"go_left": True})
    assert r1.state["branch"] == "left"

    r2 = await g.run({"go_left": False})
    assert r2.state["branch"] == "right"


async def test_engine_end_termination():
    g = _make_simple_graph()
    result = await g.run({})
    assert result.status == "completed"
    assert result.paused_node is None


async def test_engine_human_interrupt_pause():
    def hil_node(s):
        raise HumanInterrupt("need approval")

    g = CompiledGraph()
    g.add_node("entry", lambda s: s)
    g.add_node("hil", hil_node)
    g.add_edge("entry", "hil")
    g.add_edge("hil", END)
    g.set_entry_point("entry")
    g = g.compile()

    result = await g.run({})
    assert result.status == "paused"
    assert result.paused_node == "hil"
    assert result.interrupt_message == "need approval"


async def test_engine_resume_from_node():
    g = CompiledGraph()
    g.add_node("start", lambda s: {**s, "start": True})
    g.add_node("mid", lambda s: {**s, "mid": True})
    g.add_node("end_node", lambda s: {**s, "end": True})
    g.add_edge("start", "mid")
    g.add_edge("mid", "end_node")
    g.add_edge("end_node", END)
    g.set_entry_point("start")
    g = g.compile()

    # Resume from mid — start should NOT run
    result = await g.run({}, start_node="mid")
    assert result.status == "completed"
    assert result.state.get("start") is None  # was skipped
    assert result.state["mid"] is True
    assert result.state["end"] is True


async def test_engine_max_steps_guard():
    g = CompiledGraph()
    g.add_node("A", lambda s: s)
    g.add_node("B", lambda s: s)
    g.add_edge("A", "B")
    g.add_edge("B", "A")  # cycle
    g.set_entry_point("A")
    g = g.compile()

    with pytest.raises(RuntimeError, match="exceeded"):
        await g.run({})


async def test_engine_ainvoke_ignores_config():
    g = _make_simple_graph()
    state = await g.ainvoke({}, config={"configurable": {"thread_id": "abc"}})
    assert state["a"] == 1
    assert state["b"] == 2


async def test_engine_nodes_property():
    g = _make_simple_graph()
    assert "A" in g.nodes
    assert "B" in g.nodes


async def test_engine_graph_edges_property():
    g = _make_simple_graph()
    edges = g.graph.edges
    assert isinstance(edges, set)
    assert ("A", "B") in edges
