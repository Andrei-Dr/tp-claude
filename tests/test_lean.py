"""The --lean exclusion ruleset: which directories are skipped, and when.

Rules are ecosystem-gated on purpose. A bare `vendor/` in a repo with no
composer.json or go.mod is somebody's source directory, not a dependency tree,
and a public tool that drops it would be destroying work. Detection is what
separates the two.
"""
import pytest


# --- which rules fire, given what's in the tree -----------------------------

def test_node_modules_needs_no_detection(tpc):
    """node_modules is unambiguous: no other ecosystem names a directory that."""
    rules = tpc.lean_rules({"package.json"})
    assert "node_modules/" in tpc.rule_patterns(rules)


def test_vendor_requires_php_or_go(tpc):
    """`vendor/` only means 'dependencies' when a manifest says so."""
    assert "/vendor/" in tpc.rule_patterns(tpc.lean_rules({"composer.json"}))
    assert "/vendor/" in tpc.rule_patterns(tpc.lean_rules({"go.mod"}))
    assert "/vendor/" not in tpc.rule_patterns(tpc.lean_rules({"README.md"}))


def test_target_requires_cargo(tpc):
    """Rust's build dir. Without Cargo.toml, `target/` is just a name."""
    assert "/target/" in tpc.rule_patterns(tpc.lean_rules({"Cargo.toml"}))
    assert "/target/" not in tpc.rule_patterns(tpc.lean_rules({"package.json"}))


def test_laravel_storage_spares_user_uploads(tpc):
    """storage/logs and storage/framework are derived; storage/app is uploads.

    Excluding `storage/` wholesale would silently drop user data that no
    reinstall can bring back. This is the single most destructive mistake this
    feature could make, so it gets its own test.
    """
    pats = tpc.rule_patterns(tpc.lean_rules({"composer.json", "artisan"}))
    assert "/storage/logs/" in pats
    assert "/storage/framework/" in pats
    assert not any(p.rstrip("/").endswith("storage") for p in pats)
    assert not any("storage/app" in p for p in pats)


def test_anchored_patterns_do_not_match_at_depth(tpc):
    """`/vendor/` is rooted; `resources/vendor/` is a source dir and stays."""
    pats = tpc.rule_patterns(tpc.lean_rules({"composer.json"}))
    assert "/vendor/" in pats and "vendor/" not in pats


def test_node_modules_matches_at_every_depth(tpc):
    """pnpm/npm workspaces nest node_modules per package; all of them go."""
    pats = tpc.rule_patterns(tpc.lean_rules({"package.json"}))
    assert "node_modules/" in pats          # unanchored == any depth


# --- reconstructability preflight ------------------------------------------
#
# Skipping a dependency tree is only safe when a package manager can rebuild
# it. These are the cases where it cannot, and each one is reported rather than
# guessed at.

def _project(tmp_path, files):
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return tmp_path


def test_lockfile_makes_a_tree_reconstructable(tmp_path, tpc):
    root = _project(tmp_path, {"package.json": '{"name":"x"}',
                               "pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
    assert tpc.reconstructability(str(root)).warnings == []


def test_manifest_without_lockfile_warns(tmp_path, tpc):
    """No lockfile means no pinned versions to reinstall from."""
    root = _project(tmp_path, {"package.json": '{"name":"x"}'})
    warnings = tpc.reconstructability(str(root)).warnings
    assert any("lockfile" in w for w in warnings)


def test_file_dep_pointing_outside_the_tree_warns(tmp_path, tpc):
    """A `file:` dep resolves to a path that is not being synced."""
    root = _project(tmp_path, {
        "package.json": '{"dependencies":{"@a/i":"file:../outside/icons"}}',
        "package-lock.json": "{}"})
    warnings = tpc.reconstructability(str(root)).warnings
    assert any("file:" in w and "@a/i" in w for w in warnings)


def test_link_dep_warns(tmp_path, tpc):
    root = _project(tmp_path, {
        "package.json": '{"dependencies":{"@a/b":"link:../elsewhere"}}',
        "pnpm-lock.yaml": "x"})
    assert any("@a/b" in w for w in tpc.reconstructability(str(root)).warnings)


def test_workspace_relative_file_dep_is_fine(tmp_path, tpc):
    """A file: dep INSIDE the synced tree travels with it, so it rebuilds."""
    root = _project(tmp_path, {
        "package.json": '{"dependencies":{"@a/b":"file:./packages/b"}}',
        "packages/b/package.json": '{"name":"@a/b"}',
        "pnpm-lock.yaml": "x"})
    assert tpc.reconstructability(str(root)).warnings == []


def test_pnpm_patches_warn(tmp_path, tpc):
    """patchedDependencies needs the patch files; flag it so they're checked."""
    root = _project(tmp_path, {
        "package.json": '{"pnpm":{"patchedDependencies":{"foo@1":"p/foo.patch"}}}',
        "pnpm-lock.yaml": "x"})
    assert any("patch" in w.lower()
               for w in tpc.reconstructability(str(root)).warnings)


def test_composer_path_repository_warns(tmp_path, tpc):
    root = _project(tmp_path, {
        "composer.json": '{"repositories":[{"type":"path","url":"../local-pkg"}]}',
        "composer.lock": "{}"})
    assert any("path" in w for w in tpc.reconstructability(str(root)).warnings)


def test_venv_without_any_requirements_warns(tmp_path, tpc):
    """A venv with nothing declaring its contents cannot be rebuilt at all."""
    root = _project(tmp_path, {"main.py": "print(1)"})
    (root / ".venv" / "bin").mkdir(parents=True)
    assert any("venv" in w.lower()
               for w in tpc.reconstructability(str(root)).warnings)


def test_clean_repo_reports_nothing(tmp_path, tpc):
    root = _project(tmp_path, {"README.md": "hi"})
    assert tpc.reconstructability(str(root)).warnings == []
