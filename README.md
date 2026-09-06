# tp-claude

Move a project to another machine **along with its Claude Code conversations**.

`rsync` moves your code. It does not move the sessions, and Claude Code keys
those to the project's absolute path — so the same repo at `/Users/me/src/app`
on a laptop and `/home/me/src/app` on a server are two unrelated projects with
no shared history. `tp-claude` copies the code, brings the sessions with it, and
repoints them at the destination so `claude --resume` picks up where you left
off.

```
tp-claude ~/src/app  me@server:/home/me/src/app
```

## Usage

```
tp-claude <SRC> <DEST> [--lean] [--no-worktrees] [--exclude=PATTERN]
                       [--dry-run] [--delete] [--full] [-v]
```

`SRC` and `DEST` go to rsync **verbatim** and mean exactly what they mean to
rsync, trailing slash included:

```
tp-claude ~/src/app   me@server:/home/me/src/      # creates .../src/app
tp-claude ~/src/app/  me@server:/home/me/src/app/  # mirrors app's contents
tp-claude ~/src/      me@server:/home/me/src/      # mirrors all of ~/src
```

At most one side may be remote. `local -> local` works too, which is handy for
relocating a project without losing its history:

```
tp-claude ~/src/app ~/archive/
```

| flag | effect |
| --- | --- |
| `--lean` | skip dependency trees and build output a package manager can rebuild (aliases: `--no-vendors`, `--no-deps`) |
| `--no-worktrees` | skip `.claude/worktrees` at any depth — agent git checkouts naming this machine's paths |
| `--exclude=PATTERN` | one more rsync exclude; repeatable |
| `--no-lean-rule=NAME` | turn off a single `--lean` rule, by a name from the table below |
| `--dry-run` | report what would move; changes nothing |
| `--delete` | mirror exactly, pruning destination files the source no longer has (off by default) |
| `--full` | ignore the manifest and resend every session file |
| `-v` | list every transferred file |

### Requirements

| | Needed | Notes |
|---|---|---|
| Python | 3.9+ on both ends | verified on 3.11 – 3.14; the destination runs a small helper script |
| rsync | either flavor | verified against macOS `openrsync` (protocol 29) **and** GNU rsync 3.x, in both directions |
| ssh | key auth to any remote | password prompts are deliberately disabled |
| `bun` | only for one edge case | see the release table below |

| Platform | Status |
|---|:---:|
| macOS → Linux, Linux → macOS | ✅ verified end to end |
| macOS ↔ macOS, Linux ↔ Linux | ✅ |
| local → local (relocating a project) | ✅ |
| Windows | ❌ needs a POSIX shell — WSL should work, untested |

Password prompts are disabled on purpose (`BatchMode=yes`): the helper scripts
occupy ssh's stdin, so a prompt would have nowhere to render and would hang
forever instead of appearing. Set up key auth first.

`--info=`/`--progress2` are avoided because the openrsync that ships with macOS
rejects them; `--protect-args` likewise doesn't exist there, so remote paths
containing spaces inherit rsync's own quoting behavior.

## What it actually does

0. **Optionally work out what to skip** (`--lean`), from the manifests the
   project actually has — and report anything a reinstall could not rebuild.
1. **rsync `SRC` -> `DEST`**, untouched (minus those excludes).
2. **Work out where rsync put things**, by applying rsync's own trailing-slash
   rule. This is used only to locate the session directories.
3. **rsync the session directory across** — transcripts, subagents, tool
   results, memory.
4. **Rewrite the absolute paths inside those sessions** so they resolve on the
   destination.
5. **Carry the rest of the project's state** — settings, prompt history and
   `/rewind` snapshots — so the destination doesn't treat it as a project it
   has never seen.
6. **Leave a manifest** so the next run can skip what has not changed.

### Where Claude Code keeps sessions

Under `$CLAUDE_CONFIG_DIR` (or `~/.claude` if unset), in
`projects/<encoded path>`, where the encoding replaces every non-alphanumeric
character with `-`, and — for paths whose encoded form exceeds 200 characters —
truncates and appends a hash:

```js
// from the Claude Code binary
function encode(p) {
  let s = p.replace(/[^a-zA-Z0-9]/g, "-")
  if (s.length <= 200) return s
  return s.slice(0, 200) + "-" + hash(p)
}
```

`/Users/me/src/app` therefore lives in `-Users-me-src-app`. Paths are
NFC-normalized first, which matters on macOS: the filesystem returns decomposed
names, so `é` arrives as `e` + a combining accent and would otherwise encode
differently from the composed form.

