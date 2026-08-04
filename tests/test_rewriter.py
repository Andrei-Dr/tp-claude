"""The program that rewrites embedded paths on the destination.

It is generated as source and executed on the far machine, so it is exercised
here by compiling and running it against a temporary directory.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

SUBS = [
    ("-Users-me-src-widget", "-home-me-src-widget"),
    ("/Users/me/src/widget", "/home/me/src/widget"),
    ("/Users/me", "/home/me"),
]


def run_rewriter(tpc, root, only=None):
    """Execute the generated program the way the destination would."""
    program = tpc._rewriter_program(str(root), SUBS, only)
    result = subprocess.run([sys.executable, "-c", program],
                            stdout=subprocess.PIPE, text=True, check=True)
    return result.stdout


@pytest.fixture
def project(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "session.jsonl").write_text(textwrap.dedent("""\
        {"cwd":"/Users/me/src/widget"}
        {"file":"/Users/me/src/widget/app.py"}
        {"sibling":"/Users/me/src/widget2/other.py"}
        {"suffixed":"/Users/me/src/widget-backup/old.py"}
        {"elsewhere":"/Users/me/.claude/skills/s.md"}
        """))
    (tmp_path / "index.json").write_text(json.dumps(
        {"path": "/Users/me/.claude/projects/-Users-me-src-widget/x.jsonl"}))
    (tmp_path / "notes.md").write_text("nothing to see here\n")
    (tmp_path / "sub" / "blob.bin").write_bytes(b"\xff\xfe\x00binary\xc3(")
    (tmp_path / "screenshot.png").write_text("/Users/me/src/widget\n")
    return tmp_path


def test_rewrites_cwd_and_paths_inside_the_project(tpc, project):
    run_rewriter(tpc, project)
    lines = (project / "session.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["cwd"] == "/home/me/src/widget"
    assert json.loads(lines[1])["file"] == "/home/me/src/widget/app.py"


def test_leaves_sibling_directories_alone(tpc, project):
    """Rewriting '.../Flow' must not maul '.../Flow2' or '.../Flow-backup'.

    A plain string replace corrupts both; matching stops at a path boundary.
    """
    run_rewriter(tpc, project)
    lines = (project / "session.jsonl").read_text().splitlines()
    assert json.loads(lines[2])["sibling"] == "/home/me/src/widget2/other.py"
    assert json.loads(lines[3])["suffixed"] == (
        "/home/me/src/widget-backup/old.py")


def test_rewrites_paths_outside_the_project(tpc, project):
    """Sessions reference skills, config and other repos under $HOME; those
    must move too or they dangle on the far machine."""
    run_rewriter(tpc, project)
    line = (project / "session.jsonl").read_text().splitlines()[4]
    assert json.loads(line)["elsewhere"] == "/home/me/.claude/skills/s.md"


def test_rewrites_the_encoded_project_directory(tpc, project):
    run_rewriter(tpc, project)
    assert json.loads((project / "index.json").read_text())["path"] == (
        "/home/me/.claude/projects/-home-me-src-widget/x.jsonl")


def test_ignores_files_it_does_not_understand(tpc, project):
    before = (project / "screenshot.png").read_text()
    run_rewriter(tpc, project)
    assert (project / "screenshot.png").read_text() == before


def test_leaves_binary_files_untouched(tpc, project):
    before = (project / "sub" / "blob.bin").read_bytes()
    run_rewriter(tpc, project)
    assert (project / "sub" / "blob.bin").read_bytes() == before


def test_reports_only_the_files_it_changed(tpc, project):
    out = run_rewriter(tpc, project)
    assert "rewrote 2 file(s)" in out          # session.jsonl + index.json


def test_unchanged_files_are_not_rewritten(tpc, project):
    before = (project / "notes.md").stat().st_mtime_ns
    run_rewriter(tpc, project)
    assert (project / "notes.md").stat().st_mtime_ns == before


def test_clears_debris_from_an_interrupted_run(tpc, project):
    debris = project / "session.jsonl.tp-tmp"
    debris.write_text("half-written\n")
    run_rewriter(tpc, project)
    assert not debris.exists()


def test_leaves_no_temporary_files_behind(tpc, project):
    run_rewriter(tpc, project)
    assert list(project.rglob("*.tp-tmp")) == []


def test_can_be_limited_to_named_files(tpc, project):
    """Incremental runs rewrite only what was just transferred."""
    out = run_rewriter(tpc, project, only=["index.json"])
    assert "rewrote 1 file(s)" in out
    assert "/Users/me" in (project / "session.jsonl").read_text()
    assert "/home/me" in (project / "index.json").read_text()


def test_rewriting_twice_changes_nothing_the_second_time(tpc, project):
    run_rewriter(tpc, project)
    after_first = (project / "session.jsonl").read_text()
    assert "rewrote 0 file(s)" in run_rewriter(tpc, project)
    assert (project / "session.jsonl").read_text() == after_first


def test_handles_a_file_larger_than_memory_comfortably(tpc, tmp_path):
    """Session transcripts reach hundreds of megabytes, so the rewriter streams
    rather than reading whole files."""
    big = tmp_path / "big.jsonl"
    with open(big, "w") as fh:
        for _ in range(20000):
            fh.write('{"cwd":"/Users/me/src/widget","pad":"%s"}\n' % ("x" * 200))
    run_rewriter(tpc, tmp_path)
    with open(big) as fh:
        assert json.loads(fh.readline())["cwd"] == "/home/me/src/widget"
    assert "/Users/me" not in big.read_text()
