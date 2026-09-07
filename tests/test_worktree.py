"""The program that repairs git-worktree metadata on the destination.

A teleport moves a repo's files but not the absolute paths git records for its
linked worktrees: `.git/worktrees/<name>/gitdir` and each worktree's own `.git`
file still name the source machine. On the destination they point nowhere, so
`git worktree list` shows them prunable and the worktrees are unusable until
their pointers are rewritten to the new location.

Like the session rewriter, this program is generated as source and executed on
the far machine, so it is exercised here by compiling and running it against a
real git repository built in a temporary directory.
"""
import os
import subprocess
import sys

import pytest


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.fixture
def repo(tmp_path):
    """A repo whose files have been teleported: a `dest` tree that physically
    lives here, but whose worktree metadata still names a foreign `src` path.

    Returns an object exposing the paths a repair has to touch and a couple of
    helpers to assert health.
    """
    class Repo:
        FOREIGN = "/home/someone/src"

        def __init__(self):
            self.dest = tmp_path / "landed"
            self.main = self.dest                     # main checkout == dest_dir
            self.main.mkdir(parents=True)
            git(self.main, "init", "-q")
            git(self.main, "config", "user.email", "t@t")
            git(self.main, "config", "user.name", "t")
            (self.main / "a.txt").write_text("hi\n")
            git(self.main, "add", "a.txt")
            git(self.main, "commit", "-qm", "init")
            # The substitutions build_subs() would produce for this hop: the
            # foreign source dir maps to where the tree actually landed.
            self.subs = [(self.FOREIGN, str(self.dest))]

        def add_worktree(self, name):
            """Add a worktree living inside the dest tree, then rewrite its
            metadata to the foreign path the source machine would have left."""
            wt = self.dest / name
            git(self.main, "worktree", "add", "-q", str(wt))
            for f in (self.gitdir_file(name), wt / ".git"):
                f.write_text(f.read_text().replace(str(self.dest), self.FOREIGN))
            return wt

        def gitdir_file(self, name):
            return self.main / ".git" / "worktrees" / name / "gitdir"

        def list(self):
            return git(self.main, "worktree", "list").stdout

        def line_for(self, wt):
            """The `git worktree list` line for one worktree, or "" if absent."""
            want = os.path.realpath(str(wt))
            for line in self.list().splitlines():
                if os.path.realpath(line.split()[0]) == want:
                    return line
            return ""

        def healthy(self, wt):
            """That specific worktree is usable: not flagged prunable in the
            listing, and `git status` works from inside it. Scoped to the one
            worktree so a deliberately-orphaned sibling cannot mask it."""
            return ("prunable" not in self.line_for(wt)
                    and git(wt, "status").returncode == 0)

    return Repo()


def run_repair(tpc, dest_dir, subs):
    """Execute the generated program the way the destination would."""
    program = tpc._worktree_repair_program(str(dest_dir), subs)
    result = subprocess.run([sys.executable, "-c", program],
                            stdout=subprocess.PIPE, text=True, check=True)
    return result.stdout


def test_a_broken_worktree_is_reported_broken_first(repo):
    """Guard the fixture itself: the metadata really does point at the foreign
    path, so a green test later means the repair did the work."""
    wt = repo.add_worktree("wt-a")
    assert not repo.healthy(wt)
    assert "prunable" in repo.list()


def test_repair_heals_a_teleported_worktree(tpc, repo):
    wt = repo.add_worktree("wt-a")
    run_repair(tpc, repo.dest, repo.subs)
    assert repo.healthy(wt)


def test_repair_rewrites_both_pointer_files_to_the_destination(tpc, repo):
    wt = repo.add_worktree("wt-a")
    run_repair(tpc, repo.dest, repo.subs)
    gitdir = repo.gitdir_file("wt-a").read_text()
    backref = (wt / ".git").read_text()
    assert repo.FOREIGN not in gitdir and str(repo.dest) in gitdir
    assert repo.FOREIGN not in backref and str(repo.dest) in backref


def test_repair_heals_several_worktrees_at_once(tpc, repo):
    a = repo.add_worktree("wt-a")
    b = repo.add_worktree("wt-b")
    run_repair(tpc, repo.dest, repo.subs)
    assert repo.healthy(a)
    assert repo.healthy(b)


def test_repair_reports_how_many_it_fixed(tpc, repo):
    repo.add_worktree("wt-a")
    out = run_repair(tpc, repo.dest, repo.subs)
    assert "worktree" in out.lower()


def test_a_dangling_external_worktree_does_not_break_the_repair(tpc, repo):
    """A worktree that lived outside the transferred tree never arrives, so its
    gitdir pointer lands with no worktree behind it. The repair must fix the
    ones that did arrive and leave the orphan for git to prune later."""
    inside = repo.add_worktree("inside")
    # An external worktree: its gitdir pointer is present, its dir is not.
    ext = repo.dest / "external"
    git(repo.main, "worktree", "add", "-q", str(ext))
    repo.gitdir_file("external").write_text(
        repo.gitdir_file("external").read_text().replace(
            str(repo.dest), repo.FOREIGN))
    import shutil
    shutil.rmtree(ext)
    run_repair(tpc, repo.dest, repo.subs)          # must not raise
    assert repo.healthy(inside)


def test_a_repo_with_no_worktrees_is_a_no_op(tpc, repo):
    """No .git/worktrees at all: nothing to do, and nothing broken."""
    out = run_repair(tpc, repo.dest, repo.subs)
    assert git(repo.main, "status").returncode == 0
    assert "0" in out


def test_a_plain_directory_that_is_not_a_repo_is_a_no_op(tpc, tmp_path):
    """Someone teleports a directory that was never a git repo. The repair must
    do nothing and must not error."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "readme.txt").write_text("hi\n")
    out = run_repair(tpc, plain, [("/home/someone/src", str(plain))])
    assert "0" in out


def test_repair_does_not_touch_a_worktree_already_correct(tpc, repo):
    """Idempotence: a second run finds nothing foreign left and changes no
    pointer file's contents."""
    wt = repo.add_worktree("wt-a")
    run_repair(tpc, repo.dest, repo.subs)
    gitdir_before = repo.gitdir_file("wt-a").read_text()
    backref_before = (wt / ".git").read_text()
    run_repair(tpc, repo.dest, repo.subs)
    assert repo.gitdir_file("wt-a").read_text() == gitdir_before
    assert (wt / ".git").read_text() == backref_before
    assert repo.healthy(wt)


def test_repair_leaves_no_temporary_files_behind(tpc, repo):
    repo.add_worktree("wt-a")
    run_repair(tpc, repo.dest, repo.subs)
    assert list(repo.dest.rglob("*.tp-tmp")) == []
