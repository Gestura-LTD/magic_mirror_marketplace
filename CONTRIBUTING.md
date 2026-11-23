# Publishing a game on the MagicMirror

Thank you for making a game for the practice mirror. This page takes you from nothing to a game that every
mirror can install, in twelve steps. You need a GitHub account, Python 3.9 or newer and `git`. Commands are
written for macOS / Linux; on Windows use PowerShell, type `py` where it says `python3`, and copy folders in
Explorer where it says `cp -r`.

**How it works in one sentence:** your game lives in **your own** GitHub repository as a release zip; this
repository holds only a small file that points at it; the review group checks it and signs the list that the
mirrors read.

## Make it

**1. Get the SDK.** Fork this repository on GitHub (the "Fork" button), then clone **your fork**:

    git clone https://github.com/<you>/magic_mirror_marketplace.git
    cd magic_mirror_marketplace
    python3 -m pip install PySide6 cryptography        # the simulator and the signature checks

(A virtual environment is a good idea: `python3 -m venv ~/.venvs/mirror-sdk` and activate it first.)

**2. Copy the template** into a folder of its own, named after your game's id (reverse-DNS, lower case,
usually `io.github.<you>.<game>`). The id is for ever: scores are kept under it.

    mkdir -p ~/games && cp -r sdk/template/com.example.mygame ~/games/io.github.you.mygame

Change `id`, `name`, `author`, `howTo` and `scoreIds` in its `manifest.json`, put your name in `LICENSE`,
and write the game in `main.qml`. [sdk/README.md](sdk/README.md) explains the `mirror` object, the streams
(field by field), the screen size and the rules of a mirror game.

**3. Play it in the simulator** (no mirror needed; a recorded practice take plays into your game):

    python3 sdk/simulator/simulate.py ~/games/io.github.you.mygame                 # a window, real time
    python3 sdk/simulator/simulate.py ~/games/io.github.you.mygame --fast --json   # must end "ok": true

**4. Check it** with the same rules the mirror uses:

    python3 tools/check.py ~/games/io.github.you.mygame          # must say OK

## Publish it

**5. Zip it.** Use the tool, not Finder's "Compress" or Explorer's "Send to": it leaves out `__MACOSX/`,
`.DS_Store` and other system files that make a zip fail, names it `<id>-<version>.zip`, and checks it:

    python3 tools/market_tool.py make-zip ~/games/io.github.you.mygame --out ~/games
    # OK      ~/games/io.github.you.mygame-0.1.0.zip
    # sha256  3f1c...

