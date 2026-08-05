"""End-to-end runs of the real script.

Everything happens between two local directories with $HOME and
$CLAUDE_CONFIG_DIR pointed at a temporary tree, so the tests exercise the whole
program — argument handling, rsync, the manifest, the rewrite and the settings
migration — without touching a real machine or the developer's own Claude data.

$HOME matters as much as $CLAUDE_CONFIG_DIR here: the per-project settings live
in ~/.claude.json, which sits beside the config directory rather than inside it.
"""
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("rsync"),
                                reason="rsync not installed")


@pytest.fixture
def world(tmp_path, script_path):
    """A source project with one session file, ready to teleport."""
    class World:
        def __init__(self):
            self.home = tmp_path / "home"
            self.home.mkdir(parents=True)
            self.config = tmp_path / "config"
            self.src = tmp_path / "work" / "proj"
            self.dest_parent = tmp_path / "elsewhere"
            self.src.mkdir(parents=True)
            self.dest_parent.mkdir(parents=True)
            (self.src / "main.py").write_text("print('hi')\n")

        def encode(self, path):
            import re
            return re.sub(r"[^A-Za-z0-9]", "-", str(path))

        def project_dir(self, code_dir):
            return self.config / "projects" / self.encode(code_dir)

        def seed_session(self, extra=""):
            d = self.project_dir(self.src)
            d.mkdir(parents=True)
            (d / "session.jsonl").write_text(
                json.dumps({"cwd": str(self.src),
                            "file": f"{self.src}/main.py"}) + "\n" + extra)
            (d / "memory").mkdir()
            (d / "memory" / "MEMORY.md").write_text(f"see {self.src}/main.py\n")
            return d

        def run(self, *args, expect_ok=True):
            result = subprocess.run(
                [str(script_path), *[str(a) for a in args]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ,
                     "HOME": str(self.home),
                     "CLAUDE_CONFIG_DIR": str(self.config)})
            if expect_ok:
                assert result.returncode == 0, result.stdout
            return result

        @property
        def settings_file(self):
            return self.home / ".claude.json"

        def seed_settings(self, **extra):
            entry = {"hasTrustDialogAccepted": True,
                     "allowedTools": ["Bash(ls:*)"],
                     "ignorePatterns": [f"{self.src}/tmp"],
                     "lastSessionId": "abc-123",
                     "lastCost": 1.23,
                     "lastTotalInputTokens": 999,
                     "exampleFiles": ["x.py"]}
            entry.update(extra)
            self.settings_file.write_text(json.dumps(
                {"projects": {str(self.src): entry}}, indent=2))

        def seed_history(self, count=2):
            self.config.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps({"display": f"prompt {i} in {self.src}",
                                 "project": str(self.src),
                                 "timestamp": 1700000000 + i}) for i in range(count)]
            lines.append(json.dumps({"display": "unrelated",
                                     "project": "/somewhere/else"}))
            (self.config / "history.jsonl").write_text("\n".join(lines) + "\n")

        def seed_file_history(self, session="sess-1"):
            """A snapshot named the way Claude Code names them: the first 16
            hex of sha256 over the file's absolute path, then @v<n>."""
            import hashlib
            d = self.config / "file-history" / session
            d.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(
                str(self.src / "main.py").encode()).hexdigest()[:16]
            (d / f"{key}@v1").write_text("print('old')\n")
            (d / "deadbeefdeadbeef@v1").write_text("orphan\n")
            return session

        def history_key_for(self, path):
            import hashlib
            return hashlib.sha256(str(path).encode()).hexdigest()[:16]

        def settings_for(self, path):
            data = json.loads(self.settings_file.read_text())
            return data.get("projects", {}).get(str(path))

        def history_lines(self):
            f = self.config / "history.jsonl"
            return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

        @property
        def landed(self):
            return self.dest_parent / self.src.name

    return World()


# --- the happy path ---------------------------------------------------------

