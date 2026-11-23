# Contributing a game

The whole path, from this kit to every mirror, is one numbered page:
[../CONTRIBUTING.md](../CONTRIBUTING.md) (zip with `make-zip`, a release in your own repository, `make-entry`,
a pull request that adds `packages/<your id>/<version>.json`, the checks, the review, signing, live).

Before you start that page, two things here must pass:

    python3 check.py ~/games/io.github.you.mygame                                    # says OK
    QT_QPA_PLATFORM=offscreen python3 simulator/simulate.py ~/games/io.github.you.mygame --fast --json   # "ok": true

What the reviewers look at is in the same page, step 10. Be kind in reviews and issues: the players are
children and their teachers.

Improvements to the SDK itself (the simulator, the docs, the template) are welcome as pull requests too; they
are reviewed by the maintainers, and `simulator/game_host.py` must stay a byte-for-byte copy of the mirror's own.