**6. Host the zip in your own repository.** Put the game folder in a public GitHub repository of yours
(e.g. `github.com/<you>/mygame`), then make a release whose tag is `v` + the version and attach the zip:
GitHub › your repository › Releases › "Draft a new release" › tag `v0.1.0` › drop in the zip › Publish. With
the [GitHub CLI](https://cli.github.com/) it is four lines:

    cd ~/games/io.github.you.mygame
    git init && git add . && git commit -m "My game 0.1.0"
    gh repo create mygame --public --source . --push
    gh release create v0.1.0 ../io.github.you.mygame-0.1.0.zip --notes "First version."

The zip's address is then

    https://github.com/<you>/mygame/releases/download/v0.1.0/io.github.you.mygame-0.1.0.zip

**Keep that release for ever.** The list is re-signed from the zips at their addresses; if the release or
the zip is deleted, your game drops out of the list at the next signing (mirrors that installed it keep it).
To withdraw a game, open an issue instead.

**7. Make the entry** (in your clone of this repository; a branch keeps your fork tidy; if your fork is
older than this repository, press "Sync fork" on GitHub and `git pull` first):

    git checkout -b add-mygame-0.1.0
    python3 tools/market_tool.py make-entry --zip ~/games/io.github.you.mygame-0.1.0.zip \
        --source https://github.com/<you>/mygame/releases/download/v0.1.0/io.github.you.mygame-0.1.0.zip \
        --thumb ~/games/tile.png --notes "First version."

It writes `packages/io.github.you.mygame/0.1.0.json` (your manifest, the address, the sha256) and, with
`--thumb`, `packages/io.github.you.mygame/thumb.png` (optional; a PNG of at most 512 kB, shown in a 16:9
tile, so 512 x 288 fits exactly: a drawing, or a crop of `simulate.py --shot end.png`). Then check it the way the
automatic checks will:

    python3 tools/market_tool.py check-entry packages/io.github.you.mygame/0.1.0.json        # downloads your release
    python3 tools/market_tool.py check-entry packages/io.github.you.mygame/0.1.0.json --zip ~/games/io.github.you.mygame-0.1.0.zip   # before the release is public

**8. Open a pull request.**

    git add packages/io.github.you.mygame
    git commit -m "Add io.github.you.mygame 0.1.0"
    git push -u origin add-mygame-0.1.0

then GitHub › your fork › "Compare & pull request". The rules for a pull request:

- it changes **only `packages/<your id>/`** (never `index.json`, `tools/`, `sdk/` or anyone else's game);
- one game version per pull request;
- a version file is never changed once merged: a fix is a new version (`0.1.1.json`), with a new zip and release;
- by opening it you confirm you have the right to publish everything in the zip under its licences.

## What happens next

**9. The automatic checks** run on your pull request within a few minutes (the "check" job; on your first
pull request a maintainer presses "Approve and run" first, a GitHub rule for new contributors): they download
your zip, compare the sha256, unpack it safely (no links, nothing outside the folder, at most 400 files and
40 MB), run `tools/check.py`, and compare the zip's `manifest.json` with your entry. A red cross shows the
reason in the job's log; fix it, push again, and the checks run again.

**10. The review.** A reviewer of the group plays your game and answers in the pull request (questions,
changes, or an approval). Two reviewers look at every new game and every new version; one maintainer's
approval merges it. What they look at:

- *Safety and privacy*: the game reads only what it declares (sound and bow only), never tries the network,
  other files or the mirror itself. A child's practice stays on the mirror.
- *The rules of a mirror game* ([sdk/README.md](sdk/README.md)): no score on screen while playing; pause,
  never punish, when the reading is unsure; short rounds; a player is compared only with themselves.
- *It teaches something real*, and cannot be won by doing the wrong thing (a "smooth tone" game won by
  playing so softly that the microphone hears nothing). Say in your README which skill it trains.
- *Weight*: no full-screen drawing every frame, small pictures.
- *Look and words*: readable on the dark glass; no faces except on characters, no clip-art filler; short,
  kind words a child can read; no ads, no links to buy things, no personal data asked.
- *Art*: yours or allowed by its licence; the README says where each picture, sound and font comes from.

**11. Signing.** After the merge a maintainer rebuilds `index.json` and signs it (`build-index`): it downloads
every zip again, checks it again, signs your exact zip and the whole list, and raises the list's `serial`.
Your game is live when `index.json` on `main` lists your id and version
([index.json](https://github.com/Gestura-LTD/magic_mirror_marketplace/blob/main/index.json); or `git pull` and
`python3 tools/market_tool.py verify-index`, which prints the number of packages).

**12. On every mirror.** GitHub's raw file cache can take about **5 minutes** to serve the new list. A mirror
fetches the list each time its Marketplace card's **Available** tab is opened (and when its internet comes
back), checks the signatures, and shows your game there in a coral "community" frame with your name; a player
installs it with one button. Today the Marketplace card is shown on mirrors in Developer mode.

## Updates, take-downs, licences

- **A new version:** raise `version` in `manifest.json`, then steps 3 to 8 again (new zip, new release
  `v0.2.0`, new entry `0.2.0.json`). Mirrors offer it as an update; one older version is kept for a rollback.
- **Taking a game down:** write to the maintainers (an issue is fine). A game can be taken down by adding it
  to `blocked.json`; mirrors then hide it and will not start an installed copy. Its author is told why.
- **Licences:** code under MIT (or Apache-2.0 / BSD-2-Clause / BSD-3-Clause), art under CC BY 4.0 (or CC0),
  as your manifest says. There are no paid games and the group takes no share; you may add a `sponsor` link.

Be kind in reviews and issues. The players are children and their teachers.
