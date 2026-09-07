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
    """It only has to recognize its own mapping, so it stores a digest rather
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


# --- --lean -----------------------------------------------------------------
#
# Real transfers between real directories, because the thing being tested is
# whether rsync ends up skipping what it was told to skip -- and rsync's
# pattern semantics (anchored vs not) are exactly the part worth proving rather
# than assuming.

@pytest.fixture
def node_world(world):
    """A workspace whose derived directories mirror a real pnpm monorepo."""
    (world.src / "package.json").write_text(
        '{"name":"app","packageManager":"pnpm@9.15.4"}')
    (world.src / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    for rel in ("node_modules/left-pad/index.js",
                "packages/ui/node_modules/dep/index.js",
                ".turbo/cache/blob",
                "apps/web/.next/static/chunk.js",
                "dist/bundle.js",
                "src/index.ts",
                "packages/ui/button.tsx"):
        target = world.src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    return world


def test_lean_skips_dependencies_and_build_output(node_world):
    node_world.run("--lean", node_world.src, f"{node_world.dest_parent}/")
    landed = node_world.landed
    assert not (landed / "node_modules").exists()
    assert not (landed / "packages/ui/node_modules").exists()
    assert not (landed / ".turbo").exists()
    assert not (landed / "apps/web/.next").exists()
    assert not (landed / "dist").exists()


def test_lean_keeps_the_source(node_world):
    """Everything that is not derived still has to arrive."""
    node_world.run("--lean", node_world.src, f"{node_world.dest_parent}/")
    landed = node_world.landed
    assert (landed / "src/index.ts").exists()
    assert (landed / "packages/ui/button.tsx").exists()
    assert (landed / "package.json").exists()
    assert (landed / "pnpm-lock.yaml").exists()


def test_without_lean_nothing_is_skipped(node_world):
    """The default is unchanged: --lean is opt-in."""
    node_world.run(node_world.src, f"{node_world.dest_parent}/")
    assert (node_world.landed / "node_modules/left-pad/index.js").exists()
    assert (node_world.landed / ".turbo/cache/blob").exists()


def test_lean_reports_the_reinstall_command(node_world):
    out = node_world.run("--lean", node_world.src,
                         f"{node_world.dest_parent}/").stdout
    assert "pnpm install" in out
    assert str(node_world.landed) in out


def test_no_vendors_is_an_alias_for_lean(node_world):
    node_world.run("--no-vendors", node_world.src,
                   f"{node_world.dest_parent}/")
    assert not (node_world.landed / "node_modules").exists()


def test_lean_leaves_a_hand_written_vendor_alone(world):
    """No composer.json or go.mod, so `vendor/` is somebody's source."""
    (world.src / "vendor" / "mine").mkdir(parents=True)
    (world.src / "vendor" / "mine" / "code.py").write_text("mine\n")
    world.run("--lean", world.src, f"{world.dest_parent}/")
    assert (world.landed / "vendor" / "mine" / "code.py").exists()


def test_lean_skips_composer_vendor(world):
    (world.src / "composer.json").write_text('{"name":"a/b"}')
    (world.src / "composer.lock").write_text("{}")
    (world.src / "vendor" / "pkg").mkdir(parents=True)
    (world.src / "vendor" / "pkg" / "f.php").write_text("<?php\n")
    world.run("--lean", world.src, f"{world.dest_parent}/")
    assert not (world.landed / "vendor").exists()
    assert (world.landed / "composer.lock").exists()


def test_lean_spares_laravel_user_uploads(world):
    """storage/logs is derived; storage/app is uploads nothing can rebuild."""
    (world.src / "composer.json").write_text('{"name":"a/b"}')
    (world.src / "composer.lock").write_text("{}")
    (world.src / "artisan").write_text("#!/usr/bin/env php\n")
    for rel in ("storage/logs/laravel.log", "storage/framework/views/x.php",
                "storage/app/uploads/photo.jpg"):
        target = world.src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    world.run("--lean", world.src, f"{world.dest_parent}/")
    assert (world.landed / "storage/app/uploads/photo.jpg").exists()
    assert not (world.landed / "storage/logs").exists()
    assert not (world.landed / "storage/framework").exists()


