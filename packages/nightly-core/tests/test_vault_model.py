"""Unit tests for `nightly_core.vault.model` — types and ID helpers."""

from __future__ import annotations

import pytest

from nightly_core.vault.model import (
    EDGE_TYPES,
    NODE_KINDS,
    Edge,
    Node,
    is_singular_edge,
    node_id_for_dispatch,
    node_id_for_feedback,
    node_id_for_lesson,
    node_id_for_pr,
    node_id_for_run,
    node_id_for_task,
    node_kind_dir,
)


def test_node_kinds_and_edge_types_are_finite_tuples():
    assert NODE_KINDS == ("run", "task", "dispatch", "pr", "feedback", "lesson")
    assert EDGE_TYPES == (
        "parent",
        "spawned",
        "derived_from",
        "produced",
        "references",
        "superseded_by",
    )


def test_id_helpers_produce_expected_shapes():
    run_id = "2026-05-27T16-30-35Z"
    assert node_id_for_run(run_id) == "run/2026-05-27T16-30-35Z"
    assert (
        node_id_for_task(run_id, "0002-audit-todos")
        == "task/2026-05-27T16-30-35Z--0002-audit-todos"
    )
    assert (
        node_id_for_dispatch(run_id, "0002-audit-todos", 1)
        == "dispatch/2026-05-27T16-30-35Z--0002-audit-todos--1"
    )
    assert node_id_for_pr(57) == "pr/57"
    assert node_id_for_feedback(57, "ab12cd") == "feedback/57--ab12cd"
    assert node_id_for_lesson(run_id, 3) == "lesson/2026-05-27T16-30-35Z--3"


def test_node_kind_dir_maps_all_kinds():
    seen = {kind: node_kind_dir(kind) for kind in NODE_KINDS}
    assert seen == {
        "run": "runs",
        "task": "tasks",
        "dispatch": "dispatches",
        "pr": "pulls",
        "feedback": "feedback",
        "lesson": "lessons",
    }


def test_is_singular_edge_only_parent_and_superseded():
    singular = [e for e in EDGE_TYPES if is_singular_edge(e)]
    assert sorted(singular) == ["parent", "superseded_by"]


def test_node_is_frozen():
    node = Node(id="run/x", kind="run")
    with pytest.raises((AttributeError, Exception)):
        node.id = "run/y"  # type: ignore[misc]


def test_edge_carries_typed_fields():
    edge = Edge(src_id="task/x", dst_id="run/y", edge_type="parent")
    assert edge.src_id == "task/x"
    assert edge.dst_id == "run/y"
    assert edge.edge_type == "parent"