The path is also **realpath-resolved** before encoding, so a project reached
through a symlink (say `/home/me/dev -> /mnt/md0/dev`) is filed under the
target, not the link. tp-claude resolves symlinks on each machine to match — if
it didn't, teleporting into a symlinked directory would file the sessions where
Claude Code never looks, and `claude --resume` would come up empty.

Because the encoding is lossy — `/` and `-` both become `-` — two different
directories can share one session directory (`~/foo-bar` and `~/foo/bar`).
tp-claude refuses same-machine transfers that collide, since proceeding would
overwrite the original sessions.

### Differences between Claude Code releases

This layout is undocumented, so it was read out of the shipped binaries across
releases 2.1.90 – 2.1.257. Almost all of it is stable:

| behavior | across 2.1.90 … 2.1.257 |
| --- | --- |
| `[^a-zA-Z0-9]` becomes `-` | unchanged |
| length limit before truncating | 200, unchanged |
| NFC normalization | unchanged |
| `CLAUDE_CONFIG_DIR ?? ~/.claude` | unchanged |
| hash appended to over-long paths | **changed at 2.1.101** |

| releases | hash for over-long paths |
| --- | --- |
| up to 2.1.100 | `Bun.hash(p).toString(36)` |
| 2.1.101 onwards | `Math.abs(h).toString(36)` where `h = (h * 31 + charCode) | 0` |

Two machines either side of 2.1.101 therefore name the *same* long path
differently. tp-claude asks each end which version it runs and encodes with that
machine's rule — but only when a path is long enough to need the suffix, so
ordinary transfers never pay for the lookup. Below the limit every release
agrees, which covers essentially every real project.

The older `Bun.hash` needs `bun` on `PATH` to compute: it is wyhash with Bun's
own seeding, and guessing it wrong would silently point at a directory Claude
never reads. The newer hash is implemented directly and needs nothing. On a
machine with no Claude Code installed the current scheme is assumed and a note
is printed.

If Claude Code updates on one machine between runs, nothing needs to happen:
short paths encode identically on every release. For a path long enough to carry
a hash suffix the encoded name changes, the manifest stops matching, and the
project is resynced under its new name — leaving the previous directory behind
as an orphan to delete by hand.

Releases can be enumerated and fetched from the public distribution bucket,
which is how the table above was checked:

```sh
BASE=https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases
curl -s $BASE/stable                       # current stable version
curl -s $BASE/2.1.104/manifest.json        # per-version manifest
curl -s $BASE/2.1.104/darwin-arm64/claude  # the binary itself
```

The bucket serves objects but does not allow listing, so versions are found by
probing `manifest.json` for each candidate; numbering is sparse (2.1.95 does not
exist, for instance).

### What gets rewritten

Three rules, applied longest-first, to every `.jsonl`, `.json`, `.md` and
`.txt` under the session directory:

| rule | why |
| --- | --- |
| encoded directory name | appears inside the session index and cross-references |
| full project path | `cwd` and every file path in the transcript |
| `$HOME` | skills, config, other repos — anything referenced outside the project |

Matching stops at a path boundary, so rewriting `.../app` cannot maul a sibling
`.../app2`. Files are streamed line by line (transcripts reach hundreds of
megabytes) and replaced atomically, so an interrupted run never leaves a
half-written session.

### What travels, and what stays

Sessions are not the only thing Claude Code files under a project's path. This
is everything it keeps, and where each piece ends up:

| State | Where | Keyed by | Travels |
|---|---|:---:|:---:|
| Transcripts, subagents, tool results | `<config>/projects/<encoded>/` | encoded path | ✅ |
| Project memory (`memory/`) | `<config>/projects/<encoded>/` | encoded path | ✅ |
| Trust decision, `allowedTools`, MCP servers, `ignorePatterns` | `~/.claude.json` → `projects[path]` | absolute path | ✅ |
| Recalled prompts (up-arrow) | `<config>/history.jsonl` | absolute path per record | ✅ |
| `/rewind` file snapshots | `<config>/file-history/<session>/` | `sha256(abs file path)[:16]` | ✅ re-keyed |
| Project's own `.claude/` (settings, agents, commands) | inside the repo | — | ✅ via rsync³ |
| Per-run metrics (cost, tokens, durations) | `~/.claude.json` | absolute path | ❌ by choice¹ |
| Global config (`CLAUDE.md`, `settings.json`, skills, plugins) | `<config>/` | not per-project | ❌ by design² |
| Shell snapshots, paste cache, IDE locks | `<config>/` | machine/PID | ❌ machine-local |
| Empty session markers (`session-env/`) | `<config>/` | session | ❌ nothing in them |