def test_lean_warns_about_a_dep_it_cannot_rebuild(world):
    (world.src / "package.json").write_text(
        '{"dependencies":{"@a/icons":"file:../outside/icons"}}')
    (world.src / "package-lock.json").write_text("{}")
    (world.src / "node_modules").mkdir()
    out = world.run("--lean", world.src, f"{world.dest_parent}/").stdout
    assert "@a/icons" in out
    assert "outside the synced tree" in out


def test_lean_proceeds_despite_warnings(world):
    """Reporting, not refusing -- the transfer still completes."""
    (world.src / "package.json").write_text(
        '{"dependencies":{"@a/i":"file:../nope"}}')
    result = world.run("--lean", world.src, f"{world.dest_parent}/")
    assert result.returncode == 0
    assert (world.landed / "main.py").exists()


def test_exclude_takes_extra_patterns(world):
    (world.src / "secrets").mkdir()
    (world.src / "secrets" / "k.pem").write_text("k\n")
    world.run("--exclude", "secrets/", world.src, f"{world.dest_parent}/")
    assert not (world.landed / "secrets").exists()
    assert (world.landed / "main.py").exists()


def test_no_worktrees_is_independent_of_lean(world):
    """Agent worktrees are machine-local, but they are not build output."""
    wt = world.src / ".claude" / "worktrees" / "task-1"
    wt.mkdir(parents=True)
    (wt / "f.txt").write_text("scratch\n")
    (world.src / ".claude" / "settings.local.json").write_text("{}")
    world.run("--no-worktrees", world.src, f"{world.dest_parent}/")
    assert not (world.landed / ".claude" / "worktrees").exists()
    assert (world.landed / ".claude" / "settings.local.json").exists()


def test_lean_does_not_touch_sessions(node_world):
    """--lean is about the code tree; the transcripts still travel in full."""
    node_world.seed_session()
    node_world.run("--lean", node_world.src, f"{node_world.dest_parent}/")
    proj = node_world.project_dir(node_world.landed)
    assert (proj / "session.jsonl").exists()
    assert (proj / "memory" / "MEMORY.md").exists()


def test_dry_run_lean_reports_without_copying(node_world):
    out = node_world.run("--lean", "--dry-run", node_world.src,
                         f"{node_world.dest_parent}/").stdout
    assert "dry run" in out
    assert not node_world.landed.exists()


def test_lean_adopts_a_gitignored_build_dir(world):
    """The project's own .gitignore names build dirs no rule knows about."""
    (world.src / "package.json").write_text('{"name":"a"}')
    (world.src / "package-lock.json").write_text("{}")
    (world.src / ".gitignore").write_text("/public/build\n.env\n")
    for rel in ("public/build/app.js", "public/index.html"):
        target = world.src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    out = world.run("--lean", world.src, f"{world.dest_parent}/").stdout
    assert not (world.landed / "public/build").exists()
    assert (world.landed / "public/index.html").exists()
    assert "/public/build" in out          # never a silent skip


def test_lean_never_drops_dotenv(world):
    """.gitignore lists .env; the destination cannot run without it."""
    (world.src / ".gitignore").write_text(".env\n.env.local\n/dist\n")
    (world.src / ".env").write_text("SECRET=1\n")
    (world.src / ".env.local").write_text("SECRET=2\n")
    world.run("--lean", world.src, f"{world.dest_parent}/")
    assert (world.landed / ".env").read_text() == "SECRET=1\n"
    assert (world.landed / ".env.local").exists()


