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


# --- .gitignore as corroboration -------------------------------------------
#
# A .gitignore answers "should git track this?", which is broader than "is this
# derived?". It also lists secrets (.env) and local data (uploads, generated
# assets) that must travel. So entries are ADOPTED only when they independently
# look derived, and everything else is left alone.

def test_adopts_a_derived_looking_entry(tpc):
    """A build directory this ruleset does not know about by name."""
    adopted, _ = tpc.gitignore_skips("/public/build\n", set())
    assert "/public/build" in adopted


def test_does_not_adopt_an_ambiguous_entry(tpc):
    """`/public/css` is build output in one repo and hand-written in another.

    Being in .gitignore is not enough to tell them apart, so it is left alone.
    Under-skipping costs bandwidth; over-skipping costs someone's source.
    """
    adopted, _ = tpc.gitignore_skips("/public/css\n/public/js/all.js\n", set())
    assert adopted == []


def test_never_adopts_dotenv(tpc):
    """The single most damaging thing to drop: the target needs it."""
    adopted, _ = tpc.gitignore_skips(".env\n.env.*\n.env.backup\n", set())
    assert adopted == []


def test_never_adopts_credentials_or_keys(tpc):
    text = ("/storage/*.key\ntest/.db-credentials.json\n"
            ".mcp.json\nid_rsa\nsecrets.json\n*.pem\n")
    adopted, _ = tpc.gitignore_skips(text, set())
    assert adopted == []


def test_does_not_adopt_unknown_data_directories(tpc):
    """/public/assets is 196M of real assets in a real repo, not build output.

    Nothing about the name says 'derived', so it is left for the user to
    exclude by hand rather than guessed at.
    """
    adopted, _ = tpc.gitignore_skips("/public/assets\n/public/videos\n", set())
    assert adopted == []


def test_negations_are_respected(tpc):
    """`!` re-includes; adopting the pattern above it would invert intent."""
    adopted, _ = tpc.gitignore_skips("dist/\n!dist/keep.js\n", set())
    assert not any("keep" in a for a in adopted)


def test_ignores_comments_and_blanks(tpc):
    adopted, _ = tpc.gitignore_skips("# a comment\n\n  \n/dist\n", set())
    assert adopted == ["/dist"]


def test_reports_what_it_adopted(tpc):
    """Never silent: the caller prints these so a skip is always visible."""
    _, reasons = tpc.gitignore_skips("/public/build\n", set())
    assert any("/public/build" in r for r in reasons)


def test_does_not_duplicate_what_rules_already_cover(tpc):
    adopted, _ = tpc.gitignore_skips("node_modules/\n/dist/\n",
                                     {"node_modules/", "/dist/"})
    assert adopted == []


def test_adopts_cache_and_log_directories(tpc):
    adopted, _ = tpc.gitignore_skips(
        "/var/cache\n/tmp/build-artifacts\n*.log\n", set())
    assert "/var/cache" in adopted
    assert "/tmp/build-artifacts" in adopted


def test_does_not_force_a_directory_slash_onto_a_file_entry(tpc):
    """`foo/` matches only directories, so appending '/' to a file entry makes
    the pattern silently miss. Real case: .phpunit.result.cache is a file."""
    adopted, _ = tpc.gitignore_skips(".phpunit.result.cache\n", set())
    assert ".phpunit.result.cache" in adopted
    assert ".phpunit.result.cache/" not in adopted


def test_an_entry_written_with_a_slash_keeps_it(tpc):
    """A trailing slash in .gitignore is an explicit 'directory only'."""
    adopted, _ = tpc.gitignore_skips("/var/cache/\n", set())
    assert "/var/cache/" in adopted


def test_static_cache_rule_matches_cache_files(tpc):
    """The cache rule names three files; none may carry a directory slash."""
    pats = tpc.rule_patterns(tpc.lean_rules({"composer.json"}))
    for name in (".php-cs-fixer.cache", ".phpunit.cache",
                 ".phpunit.result.cache", ".DS_Store"):
        assert name in pats and f"{name}/" not in pats


def test_does_not_adopt_a_long_name_that_merely_contains_a_derived_word(tpc):
    """`/build-artifacts-i-hand-made` is not build output despite the words.

    A derived name is short and conventional (dist, build, .cache). A long
    hyphenated phrase is somebody describing their own directory, so the word
    has to account for the name rather than sit inside a sentence.
    """
    for entry in ("/build-artifacts-i-hand-made", "/output-of-my-research",
                  "/logs-i-actually-need", "/cache-of-hand-labelled-data"):
        adopted, _ = tpc.gitignore_skips(entry + "\n", set())
        assert adopted == [], entry


def test_still_adopts_conventional_compound_names(tpc):
    """Two-part names that are genuinely conventional stay adopted."""
    for entry in ("/build-cache", "/dist-prod", "/.turbo-cache"):
        adopted, _ = tpc.gitignore_skips(entry + "\n", set())
        assert adopted == [entry], entry
