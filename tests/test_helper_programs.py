"""The four helper programs ship as strings and run on the far machine.

They cannot be imported -- the destination has no tp-claude installed, which is
the whole reason they travel as source. That also means a syntax error or a
broken branch inside one is invisible until it fails over ssh, where the only
evidence is a traceback wrapped in somebody else's shell. These tests compile
them locally and exercise the functions inside them directly.
"""
import json

import pytest


TEMPLATES = ("HISTORY_PLAN", "HISTORY_RENAME", "READ_STATE", "WRITE_STATE")

# Placeholders each template interpolates before it is shipped. Filled with
# repr()'d dummies so the result is real, compilable Python.
FILLERS = {
    "src_dir": repr("/src"), "dest_dir": repr("/dest"), "config": repr("/cfg"),
    "sessions": repr(["s1"]), "plan": repr("e30="), "keep": repr(False),
    "settings": repr("/cfg/.claude.json"), "history": repr("/cfg/history.jsonl"),
    "key": repr("/src/proj"), "payload": repr("e30="),
}


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_compiles_once_filled(tpc, name):
    """A syntax error here is a failure on the far side of an ssh connection."""
    filled = getattr(tpc, name) % FILLERS
    compile(filled, f"<{name}>", "exec")


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_consumes_only_known_placeholders(tpc, name):
    """A typo'd %(key)s raises KeyError at runtime, on the remote, mid-transfer."""
    getattr(tpc, name) % FILLERS       # raises KeyError if one is unknown


def _exec_write_state(tpc, namespace_extra=None):
    """Run WRITE_STATE's body far enough to bind its helper functions."""
    filled = tpc.WRITE_STATE % {**FILLERS, "payload": repr("e30=")}
    ns = {}
    exec(compile(filled, "<WRITE_STATE>", "exec"), ns)
    return ns


def test_merge_unions_lists_without_duplicating(tpc):
    """Repeat runs must not grow allowedTools by one copy each time."""
    ns = _exec_write_state(tpc)
    merge = ns["merge"]
    assert merge(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert merge(["a"], ["a"]) == ["a"]


def test_merge_keeps_permissions_granted_on_the_destination(tpc):
    """The destination's own entries survive; that is the point of unioning."""
    ns = _exec_write_state(tpc)
    merged = ns["merge"]({"allowedTools": ["Bash(local:*)"]},
                         {"allowedTools": ["Bash(incoming:*)"]})
    assert merged["allowedTools"] == ["Bash(local:*)", "Bash(incoming:*)"]


def test_merge_recurses_into_nested_dicts(tpc):
    ns = _exec_write_state(tpc)
    merged = ns["merge"]({"a": {"b": [1]}}, {"a": {"b": [2], "c": 3}})
    assert merged == {"a": {"b": [1, 2], "c": 3}}


def test_merge_replaces_scalars(tpc):
    ns = _exec_write_state(tpc)
    assert ns["merge"](True, False) is False
    assert ns["merge"]("old", "new") == "new"


def test_merge_replaces_on_type_mismatch(tpc):
    """A list arriving where a scalar sat is incoming's shape, not a union."""
    ns = _exec_write_state(tpc)
    assert ns["merge"]("scalar", ["a"]) == ["a"]
    assert ns["merge"](["a"], "scalar") == "scalar"


def test_fingerprint_ignores_key_order_and_spacing(tpc):
    """Two writers serializing the same record must not both land in history."""
    ns = _exec_write_state(tpc)
    fp = ns["fingerprint"]
    assert fp('{"a": 1, "b": 2}') == fp('{"b":2,"a":1}')


def test_fingerprint_falls_back_to_raw_text_when_unparseable(tpc):
    ns = _exec_write_state(tpc)
    assert ns["fingerprint"]("not json at all") == "not json at all"


def test_fingerprint_distinguishes_different_records(tpc):
    ns = _exec_write_state(tpc)
    fp = ns["fingerprint"]
    assert fp('{"display": "a"}') != fp('{"display": "b"}')