def test_lean_with_delete_does_not_prune_the_excluded_dirs(world):
    """--delete prunes what the source no longer has, but an excluded
    directory is not "no longer there" -- it was never offered. rsync protects
    excludes from deletion (only --delete-excluded would remove them, which is
    never passed), so a destination that already has node_modules keeps it."""
    (world.src / "package.json").write_text('{"name":"a"}')
    (world.src / "package-lock.json").write_text("{}")
    (world.src / "node_modules" / "pkg").mkdir(parents=True)
    (world.src / "node_modules" / "pkg" / "index.js").write_text("new\n")
    world.run(world.src, f"{world.dest_parent}/")          # full transfer
    assert (world.landed / "node_modules" / "pkg" / "index.js").exists()
    world.run("--lean", "--delete", world.src, f"{world.dest_parent}/")
    assert (world.landed / "node_modules" / "pkg" / "index.js").exists()


def test_lean_skips_nested_projects_under_a_parent_directory(world):
    """`tp-claude ~/dev/ host:/home/me/dev/` syncs many projects at once.

    Anchored rules exist so `resources/vendor/` is not mistaken for Composer's,
    but anchoring them to the TRANSFER root makes them miss every project below
    it -- /target/ would mean dev/target rather than dev/app/target. They have
    to be anchored to the project that proves them instead.
    """
    parent = world.src.parent / "many"
    for name, manifest, derived in (
            ("appA", "package.json", "node_modules/x/i.js"),
            ("appB", "Cargo.toml", "target/debug/bin"),
            ("appC", "composer.json", "vendor/pkg/f.php")):
        (parent / name).mkdir(parents=True)
        (parent / name / manifest).write_text("{}")
        target = parent / name / derived
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("derived\n")
        (parent / name / "src.txt").write_text("source\n")
    # A hand-written vendor/ in a project with no PHP manifest still survives.
    (parent / "appB" / "vendor" / "mine").mkdir(parents=True)
    (parent / "appB" / "vendor" / "mine" / "code.rs").write_text("mine\n")

    out = world.dest_parent / "many"
    world.run("--lean", f"{parent}/", f"{out}/")
    assert not (out / "appA" / "node_modules").exists()
    assert not (out / "appB" / "target").exists()
    assert not (out / "appC" / "vendor").exists()
    assert (out / "appB" / "vendor" / "mine" / "code.rs").exists()
    for name in ("appA", "appB", "appC"):
        assert (out / name / "src.txt").exists()


def test_no_worktrees_matches_at_any_depth(world):
    """A worktree is machine-local wherever it sits, so a nested one arrives
    just as broken as one at the root. Unlike the ecosystem rules there is no
    ambiguous case to protect: the path is specific enough not to collide."""
    for rel in (".claude/worktrees/t1/f.txt",
                "packages/ui/.claude/worktrees/t2/f.txt"):
        target = world.src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("scratch\n")
    (world.src / "packages" / "ui" / "button.tsx").write_text("src\n")
    world.run("--no-worktrees", world.src, f"{world.dest_parent}/")
    assert not (world.landed / ".claude" / "worktrees").exists()
    assert not (world.landed / "packages/ui/.claude/worktrees").exists()
    assert (world.landed / "packages" / "ui" / "button.tsx").exists()


