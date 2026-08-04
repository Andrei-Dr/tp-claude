"""Argument parsing, rsync's trailing-slash rule, and the rewrite rules."""
import os

import pytest


# --- endpoint parsing -------------------------------------------------------

def test_recognises_a_remote_spec(tpc):
    end = tpc.Endpoint.parse("me@host:/home/me/src")
    assert (end.remote, end.ssh, end.path) == (True, "me@host", "/home/me/src")


def test_absolute_local_path_with_a_colon_is_not_a_host(tpc):
    end = tpc.Endpoint.parse("/Users/me/weird:name")
    assert end.remote is False
    assert end.path == "/Users/me/weird:name"


def test_trailing_slash_is_preserved(tpc):
    assert tpc.Endpoint.parse("/a/b/").trailing is True
    assert tpc.Endpoint.parse("/a/b").trailing is False


def test_relative_local_path_resolves_against_cwd(tpc, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    end = tpc.Endpoint.parse("proj").resolve()
    assert end.path == str(tmp_path / "proj")


def test_quoted_tilde_is_expanded_not_taken_literally(tpc):
    """An unquoted ~ is expanded by the shell, but a quoted one reaches us
    intact and must not become a directory literally named '~'."""
    end = tpc.Endpoint.parse("~/src/x").resolve()
    assert end.path == os.path.expanduser("~/src/x")
    assert "~" not in end.path


def test_remote_tilde_expands_against_the_remote_home(tpc, monkeypatch):
    end = tpc.Endpoint.parse("me@host:~/src/x")
    monkeypatch.setattr(type(end), "home", property(lambda self: "/home/me"))
    end.resolve()
    assert end.path == "/home/me/src/x"
    assert end.arg == "me@host:/home/me/src/x"


def test_named_tilde_is_refused(tpc):
    end = tpc.Endpoint.parse("me@host:~someone/src")
    with pytest.raises(SystemExit):
        end.resolve()


# --- rsync's trailing-slash rule --------------------------------------------

@pytest.mark.parametrize("src_spec, dest_spec, expected", [
    # "contents of SRC go into DEST"
    ("/u/me/src/", "h:/h/me/src/", ("/u/me/src", "/h/me/src")),
    ("/u/me/src/widget/", "h:/h/me/src/widget/",
     ("/u/me/src/widget", "/h/me/src/widget")),
    # no trailing slash: rsync creates DEST/basename(SRC)
    ("/u/me/src/widget", "h:/h/me/src/", ("/u/me/src/widget", "/h/me/src/widget")),
    ("/u/me/src/widget", "h:/h/me/src", ("/u/me/src/widget", "/h/me/src/widget")),
])
def test_landing_directory_follows_rsync(tpc, src_spec, dest_spec, expected):
    src = tpc.Endpoint.parse(src_spec)
    dest = tpc.Endpoint.parse(dest_spec)
    assert tpc.effective_landing(src, dest) == expected


def test_teleporting_a_tree_onto_itself_does_not_nest(tpc):
    """`tp-claude ~/src/ host:/home/me/src/` must stay src -> src."""
    src = tpc.Endpoint.parse("/Users/me/src/")
    dest = tpc.Endpoint.parse("h:/home/me/src/")
    assert tpc.effective_landing(src, dest) == ("/Users/me/src", "/home/me/src")


# --- substitution rules -----------------------------------------------------

def _subs(tpc, src_home, dest_home, src_dir, dest_dir):
    src = tpc.Endpoint.parse(src_dir)
    dest = tpc.Endpoint.parse("h:" + dest_dir)
    for end, home in ((src, src_home), (dest, dest_home)):
        object.__setattr__(end, "_home", home)
    return tpc.build_subs(src, dest, src_dir, dest_dir)


def test_cross_machine_rewrites_dir_encoding_and_home(tpc):
    subs = _subs(tpc, "/Users/me", "/home/me",
                 "/Users/me/src/widget", "/home/me/src/widget")
    assert ("-Users-me-src-widget", "-home-me-src-widget") in subs
    assert ("/Users/me/src/widget", "/home/me/src/widget") in subs
    assert ("/Users/me", "/home/me") in subs


def test_rules_are_ordered_longest_first(tpc):
    """A short rule applied first would clip the text a longer one needs."""
    subs = _subs(tpc, "/Users/me", "/home/me",
                 "/Users/me/src/widget", "/home/me/src/widget")
    lengths = [len(old) for old, _ in subs]
    assert lengths == sorted(lengths, reverse=True)


def test_same_home_relocation_keeps_only_the_meaningful_rules(tpc):
    """Moving a project within one machine: $HOME is unchanged, so that rule
    would be a no-op and is dropped, but the directory rules still apply."""
    subs = _subs(tpc, "/Users/me", "/Users/me", "/Users/me/a", "/Users/me/b")
    assert ("/Users/me", "/Users/me") not in subs
    assert ("/Users/me/a", "/Users/me/b") in subs


def test_identical_endpoints_produce_no_rules(tpc):
    assert _subs(tpc, "/h/me", "/h/me", "/h/me/x", "/h/me/x") == []


# --- manifest validity ------------------------------------------------------

def test_manifest_is_reused_only_for_the_same_mapping(tpc):
    subs = [("/a", "/b")]
    digest = tpc.mapping_digest(subs, "/a", "/b")
    manifest = {"version": tpc.MANIFEST_VERSION, "mapping": digest, "files": {}}
    assert tpc._manifest_applies(manifest, digest)
    # any change to where things are going invalidates it
    assert not tpc._manifest_applies(
        manifest, tpc.mapping_digest(subs, "/a", "/other"))
    assert not tpc._manifest_applies(
        manifest, tpc.mapping_digest([("/x", "/y")], "/a", "/b"))
    assert not tpc._manifest_applies({**manifest, "version": 99}, digest)
    assert not tpc._manifest_applies(None, digest)


def test_manifest_does_not_record_the_source_paths(tpc):
    """The destination should not end up holding a description of the source
    machine's layout; a digest answers the only question that matters."""
    digest = tpc.mapping_digest([("/Users/me/secret-project", "/home/me/x")],
                                "/Users/me/secret-project", "/home/me/x")
    assert "/Users/me" not in digest
    assert "secret-project" not in digest
    assert len(digest) == 64


def test_mapping_digest_is_stable_and_order_independent(tpc):
    a = tpc.mapping_digest([("/a", "/b")], "/a", "/b")
    b = tpc.mapping_digest([("/a", "/b")], "/a", "/b")
    assert a == b
