# MagicMirror_MusicLearningCompanion Marketplace

The list of community games for the MagicMirror_MusicLearningCompanion, the practice mirror for string players.
A mirror in Developer mode reads this list on its Marketplace card ("Available"), and can install, update and
remove the games in it.

This repository holds **the list** and **the SDK** (the kit to make a game: template, simulator, rules). Each
game lives in its author's own public repository; a game version is added here by a pull request, checked
automatically, reviewed by the group, and then signed.

**Want to make a game?** Start with [sdk/README.md](sdk/README.md), then follow
[CONTRIBUTING.md](CONTRIBUTING.md): twelve numbered steps from nothing to a game on every mirror.

## What is here

| path | what |
|---|---|
| `sdk/` | the SDK: [README](sdk/README.md) (the `mirror` object, the streams, the rules), `template/`, `simulator/`, `examples/`, `check.py` |
| `index.json` | the signed list the mirrors read (built by `tools/market_tool.py build-index`, never by hand) |
| `index.json.sig` | its Ed25519 signature: `{"alg": "ed25519", "key": "<key id>", "sig": "<base64>"}` |
| `packages/<id>/<version>.json` | one submitted game version: its manifest, the zip's address and sha256 ([entry schema](schemas/entry.schema.json)) |
| `packages/<id>/thumb.png` | optional tile picture (reviewed with the entry) |
| `blocked.json` | take-down list (see "Taking a game down"): mirrors hide these games and will not start them |
| `root/<N>.json` | the root of trust (once the group has made its keys): root keys, index keys, revoked keys |
| `keys/*.pub` | the public signing key(s). Before `root/1.json` exists, mirrors trust these directly |
| `schemas/` | [index](schemas/index.schema.json), [entry](schemas/entry.schema.json) and [manifest](schemas/manifest.schema.json) formats |
| `tools/check.py` | the package rules, the same as the mirror's and the SDK's `check.py` |
| `tools/market_tool.py` | make and check a zip or an entry, run the CI, build and sign the index, make a key |
| `.github/workflows/check.yml` | the automatic checks on every pull request |

## How a mirror trusts a game

1. It follows the root chain (`root/<N+1>.json` until there is none), then downloads `index.json` and
   `index.json.sig` (https only) and checks the signature with an index key the newest root names (never a revoked
   one). A bad signature, an unknown or revoked key, a list **older** (lower `serial`) than one it has already seen,
   or a list past its `expires` date is refused (an expired list installs nothing).
2. Every game in the list has its own signature over its id, version and the sha256 of its zip. The mirror
   downloads the zip, checks the sha256 and that signature.
3. It unpacks the zip into a staging folder (no links, nothing outside the folder, at most 400 files and 40 MB),
   runs the same rules as `tools/check.py`, checks that the zip's `manifest.json` is the reviewed one, and only then
   moves the game into place. One older version is kept for a rollback.
4. The game then runs like every other package: in its own process, with no network and no files, reading **sound
   and bow only**. It never gets the camera, the microphone or the player's pose.

Signing says *the group reviewed this exact file*. The mirror's sandbox is what keeps the player safe.

## Making a game

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: build it with the SDK, check it with `tools/check.py`, zip it
with `tools/market_tool.py make-zip`, publish the zip as a release in your own repository, run `make-entry`, then
open a pull request here that adds `packages/<your id>/<version>.json`.

## For maintainers

### The checklist for a submitted pull request

1. **The CI is green** (the "check" job). A fork's pull request may change only `packages/<id>/`, never edits a
   merged version file, and its zip must pass the same checks as the mirror. Never re-run it with secrets, never
   switch it to `pull_request_target`.
2. **The source is the author's own GitHub release** (`https://github.com/<author>/<repo>/releases/download/...`),
   in a public repository whose README and licence match the manifest. The id fits the author
   (`io.github.<author>...`) and is not taken by someone else.
3. **Read the code.** It reads only the streams it declares; no tricks around the checks (string-built URLs,
   `Qt.include` of anything outside, huge loops); no full-screen `Canvas` per frame; nothing that asks a child for
   personal data, links out or sells.
4. **Play it** in the simulator against the demo take and, if possible, on a mirror (Developer mode › Market ›
   Available, from a test index): `python3 sdk/simulator/simulate.py <unzipped folder> --fast --json`.
5. **The rules of a mirror game:** no score while playing; pause on an unsure reading, never a miss; short
   rounds; compares a player only with themselves; teaches something real and cannot be won by doing it wrong.
6. **Art and words:** licences as declared, sources named in the README; no faces except on characters, no
   clip-art filler; short, kind words.
7. **Two reviewers** look at every new game and version; **one maintainer** approves and merges (squash).
8. **Sign the list** right after merging (below), then check that `index.json` on `main` lists the version and
   tell the author in the pull request ("live; mirrors see it within about 5 minutes when the Available tab
   opens").

### Signing a new list

The private keys never enter this repository (and never a pull request, never CI). After merging reviewed
entries, on the maintainer's own machine, with the index key (today the development key of 12 Nov 2026, kept by
the maintainer; after the root keys exist, the encrypted index key):

    git pull
    python3 -m pip install cryptography
    python3 tools/market_tool.py build-index --key /offline/index_2026.pem --days 30
    python3 tools/market_tool.py verify-index
    python3 tools/market_tool.py ci
    git add index.json index.json.sig && git commit -m "index: serial N" && git push