def test_copies_the_code(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    assert (world.landed / "main.py").read_text() == "print('hi')\n"


def test_copies_the_sessions_into_the_destination_project_dir(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    assert (world.project_dir(world.landed) / "session.jsonl").exists()
    assert (world.project_dir(world.landed) / "memory" / "MEMORY.md").exists()


def test_repoints_the_sessions_at_the_new_location(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    session = world.project_dir(world.landed) / "session.jsonl"
    record = json.loads(session.read_text().splitlines()[0])
    assert record["cwd"] == str(world.landed)
    assert record["file"] == f"{world.landed}/main.py"


def test_a_project_without_sessions_still_syncs_its_code(world):
    out = world.run(world.src, f"{world.dest_parent}/").stdout
    assert "code only" in out
    assert (world.landed / "main.py").exists()


# --- the manifest -----------------------------------------------------------

def test_leaves_a_manifest_on_the_destination(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    manifest = json.loads(
        (world.project_dir(world.landed) / ".tp-claude.run").read_text())
    assert manifest["version"] == 2
    assert len(manifest["mapping"]) == 64
    assert "session.jsonl" in manifest["files"]


def test_the_manifest_does_not_describe_the_source(world):
    """It only has to recognise its own mapping, so it stores a digest rather
    than leaving the source machine's layout on the destination."""
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    raw = (world.project_dir(world.landed) / ".tp-claude.run").read_text()
    assert str(world.src) not in raw
    assert str(world.config) not in raw


def test_an_unchanged_project_is_not_sent_again(world):
    """The whole point of the manifest: rewriting makes the two sides differ,
    so without it rsync would resend everything on every run."""
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    out = world.run(world.src, f"{world.dest_parent}/").stdout
    assert "already up to date" in out
    assert "rewrote" not in out


def test_an_edited_source_file_is_sent_again(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    session = world.project_dir(world.src) / "session.jsonl"
    session.write_text(session.read_text() +
                       json.dumps({"cwd": str(world.src), "n": 2}) + "\n")
    out = world.run(world.src, f"{world.dest_parent}/").stdout
    assert "1 of" in out and "changed" in out
    landed = world.project_dir(world.landed) / "session.jsonl"
    assert json.loads(landed.read_text().splitlines()[-1])["cwd"] == str(
        world.landed)


def test_a_meddled_destination_file_is_restored(world):
    """A source-only watermark would call this file untouched and leave the
    destination diverged, so the manifest tracks both sides."""
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    landed = world.project_dir(world.landed) / "session.jsonl"
    landed.write_text("clobbered\n")
    world.run(world.src, f"{world.dest_parent}/")
    assert json.loads(landed.read_text().splitlines()[0])["cwd"] == str(
        world.landed)


def test_full_ignores_the_manifest(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    out = world.run(world.src, f"{world.dest_parent}/", "--full").stdout
    assert "already up to date" not in out


def test_repeated_runs_converge(world):
    world.seed_session()
    for _ in range(3):
        world.run(world.src, f"{world.dest_parent}/")
    session = world.project_dir(world.landed) / "session.jsonl"
    assert str(world.src) not in session.read_text()
    assert json.loads(session.read_text().splitlines()[0])["cwd"] == str(
        world.landed)


# --- rsync semantics --------------------------------------------------------

def test_a_trailing_slash_on_the_source_syncs_contents(world):
    """`tp-claude src/ dest/` must not nest the project inside itself."""
    world.seed_session()
    dest = world.dest_parent / "proj"
    world.run(f"{world.src}/", f"{dest}/")
    assert (dest / "main.py").exists()
    assert not (dest / "proj").exists()


def test_dry_run_changes_nothing(world):
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/", "--dry-run")
    assert not world.landed.exists()
    assert not world.project_dir(world.landed).exists()


# --- refusals ---------------------------------------------------------------

def test_skips_something_that_is_not_a_directory(world):
    loose = world.src.parent / "notes.txt"
    loose.write_text("x\n")
    out = world.run(loose, f"{world.dest_parent}/").stdout
    assert "not a directory" in out


def test_refuses_two_remotes(world):
    result = world.run("a@h1:/x", "b@h2:/y", expect_ok=False)
    assert result.returncode != 0
    assert "remote -> remote" in result.stdout


def test_refuses_to_copy_a_directory_onto_itself(world):
    result = world.run(f"{world.src}/", f"{world.src}/", expect_ok=False)
    assert result.returncode != 0
    assert "same directory" in result.stdout


def test_refuses_when_both_sides_share_one_project_dir(world, tmp_path):
    """The encoding is lossy, so ~/foo-bar and ~/foo/bar collide. Proceeding
    would rewrite the original sessions in place and destroy them.
    """
    a = tmp_path / "foo-bar"
    b = tmp_path / "foo" / "bar"
    a.mkdir()
    b.mkdir(parents=True)
    result = world.run(f"{a}/", b, expect_ok=False)
    assert result.returncode != 0
    assert "share one Claude project directory" in result.stdout


# --- project settings and prompt history ------------------------------------

def test_carries_the_settings_entry_to_the_new_path(world):
    """Without this the destination shows the trust dialog again and every
    granted tool permission is re-prompted."""
    world.seed_session()
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/")
    entry = world.settings_for(world.landed)
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["allowedTools"] == ["Bash(ls:*)"]
    assert entry["lastSessionId"] == "abc-123"


def test_rewrites_paths_inside_the_settings_entry(world):
    world.seed_session()
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/")
    assert world.settings_for(world.landed)["ignorePatterns"] == [
        f"{world.landed}/tmp"]


def test_leaves_per_run_measurements_behind(world):
    """They describe the machine that produced them, not the project."""
    world.seed_session()
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/")
    entry = world.settings_for(world.landed)
    assert "lastCost" not in entry
    assert "lastTotalInputTokens" not in entry
    assert "exampleFiles" not in entry


def test_keeps_the_original_settings_entry(world):
    world.seed_session()
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/")
    assert world.settings_for(world.src) is not None


def test_carries_prompt_history_for_the_project(world):
    world.seed_session()
    world.seed_history(count=2)
    world.run(world.src, f"{world.dest_parent}/")
    moved = [h for h in world.history_lines()
             if h.get("project") == str(world.landed)]
    assert len(moved) == 2
    assert str(world.landed) in moved[0]["display"]


def test_leaves_other_projects_history_alone(world):
    world.seed_session()
    world.seed_history()
    world.run(world.src, f"{world.dest_parent}/")
    assert any(h.get("project") == "/somewhere/else"
               for h in world.history_lines())


def test_does_not_duplicate_history_on_repeat_runs(world):
    world.seed_session()
    world.seed_history(count=3)
    world.run(world.src, f"{world.dest_parent}/")
    first = len(world.history_lines())
    world.run(world.src, f"{world.dest_parent}/")
    assert len(world.history_lines()) == first


def test_settings_travel_even_without_sessions(world):
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/")
    assert world.settings_for(world.landed)["hasTrustDialogAccepted"] is True


def test_dry_run_does_not_touch_settings(world):
    world.seed_session()
    world.seed_settings()
    world.run(world.src, f"{world.dest_parent}/", "--dry-run")
    assert world.settings_for(world.landed) is None


# --- merging without duplicating -------------------------------------------

def test_repeated_runs_do_not_grow_list_settings(world):
    """Unioning is the point: concatenating would add a copy every run."""
    world.seed_session()
    world.seed_settings(allowedTools=["Bash(ls:*)", "Read"])
    for _ in range(3):
        world.run(world.src, f"{world.dest_parent}/")
    assert world.settings_for(world.landed)["allowedTools"] == [
        "Bash(ls:*)", "Read"]


def test_keeps_permissions_granted_on_the_destination(world):
    """Replacing the list wholesale would silently revoke them."""
    world.seed_session()
    world.seed_settings(allowedTools=["Bash(ls:*)"])
    world.run(world.src, f"{world.dest_parent}/")
    # something granted locally on the destination afterwards
    data = json.loads(world.settings_file.read_text())
    data["projects"][str(world.landed)]["allowedTools"].append("Write")
    world.settings_file.write_text(json.dumps(data, indent=2))

    world.run(world.src, f"{world.dest_parent}/")
    merged = world.settings_for(world.landed)["allowedTools"]
    assert "Write" in merged and "Bash(ls:*)" in merged
    assert len(merged) == len(set(merged))


def test_merges_nested_settings_without_duplicates(world):
    world.seed_session()
    world.seed_settings(mcpServers={"alpha": {"command": "run-alpha"}})
    world.run(world.src, f"{world.dest_parent}/")
    world.run(world.src, f"{world.dest_parent}/")
    servers = world.settings_for(world.landed)["mcpServers"]
    assert servers == {"alpha": {"command": "run-alpha"}}


def test_history_dedupe_survives_different_key_ordering(world):
    """Entries are compared by content; matching raw text would append a fresh
    copy of everything whenever the writers disagree on key order."""
    world.seed_session()
    world.seed_history(count=2)
    world.run(world.src, f"{world.dest_parent}/")
    before = len(world.history_lines())

    # rewrite the destination's entries with keys in a different order
    lines = world.history_lines()
    reordered = [json.dumps(dict(sorted(h.items(), reverse=True))) for h in lines]
    (world.config / "history.jsonl").write_text("\n".join(reordered) + "\n")

    world.run(world.src, f"{world.dest_parent}/")
    assert len(world.history_lines()) == before


def test_a_repeat_run_adds_no_history_at_all(world):
    world.seed_session()
    world.seed_history(count=4)
    world.run(world.src, f"{world.dest_parent}/")
    first = world.history_lines()
    world.run(world.src, f"{world.dest_parent}/")
    assert world.history_lines() == first


# --- rewind snapshots -------------------------------------------------------

def test_rewind_snapshots_are_rekeyed_to_the_new_paths(world):
    """Their filenames hash the absolute path of the file they snapshot, so a
    straight copy would leave every one unreadable at the destination."""
    session = world.seed_file_history()
    world.seed_session()
    # the transcript must be named after the session for it to be found
    (world.project_dir(world.src) / f"{session}.jsonl").write_text(
        json.dumps({"cwd": str(world.src)}) + "\n")

    world.run(world.src, f"{world.dest_parent}/")

    landed = world.config / "file-history" / session
    expected = world.history_key_for(world.landed / "main.py")
    assert (landed / f"{expected}@v1").exists()
    assert (landed / f"{expected}@v1").read_text() == "print('old')\n"


def test_snapshots_of_vanished_files_are_not_rekeyed(world):
    """There is nothing to re-key them against, so they gain no new name."""
    session = world.seed_file_history()
    world.seed_session()
    (world.project_dir(world.src) / f"{session}.jsonl").write_text(
        json.dumps({"cwd": str(world.src)}) + "\n")

    world.run(world.src, f"{world.dest_parent}/")
    landed = world.config / "file-history" / session
    names = {f.name for f in landed.iterdir()}
    # the orphan keeps its own name and acquires no destination twin
    assert "deadbeefdeadbeef@v1" in names
    assert len([n for n in names if n.endswith("@v1")]) == 3


def test_the_source_snapshots_are_left_alone(world):
    session = world.seed_file_history()
    world.seed_session()
    (world.project_dir(world.src) / f"{session}.jsonl").write_text(
        json.dumps({"cwd": str(world.src)}) + "\n")
    original = world.history_key_for(world.src / "main.py")

    world.run(world.src, f"{world.dest_parent}/")
    assert (world.config / "file-history" / session /
            f"{original}@v1").exists()


# --- symlinked locations ----------------------------------------------------
#
# Claude Code realpaths the working directory before naming a project, so a
# project reached through a symlink is filed under the target. tp-claude must
# key its data off the same resolved path or the sessions land where Claude
# will never look — the symptom being an empty `claude --resume`.

def test_data_follows_a_symlinked_destination(world, tmp_path):
    """dest given via a symlink -> data must land under the real path."""
    real = tmp_path / "real-store"
    real.mkdir()
    link = tmp_path / "linked-store"
    link.symlink_to(real)

    world.seed_session()
    world.run(world.src, f"{link}/")

    landed_real = real / world.src.name
    assert world.project_dir(landed_real).exists()          # realpath-encoded
    assert not world.project_dir(link / world.src.name).exists()  # not the link
    session = world.project_dir(landed_real) / "session.jsonl"
    assert json.loads(session.read_text().splitlines()[0])["cwd"] == str(
        landed_real)


def test_data_follows_a_symlinked_source(world, tmp_path):
    """src reached via a symlink -> data located under the real path too."""
    real = tmp_path / "real-src"
    real.mkdir()
    (real / "main.py").write_text("print('hi')\n")
    link = tmp_path / "linked-src"
    link.symlink_to(real)

    # seed the session under the *resolved* path, as Claude Code would have
    d = world.project_dir(real)
    d.mkdir(parents=True)
    (d / "session.jsonl").write_text(
        json.dumps({"cwd": str(real), "file": f"{real}/main.py"}) + "\n")

    dest = tmp_path / "out"
    world.run(f"{link}/", f"{dest}/")

    assert (dest / "main.py").exists()
    assert world.project_dir(dest).exists()
    session = world.project_dir(dest) / "session.jsonl"
    assert json.loads(session.read_text().splitlines()[0])["cwd"] == str(dest)


def test_symlinked_dest_that_does_not_exist_yet(world, tmp_path):
    """A brand-new project name under a symlinked parent still resolves: only
    the existing ancestor is realpathed, the new leaf is re-appended."""
    real = tmp_path / "real-parent"
    real.mkdir()
    link = tmp_path / "linked-parent"
    link.symlink_to(real)

    world.seed_session()
    world.run(world.src, f"{link}/")          # creates <real>/proj fresh

    assert (real / world.src.name / "main.py").exists()
    assert world.project_dir(real / world.src.name).exists()