<sub>¹ They describe the machine that produced them, not the project.
² Deliberately untouched — these are *your machine's* configuration, not the
project's, and overwriting them on the destination would be surprising. Sync
them separately if you want them to match.
³ Except `.claude/worktrees/` when `--no-worktrees` is passed.</sub>

`--lean`, `--no-worktrees` and `--exclude` narrow the **code** sync only. The
session directory is Claude's own data and is never filtered — a transcript
that happens to sit under a directory named `dist/` is not build output.

Two of these are worth explaining.

**`~/.claude.json` sits beside the config directory, not inside it**, so it
follows `$HOME` rather than `$CLAUDE_CONFIG_DIR`. Without its entry the
destination treats the project as one it has never seen: the trust dialog
reappears and every previously granted tool permission is asked for again.

**`/rewind` snapshots are named `sha256(<absolute file path>)[:16]@v<n>`.**
Copying them unchanged would orphan every one, because the destination's paths
hash differently — so tp-claude re-keys them to the new paths as it copies.
Snapshots of files that no longer exist are left behind; there is nothing to
re-key them against. When both sides are the same machine the originals are
copied rather than moved, so the source keeps its own history.

Merging into the two shared files is done carefully, since both are global:

- **Lists are unioned, not replaced or appended.** Replacing would silently
  revoke permissions granted on the destination; appending would add a
  duplicate on every run.
- **Nested objects merge key by key**, with the source winning ties.
- **History is compared by content, not by its serialized text**, since key
  order and spacing differ between writers and matching raw text would append a
  fresh copy of every entry each time.
- Both are rewritten atomically. A Claude Code running on the destination at
  that moment could still overwrite the change, so prefer an idle target.

### Skipping what can be rebuilt

`node_modules`, `vendor`, `target`, `.venv` and build output are usually most
of a project by size, and they are the least worth moving: they are derived
from files that *do* travel, and they are frequently the wrong architecture for
the destination. A mac's `node_modules` carries `darwin-arm64` binaries that a
Linux box cannot load, so copying them wastes the transfer and then breaks the
build with a confusing error rather than an honest missing-dependency one.

`--lean` skips them and prints the command that rebuilds them:

```
tp-claude --lean ~/dev/app  me@server:/home/me/dev/
```

Measured on real projects across several stacks:

| project | default | `--lean` |
| --- | --- | --- |
| pnpm monorepo (Next.js, turbo) | 8.3 GB / 113,753 files | 188 MB / 5,969 files |
| Next.js app (pnpm, drizzle) | 1.7 GB / 60,286 files | 10 MB / 1,224 files |
| Rust + React (Cargo, npm) | 2.6 GB / 23,722 files | 33 MB / 1,529 files |
| TS monorepo + SQLite state | 923 MB / 56,645 files | 93 MB / 4,236 files |
| Go + React + Python ETL | 3.8 GB / 89,399 files | 767 MB / 28,530 files |
| Laravel + npm (Composer) | 2.6 GB / 139,098 files | 1.2 GB / 6,446 files |
| Python + ML weights (`.venv`) | 1.7 GB / 54,634 files | 182 MB / 183 files |
| Python package (`pyproject.toml`) | 132 MB / 4,298 files | 2.5 MB / 489 files |
| Go CLI, nothing installed | 19.4 MB / 213 files | 19.4 MB / 210 files |

Most of what survives in the ML row is model weights — `.pt`, `.onnx`,
`.safetensors` files sitting beside the code. Nothing skips those: a virtualenv
is rebuilt by `pip`, but a downloaded checkpoint is not reproduced by any
package manager, so it travels. The same holds for live state — a SQLite file
and its WAL under a `.gitignore`d `.data/`, a directory of `.sql.gz` dumps —
which is why a `.gitignore` is read for corroboration rather than obeyed.

The last row is the honest case: a repo whose dependencies were never installed
has nothing to skip, and `--lean` costs it a survey and saves it nothing.

Two things keep this from destroying work.

**Rules are gated on a manifest.** The same directory name means different
things in different projects, so a rule only fires when something proves the
directory was generated:

| name | derived when | otherwise |
| --- | --- | --- |
| `vendor/` | beside a `composer.json` or `go.mod` | a directory somebody named vendor |
| `target/` | beside a `Cargo.toml` | a build target, an output folder, anything |
| `dist/` | beside any manifest | hand-written files someone distributes |
| `bin/` | never — no rule skips it | committed scripts, far more often than not |

Matching on the name alone would delete source.