`build-index` re-downloads every entry's zip, checks it again, signs each package and the whole list, and raises
`serial` by one. An entry whose zip cannot be downloaded or no longer matches its sha256 (a deleted release) is
printed as `SKIPPED` and **left out of the list**: read those lines, tell the author, and do not sign a list
that silently drops a game by accident. The list expires after `--days`: re-sign at least once a month even with
no new games (a calendar reminder), or mirrors stop installing from it.

The key in `keys/` today is the **development key** made on 12 Nov 2026. When the group makes its own key:
`python3 tools/market_tool.py keygen --passphrase --out <offline place>/marketplace_ed25519.pem --pub keys/marketplace_ed25519.pub`,
then rebuild the index and ship the new public key in the mirror (`mirror_qt/market_trust/`).

### Trust: root keys, the index key, rotation (R2b, Nov 2026)

There are two kinds of key:

* **Root keys** (three; any two sign): they say which index key(s) are valid. Kept offline by three holders, each a
  passphrase-encrypted PEM (or a hardware key). Never in this repository, never in CI.
* **The index key**: signs `index.json` and every package. Kept by the maintainer, passphrase-encrypted.

`root/<N>.json` = `{"signed": {"type": "root", "version": N, "expires": ..., "keys": {...},
"roles": {"root": {"keyids": [...], "threshold": 2}, "index": {"keyids": [...], "threshold": 1}}, "revoked": [...]},
"signatures": [{"key": ..., "sig": ...}]}`; the signatures cover `"magicmirror-root-v1\n"` + the canonical JSON of
`signed`. Root N+1 must be signed by two of root N's root keys **and** two of its own. Mirrors ship `root/1.json` and
follow the chain on every refresh, so a new root reaches every mirror without a new app.

#### Making the keys (once; the group, offline)

    python3 -m pip install cryptography
    # each of the three holders, on their own machine:
    python3 tools/market_tool.py keygen --passphrase --out /offline/root_roberto.pem --pub root_roberto.pub --comment "root, Roberto"
    # the maintainer: today's dev key becomes the first index key, encrypted
    python3 tools/market_tool.py encrypt-key --key data/keys/marketplace_ed25519.pem --out /offline/index_2026.pem
    python3 tools/market_tool.py make-root --version 1 --root-pub root_roberto.pub --root-pub root_ryan.pub \
        --root-pub root_backup.pub --threshold 2 --index-pub keys/marketplace_ed25519.pub --out root/1.json
    python3 tools/market_tool.py sign-root root/1.json --key /offline/root_roberto.pem     # holder 1
    python3 tools/market_tool.py sign-root root/1.json --key /offline/root_ryan.pem        # holder 2
    python3 tools/market_tool.py verify-root
    git add root/1.json && git commit -m "root 1"     # then copy it to the mirror: mirror_qt/market_trust/root.json

Back up every encrypted PEM to two places. A passphrase can be given in scripts as `MARKET_KEY_PASSPHRASE`.

#### What a schema 2 signature covers

`build-index` writes schema 2: the signature covers `"magicmirror-index-v2\n"` + the bytes, and the list carries
`expires` (`--days`, 30 by default) and `root` (the newest `root/<N>.json`, 0 before the first). Mirrors accept the
older schema 1 list (today's, serial 2) until it is re-signed.

#### Rotating or revoking the index key

    python3 tools/market_tool.py keygen --passphrase --out /offline/index_2027.pem --pub index_2027.pub
    python3 tools/market_tool.py make-root --version 2 --root-pub ... (the same three, or new ones) \
        --index-pub index_2027.pub --revoke <old index key id> --out root/2.json
    # two holders of root 1's keys AND two of root 2's keys run sign-root on root/2.json
    python3 tools/market_tool.py verify-root
    python3 tools/market_tool.py build-index --key /offline/index_2027.pem

Mirrors pick up `root/2.json` at their next refresh and refuse anything signed by the revoked key. Losing one root
key is fine (two still sign); make a new root that replaces it.

**If the index key leaks** (or its laptop is lost): do the rotation above the same day, with `--revoke` of the old
key id, and re-sign. Until `root/1.json` exists the only cure is a new key in `keys/` **and** a new mirror app
(`mirror_qt/market_trust/`), which is why the group should make the root keys early.

### Taking a game down

Add an entry to `blocked.json` and rebuild the index. Every field an entry gives must match; `minVersion` blocks only
the versions below it; an entry needs at least one of `id`, `sha256`, `author`:

    [{"id": "io.github.someone.game", "why": "plays a sound too loud"},
     {"id": "io.github.someone.game", "minVersion": "1.1.0", "why": "1.0.x crashes; 1.1.0 is fine"},
     {"id": "io.github.someone.game", "version": "1.0.2", "why": "this one version"},
     {"sha256": "<64 hex>", "why": "this exact zip"},
     {"author": "spam-games", "why": "every game of this author"}]

Mirrors hide matching versions and refuse to start an installed copy, even offline (from the cached signed list).

Steps: (1) add the entry with a short `why`; (2) `build-index` + `verify-index` + `ci`; (3) commit `blocked.json`,
`index.json`, `index.json.sig` together and push; (4) tell the author (an issue or their pull request), and why. Do
not delete the game's `packages/` files: the history shows what was reviewed. For a danger to children (not a bug),
sign at once; mirrors see it within about 5 minutes of their next refresh.

## Licence

The tools and this list: MIT (see `LICENSE`). Each game keeps its own licence, given in its manifest (MIT /
Apache-2.0 / BSD for code, CC BY 4.0 / CC0 for art).
