"""The project-directory encoding must match Claude Code exactly.

Get this wrong and sessions land in a directory Claude will never look in, so
these cases are checked against the algorithm lifted from the shipped binaries:

    const MAX = 200
    function encode(p) {
      let s = p.replace(/[^a-zA-Z0-9]/g, "-")
      if (s.length <= MAX) return s
      return s.slice(0, MAX) + "-" + <hash>(p)
    }

Reading releases 2.1.90 through 2.1.221, only <hash> ever changed, and only
once: Bun.hash up to 2.1.100, and a plain 32-bit string hash from 2.1.101.
Paths short enough to skip the suffix encode identically on every release.
"""
import json
import shutil
import subprocess
import unicodedata

import pytest

# up to 2.1.100
REFERENCE_JS_BUN = """
const MAX = 200;
function encode(p) {
  let s = p.replace(/[^a-zA-Z0-9]/g, "-");
  if (s.length <= MAX) return s;
  return s.slice(0, MAX) + "-" + Bun.hash(p).toString(36);
}
console.log(JSON.stringify(JSON.parse(process.argv[1]).map(encode)));
"""

# 2.1.101 onwards
REFERENCE_JS_31 = """
const MAX = 200;
function h(e){let t=0;for(let r=0;r<e.length;r++)t=(t<<5)-t+e.charCodeAt(r)|0;return t}
function encode(p) {
  let s = p.replace(/[^a-zA-Z0-9]/g, "-");
  if (s.length <= MAX) return s;
  return s.slice(0, MAX) + "-" + Math.abs(h(p)).toString(36);
}
console.log(JSON.stringify(JSON.parse(process.argv[1]).map(encode)));
"""

PATHS = [
    "/Users/me/src/widget",
    "/Users/me/Downloads/My Notes v2.0 (final)",
    "/Users/me/.claude",
    "/home/me/src/widget",
    "/",
    "/tmp/a b/c(d)e",
    "/srv/projects/widget",
    # long enough that Claude truncates and appends a Bun.hash suffix
    "/Users/me/src/" + "verylongsegment/" * 14 + "final",
    "/Users/me/src/" + "x" * 300,
]


def test_simple_paths_map_every_non_alnum_to_dash(tpc):
    assert tpc.encode_path("/a/b-c.d") == "-a-b-c-d"
    assert tpc.encode_path("/.claude") == "--claude"
    assert tpc.encode_path("/Users/me/Downloads/My Notes v2.0 (final)") == (
        "-Users-me-Downloads-My-Notes-v2-0--final-"
    )


def test_short_paths_are_not_truncated(tpc):
    encoded = tpc.encode_path("/Users/me/src/widget")
    assert len(encoded) <= tpc.ENCODE_MAX
    assert encoded == "-Users-me-src-widget"


def test_long_paths_are_truncated_and_suffixed(tpc):
    long_path = "/Users/me/src/" + "x" * 300
    encoded = tpc.encode_path(long_path)
    head, _, suffix = encoded.rpartition("-")
    assert len(head) == tpc.ENCODE_MAX
    assert suffix and suffix.isalnum()


def test_short_paths_ignore_the_hash_entirely(tpc):
    """Below the limit the two schemes cannot disagree, which is why ordinary
    transfers never need to know either machine's version."""
    def explode(_text):
        raise AssertionError("hash must not be consulted")

    assert tpc.encode_path("/Users/me/src/widget", explode) == "-Users-me-src-widget"


def test_normalises_to_nfc_before_encoding(tpc):
    """macOS hands back decomposed names; Claude normalises to NFC first.

    Decomposed "é" is "e" + a combining accent, which would otherwise encode to
    "e-" while the composed form encodes to "-".
    """
    composed = unicodedata.normalize("NFC", "/tmp/caf\u00e9")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert tpc.encode_path(decomposed) == tpc.encode_path(composed)
    assert tpc.encode_path(composed) == "-tmp-caf-"


def _reference(script):
    return json.loads(subprocess.run(
        ["bun", "-e", script, json.dumps(PATHS)],
        stdout=subprocess.PIPE, text=True, check=True).stdout)


@pytest.mark.skipif(not shutil.which("bun"), reason="bun not installed")
def test_matches_releases_from_2_1_101(tpc):
    expected = _reference(REFERENCE_JS_31)
    assert [tpc.encode_path(p, tpc.js31_hash36) for p in PATHS] == expected


@pytest.mark.skipif(not shutil.which("bun"), reason="bun not installed")
def test_matches_releases_up_to_2_1_100(tpc):
    expected = _reference(REFERENCE_JS_BUN)
    assert [tpc.encode_path(p, tpc.bun_hash36) for p in PATHS] == expected


@pytest.mark.skipif(not shutil.which("bun"), reason="bun not installed")
def test_the_two_schemes_agree_below_the_length_limit(tpc):
    """Only over-long paths can diverge between releases."""
    short = [p for p in PATHS
             if len(tpc.encode_path(p, tpc.js31_hash36)) <= tpc.ENCODE_MAX]
    assert short
    old = _reference(REFERENCE_JS_BUN)
    new = _reference(REFERENCE_JS_31)
    for path in short:
        assert old[PATHS.index(path)] == new[PATHS.index(path)]


def test_the_two_schemes_differ_above_the_length_limit(tpc):
    long_path = "/Users/me/src/" + "x" * 300
    assert (tpc.encode_path(long_path, tpc.js31_hash36)
            != tpc.encode_path(long_path, tpc.bun_hash36)
            if shutil.which("bun") else True)


def test_js31_hash_is_the_documented_algorithm(tpc):
    """h = (h * 31 + charCode) | 0, then Math.abs(...).toString(36)."""
    def reference(text):
        value = 0
        for unit in text.encode("utf-16-le").hex(" ", 2).split():
            code = int(unit[2:4] + unit[0:2], 16)
            value = (value * 31 + code) & 0xFFFFFFFF
            if value >= 0x80000000:
                value -= 0x100000000
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        number, out = abs(value), ""
        while number:
            number, rem = divmod(number, 36)
            out = digits[rem] + out
        return out or "0"

    for sample in ["a", "hello", "/Users/me/src/widget", "", "caf\u00e9"]:
        assert tpc.js31_hash36(sample) == reference(sample)


def test_the_release_the_hash_changed_in_is_recorded(tpc):
    assert tpc.HASH_CHANGED_IN == (2, 1, 101)


def test_distinct_paths_can_collide(tpc):
    """The encoding is lossy: '/' and '-' both become '-'.

    tp-claude has to refuse same-machine transfers that collide, so this
    documents the property the guard exists for.
    """
    assert tpc.encode_path("/home/me/foo-bar") == tpc.encode_path("/home/me/foo/bar")