**Rules are scoped by path**, because a directory that is *mostly* derived can
still hold something unrecoverable. Laravel's `storage/` is the sharpest case —
`storage/logs` and `storage/framework` regenerate, while `storage/app` is user
uploads that no reinstall brings back — so the patterns name the two, never
`storage` itself. The same reasoning keeps `/vendor/` anchored to the root
(a `resources/vendor/` of hand-written code is untouched) and leaves
`node_modules` unanchored (workspaces nest one per package, and all of them are
derived).

Multi-language repos get the union: a Go API with a React admin panel and a
Python ETL job matches the `go`, `node`, `python` and `build` rules at once, and
each anchored pattern is emitted per project directory, so a monorepo's
`services/api/vendor/` and `ui/dist/` both go while a sibling's do not.

| rule | needs | skips |
| --- | --- | --- |
| `node` | — | `node_modules/` |
| `php` | `composer.json` | `/vendor/` |
| `go` | `go.mod` | `/vendor/` |
| `rust` | `Cargo.toml` | `/target/` |
| `python` | `pyproject.toml`, `requirements.txt`, … | `.venv/`, `venv/`, `__pycache__/`, `*.pyc`, `.tox/`, `.mypy_cache/`, `.ruff_cache/` |
| `ruby` | `Gemfile` | `.bundle/`, `/vendor/bundle/` |
| `rails` | `config/application.rb` | `/tmp/cache/`, `/tmp/pids/`, `/tmp/sockets/`, `/log/` |
| `laravel` | `artisan` | `/bootstrap/cache/`, `/storage/framework/`, `/storage/logs/` |
| `build` | any manifest | `.turbo/`, `.next/`, `.nuxt/`, `.svelte-kit/`, `.parcel-cache/`, `/dist/`, `/build/`, `/out/` |
| `cache` | — | `.cache/`, `.npm/`, `.pytest_cache/`, `coverage/`, `.gradle/`, `.DS_Store`, PHP tool caches |

Drop a single rule with `--no-lean-rule=build`, or add your own patterns with
`--exclude`.

The two framework rules show where the line sits. Rails and Laravel both keep
regenerable state next to irreplaceable state — `tmp/cache` beside `public/assets`,
`storage/logs` beside `storage/app` — so each rule names the derived children
and never the parent. `public/assets` in particular is compiled output under
Rails and hand-written source elsewhere, so no rule touches it.

#### What `.gitignore` contributes, and what it doesn't

A `.gitignore` looks like a ready-made list of things not worth sending, and it
isn't: it answers *"should git track this?"*, whose answer covers three kinds of
file.

| in `.gitignore` | example | skipped |
| --- | --- | :---: |
| derived | `/public/build`, `.turbo/` | ✅ |
| secrets | `.env`, `*.key`, `.mcp.json`, credentials | ❌ the destination cannot run without them |
| local data | uploads, generated assets — often hundreds of MB | ❌ nothing regenerates them |

So entries are adopted only when the name itself says a tool produced them, and
never when it could name a credential. That lets a project's own `.gitignore`
contribute the build directories no generic rule knows about — a
framework-specific `/public/build`, say — while `.env` still travels. Anything
adopted this way is printed, so a skip is never silent.

Adoption is deliberately narrow, because a `.gitignore` entry carries no
evidence that the directory is derived — only that it is untracked:

- **Anchored and directory-only.** An adopted entry becomes `/entry/`, never a
  bare `entry` that would also match at every depth and match *files*. A
  hand-written `resources/vendor/`, and a file that merely shares a derived
  name, both survive.
- **Never a name the rules already gate.** `vendor`, `target`, `bin`, `out`,
  `tmp`, `obj` and `bundle` are skipped *only* when a manifest proves them
  derived, so they are never adopted from a `.gitignore` — that would route
  around the gate. `bin` is the most common Go/Java `.gitignore` entry and also
  the most common name for a directory of committed scripts.
- **Never `logs`.** The Laravel rule is scoped to `/storage/logs/` rather than
  `/storage/` for a reason; a bare `logs` would undo that at every depth, and
  an application audit trail is a record of events, not rebuildable output.
- **The whole name has to be conventional.** `build-cache` and `dist-prod` are
  adopted; `my_build_notes` is not — one derived word inside a phrase is
  somebody describing their own directory.
- **Negations follow git.** `!dist` drops the `/dist` exclusion, while
  `!dist/keep.js` does not: git cannot re-include a file underneath an excluded
  directory, so that line is a no-op there and stays one here.

An ambiguous entry like `/public/css` is left alone: it is build output in one
repo and hand-written in another, and under-skipping costs bandwidth while
over-skipping costs source.

#### Before it skips, it checks