def test_lean_keeps_downloaded_model_weights(world):
    """Model weights sit loose beside the code and no reinstall reproduces
    them, so nothing may skip them even though the .venv around them goes."""
    (world.src / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (world.src / ".venv" / "lib").mkdir(parents=True)
    (world.src / ".venv" / "lib" / "pkg.py").write_text("installed\n")
    for name in ("yolo26s.pt", "model.onnx", "weights.safetensors"):
        (world.src / name).write_text("binary blob\n")
    out = world.run("--lean", world.src, f"{world.dest_parent}/").stdout
    assert not (world.landed / ".venv").exists()
    for name in ("yolo26s.pt", "model.onnx", "weights.safetensors"):
        assert (world.landed / name).exists(), name
    assert "pip install -e ." in out          # lockless pyproject still guided


def test_lean_keeps_a_live_sqlite_database(world):
    """A real .gitignore lists `.data` and `*.db` beside `dist` and `.turbo`.

    The database and its WAL are live state that nothing regenerates, so they
    travel while the build output around them does not.
    """
    (world.src / "package.json").write_text('{"name":"a"}')
    (world.src / "package-lock.json").write_text("{}")
    (world.src / ".gitignore").write_text(
        "node_modules\n.next\ndist\n.env\n.data\n*.db\n.turbo\n")
    (world.src / ".data").mkdir()
    for name in ("app.db", "app.db-wal", "app.db-shm"):
        (world.src / ".data" / name).write_text("sqlite\n")
    (world.src / ".env").write_text("DATABASE_URL=x\n")
    (world.src / "dist").mkdir()
    (world.src / "dist" / "bundle.js").write_text("built\n")
    world.run("--lean", world.src, f"{world.dest_parent}/")
    for name in ("app.db", "app.db-wal", "app.db-shm"):
        assert (world.landed / ".data" / name).exists(), name
    assert (world.landed / ".env").exists()
    assert not (world.landed / "dist").exists()


def test_lean_on_a_rails_app(world):
    """Bundled gems and Rails' own caches go; a hand-written vendor/ and
    public/assets stay. public/assets is compiled output in Rails and real
    source elsewhere, so it is left alone the way storage/app is."""
    (world.src / "Gemfile").write_text('source "https://rubygems.org"\n')
    (world.src / "Gemfile.lock").write_text("GEM\n")
    (world.src / "config").mkdir()
    (world.src / "config" / "application.rb").write_text("module App; end\n")
    for rel in ("vendor/bundle/ruby/gem.rb", ".bundle/config",
                "tmp/cache/x", "log/development.log",
                "app/models/user.rb", "vendor/mygem/lib.rb",
                "public/assets/app-abc.js"):
        target = world.src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    out = world.run("--lean", world.src, f"{world.dest_parent}/").stdout
    assert not (world.landed / "vendor" / "bundle").exists()
    assert not (world.landed / ".bundle").exists()
    assert not (world.landed / "tmp" / "cache").exists()
    assert not (world.landed / "log").exists()
    assert (world.landed / "app" / "models" / "user.rb").exists()
    assert (world.landed / "vendor" / "mygem" / "lib.rb").exists()
    assert (world.landed / "public" / "assets" / "app-abc.js").exists()
    assert "bundle install" in out


# --- git worktrees survive the hop -------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _make_repo_with_worktree(root):
    """A git repo at `root` with one linked worktree inside it, named the way
    the agent worktrees that prompted this feature are: .claude/worktrees/*."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "main.py").write_text("print('hi')\n")
    _git(root, "add", "main.py")
    _git(root, "commit", "-qm", "init")
    wt = root / ".claude" / "worktrees" / "agent-1"
    _git(root, "worktree", "add", "-q", str(wt))
    return wt


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_a_git_worktree_is_reconnected_at_the_destination(world):
    """A repo carrying a linked worktree lands with that worktree's git
    metadata still naming the source path; the teleport re-homes it so it is
    usable on the destination rather than showing up prunable."""
    _make_repo_with_worktree(world.src)
    world.seed_session()
    world.run(world.src, f"{world.dest_parent}/")
    landed_wt = world.landed / ".claude" / "worktrees" / "agent-1"
    listing = _git(world.landed, "worktree", "list").stdout
    # The landed worktree is healthy: named in the listing, not prunable, and
    # git commands work from inside it.
    assert str(landed_wt) in listing.replace("/private", "")\
        or str(landed_wt) in listing
    assert "prunable" not in listing
    assert _git(landed_wt, "status").returncode == 0
    # And its pointer files no longer name the source tree.
    gitdir = (world.landed / ".git" / "worktrees" / "agent-1" / "gitdir")
    assert str(world.src) not in gitdir.read_text()


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_the_worktree_step_runs_even_without_sessions(world):
    """The code-only path (no Claude data for this project) still repairs
    worktrees -- a repo can have them whether or not it was ever opened here."""
    landed_wt = world.landed / ".claude" / "worktrees" / "agent-1"
    _make_repo_with_worktree(world.src)
    out = world.run(world.src, f"{world.dest_parent}/").stdout
    assert "code only" in out
    assert "reconnecting git worktrees" in out
    assert _git(landed_wt, "status").returncode == 0
