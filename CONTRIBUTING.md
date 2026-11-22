# Submitting a game

Thank you for making a game for the practice mirror. This page says how a game gets from your repository onto the
mirrors.

## 1. Make and check the game

Build it with the MagicMirror_MusicLearningCompanion SDK (the template, the simulator and the rules are there).
Then check it with the same rules the mirror uses:

    python tools/check.py path/to/io.github.yourname.mygame

and play it through the simulator against a recorded take (`simulator/simulate.py ... --fast --json` ends with
`"ok": true`).

## 2. Publish a release zip in your own repository

Zip the package folder (either its files at the top of the zip, or the one folder named after your id):

    cd path/to && zip -r io.github.yourname.mygame-1.0.0.zip io.github.yourname.mygame

Attach it to a GitHub release (tag `v1.0.0`) of **your own public repository**. Check the zip and get its sha256:

    python tools/market_tool.py check-zip io.github.yourname.mygame-1.0.0.zip

The zip must have: no links, no paths outside the folder, at most 400 files, at most 40 MB unpacked, a `LICENSE`,
and only QML, JS, JSON, pictures, sounds, fonts and text.

## 3. Open a pull request here

Add **one file**, `packages/<your id>/<version>.json`:

    {
      "manifest": { ...exactly your manifest.json... },
      "source": "https://github.com/<you>/<repo>/releases/download/v1.0.0/io.github.yourname.mygame-1.0.0.zip",
      "sha256": "<the zip's sha256>",
      "thumb": "thumb.png",
      "notes": "First version."
    }

`thumb` is optional: a PNG beside the file (at most 512 kB; 512 x 288 looks best on the tile). Check your entry
before you push:

    python tools/market_tool.py check-entry packages/io.github.yourname.mygame/1.0.0.json

A new version is a new file (`1.1.0.json`); never edit a file that is already in the list.

## 4. What happens next

1. **The automatic checks** (`.github/workflows/check.yml`) download your zip, compare the sha256, unpack it
   safely, run `tools/check.py`, and compare the zip's manifest with your entry.
2. **The review.** The group looks at every new game and every new version. The review is about the player:
   - *Safety and privacy*: the game reads only what it declares (sound and bow only), never tries the network,
     other files or the mirror itself.
   - *The rules of a mirror game*: no score on screen while playing; pause, never punish, when the reading is
     unsure; short rounds; a player is compared only with themselves.
   - *It teaches something real*, and cannot be won by doing the wrong thing.
   - *Art*: yours or allowed by its licence; the README says where each picture, sound and font comes from.
3. **Merge and sign.** A maintainer merges, rebuilds `index.json` and signs it. Mirrors see the game the next time
   their Marketplace card opens the "Available" tab. Your game shows in a coral "community" frame with your name.

## Licences

Code under MIT (or Apache-2.0 / BSD-2-Clause / BSD-3-Clause), art under CC BY 4.0 (or CC0), as your manifest says.
By opening a pull request you confirm you have the right to publish everything in the zip under those licences.

## Taking a game down

Write to the maintainers (an issue is fine). A game can be taken down by adding it to `blocked.json`; mirrors then
hide it and will not start an installed copy.
