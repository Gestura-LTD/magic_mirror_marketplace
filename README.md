# MagicMirror_MusicLearningCompanion Marketplace

The list of community games for the MagicMirror_MusicLearningCompanion, the practice mirror for string players.
A mirror in Developer mode reads this list on its Marketplace card ("Available"), and can install, update and
remove the games in it.

This repository holds **the list only**. Each game lives in its author's own public repository; a game version is
added here by a pull request, checked automatically, reviewed by the group, and then signed.

## What is here

| path | what |
|---|---|
| `index.json` | the signed list the mirrors read (built by `tools/market_tool.py build-index`, never by hand) |
| `index.json.sig` | its Ed25519 signature: `{"alg": "ed25519", "key": "<key id>", "sig": "<base64>"}` |
| `packages/<id>/<version>.json` | one submitted game version: its manifest, the zip's address and sha256 ([entry schema](schemas/entry.schema.json)) |
| `packages/<id>/thumb.png` | optional tile picture (reviewed with the entry) |
| `blocked.json` | take-down list `[{"id": ..., "why": ...}]`: mirrors hide these games and will not start them |
| `keys/*.pub` | the public signing key(s). Mirrors carry the same key and check everything offline |
| `schemas/` | [index](schemas/index.schema.json), [entry](schemas/entry.schema.json) and [manifest](schemas/manifest.schema.json) formats |
| `tools/check.py` | the package rules, the same as the mirror's and the SDK's `check.py` |
| `tools/market_tool.py` | check a zip or an entry, run the CI, build and sign the index, make a key |
| `.github/workflows/check.yml` | the automatic checks on every pull request |

## How a mirror trusts a game

1. It downloads `index.json` and `index.json.sig` and checks the signature with the public key it carries. A bad
   signature, an unknown key, or a list **older** (lower `serial`) than one it has already seen is refused as a whole.
2. Every game in the list has its own signature over its id, version and the sha256 of its zip. The mirror
   downloads the zip, checks the sha256 and that signature.
3. It unpacks the zip into a staging folder (no links, nothing outside the folder, at most 400 files and 40 MB),
   runs the same rules as `tools/check.py`, checks that the zip's `manifest.json` is the reviewed one, and only then
   moves the game into place. One older version is kept for a rollback.
4. The game then runs like every other package: in its own process, with no network and no files, reading **sound
   and bow only**. It never gets the camera, the microphone or the player's pose.

Signing says *the group reviewed this exact file*. The mirror's sandbox is what keeps the player safe.

## Making a game

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: build it with the SDK, check it with `tools/check.py`, publish a
release zip in your own repository, then open a pull request here that adds `packages/<your id>/<version>.json`.

## For maintainers: signing a new list

The private key never enters this repository (and never a pull request). After merging reviewed entries:

    pip install cryptography
    python tools/market_tool.py build-index --key /path/to/marketplace_ed25519.pem
    python tools/market_tool.py verify-index
    git add index.json index.json.sig && git commit -m "index: serial N" && git push

`build-index` re-downloads every entry's zip, checks it again, signs each package and the whole list, and raises
`serial` by one. To take a game down, add it to `blocked.json` and rebuild.

The key in `keys/` today is the **development key** made on 12 Nov 2026. When the group makes its own key:
`python tools/market_tool.py keygen --out <offline place>/marketplace_ed25519.pem --pub keys/marketplace_ed25519.pub`,
then rebuild the index and ship the new public key in the mirror (`mirror_qt/market_trust/`).

## Licence

The tools and this list: MIT (see `LICENSE`). Each game keeps its own licence, given in its manifest (MIT /
Apache-2.0 / BSD for code, CC BY 4.0 / CC0 for art).