Skipping is only safe when a package manager can rebuild what was skipped, so
`--lean` first reports what it could not:

- a `file:`/`link:`/`portal:` dependency resolving **outside** the synced tree
- `pnpm` `patchedDependencies`, whose patch files have to travel to match
- a Composer `path` repository pointing outside the tree
- a manifest with no lockfile above it — versions unpinned, so a reinstall may
  not reproduce the same tree
- a `.venv` with no `requirements.txt` or `pyproject.toml` declaring it

These are heuristics about someone else's project, so they are reported and the
transfer proceeds rather than being refused. A virtualenv is the weakest case
for rebuilding and the strongest for not copying: it hard-codes the source
machine's interpreter path, so it arrives broken either way.

Afterwards the reinstall command is printed — detected from the lockfiles
present, with `packageManager` breaking ties when a repo carries more than one:

```
>> done. resume with:  cd /home/me/dev/app && claude --resume

   rebuild the skipped directories with:

     cd /home/me/dev/app && pnpm install --frozen-lockfile
```

A repo with several ecosystems gets them chained in one line, so a Go API with
a React panel and a Python ETL job comes back with:

```
     cd /home/me/dev/app && npm ci && uv sync && go mod download
```

Ties are resolved rather than duplicated: `packageManager` picks between a
`pnpm-lock.yaml` and a stray `package-lock.json`, and a `uv.lock` or
`poetry.lock` suppresses a `requirements.txt` beside it, since that file is
usually an export of the lockfile rather than a second thing to install.

A manifest that brought no lockfile still gets the install it implies — a bare
`pyproject.toml` yields `pip install -e .`. That is weaker advice, and the
preflight says so, but skipping a dependency tree and then printing no way to
rebuild it would be worse.

It is printed and never run. Reinstalling is a long, network-bound build that
can fail on its own terms, and making it a side effect of a sync would leave a
transfer that already succeeded looking like it failed.

Excludes apply to the **code** sync only. The session directory is Claude's own
data, and a transcript that happens to sit under a directory named `dist/` is
not build output.

### The manifest

Step 4 necessarily makes the destination copies differ from their sources, so
rsync alone would resend and rewrite every session file on every run. tp-claude
leaves a `.tp-claude.run` file on the destination recording, per file, both the
**source** fingerprint it copied from and the **destination** fingerprint it
produced.

A file is skipped only while *both* still hold. Tracking only the source would
go blind to changes made on the destination; tracking both means work done on
either machine is noticed and reconciled. The manifest is discarded whenever
the mapping changes (different destination, different `$HOME`), and `--full`
ignores it entirely.

It identifies that mapping by a digest rather than by storing the paths, so the
destination never ends up holding a description of the source machine's layout.
Fingerprints are size and mtime — the same cheap check rsync itself trusts —
so nothing has to be re-read to decide what changed.

## Tests

```
python3 -m pytest
```

The suite runs the real script between temporary directories with
`$CLAUDE_CONFIG_DIR` redirected, so it never touches your own Claude data. The
encoding is checked against the reference implementation above via `bun` when
available, and skipped when not.

## Notes and limits

- **remote -> remote** is not supported; one side must be local.
- **`--delete` implies `--full`.** Pruning and incremental bookkeeping don't mix
  cleanly, so a delete run resends everything.
- **`--delete` prunes destination sessions.** If you have conversations on the
  destination that don't exist on the source, they are removed. It is off by
  default for exactly this reason.
- **Remote destination paths containing spaces** hit rsync's own quoting
  behavior. `--protect-args` would fix it but is absent from the openrsync that
  ships with macOS, so the behavior is inherited rather than papered over.
- **`--lean` is opt-in.** Without it nothing is skipped, so an existing
  transfer never changes shape. Its rules are heuristics about someone else's
  project: they are conservative by design, and `--dry-run -v` shows exactly
  what a run would leave behind before you trust it.
- **`--lean` does not run the reinstall for you.** The destination has the
  code and the lockfile but not the packages until you run the printed command.
- **`--lean` with `--delete` does not prune what it skipped.** rsync protects
  excluded paths from deletion, so a destination that already has its
  `node_modules` keeps it rather than having it pruned as "missing from the
  source".
- Teleporting **does not remove anything from the source**; both machines end
  up holding the project and its history.
- Session transcripts contain **everything you and Claude discussed**, including
  file contents and any secrets that passed through. Teleporting a project moves
  all of it. Treat the destination accordingly.
- The session layout is **undocumented and version-specific**. It has held from
  2.1.90 to 2.1.257, but Anthropic can change it; re-check the table above
  against a new release before trusting it.
