# Hold it (template)

Copy this folder out of the SDK, rename it to your own id (for example `io.github.yourname.mygame`), and change
`manifest.json` to match (`id` = the folder's name). Then, from the SDK's folder:

    python3 check.py ~/games/io.github.yourname.mygame                       # the rules
    python3 simulator/simulate.py ~/games/io.github.yourname.mygame --fast   # play the demo take into it

The game: play any note and hold it within 10 cents for 2 seconds. Reads `sound.pitch` and
`sound.signal` only.

Replace this README with your own: how to play, which skill the game trains, and where every picture, sound
and font came from.
