# MagicMirror_MusicLearningCompanion SDK: make a game for the practice mirror

The MagicMirror_MusicLearningCompanion ("the mirror") is a tall screen (portrait 9:16, 2160 x 3840 pixels on
the glass) that a music student practises in front of. It listens to the instrument and, with a sensor on the
bow, feels the bow move. Games on the mirror turn that into play: hold a smooth bow, land a note in tune, keep
a steady beat.

This kit lets you make one of those games yourself, on your own laptop, without a mirror. When it is ready,
[../CONTRIBUTING.md](../CONTRIBUTING.md) takes you from here to every mirror, step by step.

A game is a small folder (a **package**) with a `manifest.json` and a QML file. QML is Qt's language for
moving pictures; if you know a little JavaScript you can read it.

## Make a game in 30 minutes

1. **Get the kit.** It lives in the Marketplace repository, in this `sdk/` folder:

       git clone https://github.com/Gestura-LTD/magic_mirror_marketplace.git
       cd magic_mirror_marketplace/sdk

   (Plan to submit? Fork the repository on GitHub first and clone your fork instead; see
   [../CONTRIBUTING.md](../CONTRIBUTING.md).)

2. **Install** Python 3.9 or newer, PySide6 (the simulator) and cryptography (the Marketplace's tools).
   Use `python3` (on Windows: `py`); a virtual environment keeps your system Python clean:

       python3 -m venv ~/.venvs/mirror-sdk && . ~/.venvs/mirror-sdk/bin/activate   # Windows: py -m venv ... and Scripts\activate
       python3 -m pip install PySide6 cryptography

3. **Copy the template** and give it your own id (reverse-DNS, lower case, at least three parts, e.g.
   `io.github.<your GitHub name>.<game>`). Keep your game **outside** the clone, in its own folder (later its
   own repository):

       mkdir -p ~/games && cp -r template/com.example.mygame ~/games/io.github.yourname.mygame

   Open `manifest.json` and change `id` (exactly the folder's name), `name`, `author`, `howTo` and
   `scoreIds`. Put your name in `LICENSE`.

4. **Check it** against the rules:

       python3 check.py ~/games/io.github.yourname.mygame

5. **Play it** with a recorded practice take, no mirror needed:

       python3 simulator/simulate.py ~/games/io.github.yourname.mygame

   A window opens and the recording plays into your game in real time. Every score your game sends is
   printed. `--fast` runs it with no waiting; `--shot end.png` saves a picture at the end; `--stream take.jsonl`
   plays your own recording. Headless (CI, SSH): put `QT_QPA_PLATFORM=offscreen` in front and add `--json`.
   The simulator runs your game in `simulator/game_host.py`, the same separate process the mirror uses (a
   byte-for-byte copy), so what is blocked on the mirror (see below) is blocked here too, and you see it printed.

6. **Change `main.qml`** and play it again. That is the whole loop.

The template, "Hold it", is about 50 lines: play any note and hold it in tune for two seconds. The
example `examples/io.github.robertoalonsotrillo.glass` is a real mirror game ("Glass": keep one long bow
smooth), ported into a package line for line.

## What your game gets: the `mirror` object

Your QML sees exactly one object from the mirror, `mirror`. Nothing else: not the mirror's settings, not
the teacher, not other games' scores.

| | |
|---|---|
| `mirror.d` | The latest reading, about 12 to 16 times a second. **Only the streams your manifest lists.** |
| `mirror.now()` | The time in seconds, wall-clock (like `Date.now() / 1000`). Use it instead of `Date.now()`: the simulator's `--fast` replays recorded time through it. Only differences between two `now()` values mean anything. |
| `mirror.score(id, value, detail)` | The end of a round. `value` from 0 to 1; `id` must be in your manifest's `scoreIds`; `detail` is a short text (60 characters are kept). |
| `mirror.setTarget(hz)` | The pitch the player is aiming at, so the mirror can record it. Needs `"target.pitch"` in `outputs`. |
| `mirror.setNoiseTarget({game, hnr})` | The same for tone-noise games. Needs `"target.noise"` in `outputs`. |
| `mirror.exit()` | Leave the game. |
| `mirror.sdk`, `mirror.packageId`, `mirror.streams` | Which SDK level, your id, what you declared. |

React to readings with `onDChanged` (see the template). Start your clocks at the first reading, not in
`Component.onCompleted` (that runs before any reading has arrived).

### The streams

A package reads **sound and the bow only**. Never the camera, the microphone's raw sound, or the
player's body. Declare what you read in `streams`; the mirror sends nothing else. `sound.pitch` arrives as
`mirror.d.pitch`, `sound.signal` as `mirror.d.signal`, and so on (the part after `sound.`). Any field can be
missing or `null`: always check before you use it (`typeof p.cents === "number"`).

| stream | what it is |
|---|---|
| `sound.pitch` | the note now (fields below) |
| `sound.signal` | is the instrument sounding, and how (fields below) |
| `sound.level` | loudness: `db` (dBFS, about -90..0), `above_room_db` (dB above the room's own noise), `dynamic` ("pp".."ff" once the player has set a range, else "") |
| `sound.tone` | tone colour and noise: `tone.noise.hnr_db` (higher = cleaner, about 0..30), `grain` (0..1, rougher = higher), `live` / `held` (a bow is sounding / just ended), `phase` ("attack", "sustain", "silent"); `tone.colour` |
| `sound.rhythm`, `sound.pulse`, `sound.tempo` | note starts and the beat |
| `sound.source`, `sound.source2` | what is sounding: `label` ("violin", "voice", "room", ...), `music` (true / false) |
| `sound.tuning` | the ruler the notes and cents are measured with: `a4` (Hz, e.g. 440.0), `system` ("equal", ...), `label` ("equal · 440") |
| `bow`, or `bow.stroke`, `bow.level`, `bow.lean`, `bow.straight`, `bow.mounted` | the bow sensor (only when a sensor is on the bow: check `bow.mounted`) |

**`d.pitch`** (one reading, the mirror's sound server):

| field | shape |
|---|---|
| `note` | the nearest note name: `"A4"`, `"C#5"`, `"F#3"`. Sharps only, written `#` (never `♯` or flats). `"—"` (an em dash) when there is no note. |
| `cents` | how far `f0_hz` is from that note, -50 .. +50 (negative = flat). Measured from the note **in the temperament and A4 of `d.tuning`** (equal temperament at 440 Hz unless the teacher changed it). `null` when there is no note. |
| `f0_hz` | the pitch in Hz, `null` when there is no note. |
| `conf` | how clear the pitch is, 0 .. 1. The mirror's own games treat **below 0.6 as "unclear"** (pause, do not judge). |
| `poly` | `true` when two notes sound at once (a double stop, a ringing string): the single-note fields are not meaningful then. |
| `speech` | `{"on": true, ...}` while someone is talking: no pitch is judged then. `{"on": false}` otherwise. |
| `trace` | the last 12 `[f0_hz, clarity]` pairs, 4 per audio block (about 62 a second), oldest first, `null` f0 = unvoiced; `trace_seq` counts blocks, so you can keep only what is new. For drawing a pitch line. |
| `verdict`, `cents_med`, `settled` | the note so far: a median with hysteresis ("in tune", "a little sharp", ...); steadier than `cents` under vibrato. |

**`d.signal`**: `sounding` (true / false: is the instrument playing above the room?) and `state`, one of
`"silent"`, `"pitched"` (a clear tone), `"unpitched"` (sound with no clear pitch: a scratch, a cough, a page
turn) and `"clipped"` (too loud for the microphone). Also `hnr_db` and `dbfs`.

A safe "is there one clear note?" test, the same as the mirror's own games:

    var p = d.pitch || {}, s = d.signal || {}
    var clear = s.sounding && s.state === "pitched" && !p.poly && !(p.speech && p.speech.on)
                && typeof p.cents === "number" && (p.conf || 0) >= 0.6
    // not clear = pause, never a miss

`simulator/streams/glass_demo.jsonl` shows the shapes: one reading per line, with `t` in seconds (made by
`simulator/make_demo_stream.py`). Any JSON-lines file in the same shape works with `--stream`, so you can
script the exact situation you want to test.

## The rules of a mirror game

These come from the mirror's own game rules. Reviewers check them.

1. **No score on screen while playing.** Show the score at the end of a round.
2. **Pause, never punish.** When the reading is unsure (no clear note, talking, too soft for the room),
   wait. Never count it as a miss.
3. **Short rounds with a clear end**, and one figure carried forward.
4. **It is a mirror.** Portrait, sparse, light on dark; the player sees themselves behind the game. Thin
   lines over filled areas. Your game's area is **734 x 1273 logical pixels** (the simulator's window). The
   mirror lays out a 1080 x 1920 logical screen on the 2160 x 3840 glass and shows games at a 1.4 scale inside
   the Marketplace frame, so one of your pixels is about 2.8 pixels on the glass: use shapes and text, and
   draw pictures at 3x their size on screen.
5. **Compare a player only with themselves**, never with another student.
6. **Be light.** No drawing the whole screen every frame (no big `Canvas` repainted per frame); the
   mirror listens and draws at the same time, and a slow game delays the sound.
7. **Text a child can read.** Short words, large type.

## What a package may not do

`check.py` refuses a package that does any of these, and so does the mirror:

- import anything but `QtQuick`, `QtQuick.Shapes`, `QtQuick.Effects`, `QtQuick.Particles`, `QtQml`, or
  its own folders;
- use the network (`XMLHttpRequest`, `WebSocket`, any `http://` address), `LocalStorage`, or files
  outside its own folder (`../`, `file://`);
- call `Qt.openUrlExternally`, `Qt.quit`, `Qt.createQmlObject`;
- ship anything but QML, JavaScript, JSON, pictures (PNG, WebP, JPG, SVG), sounds (OGG, WAV), fonts
  (TTF, OTF) and text; no programs or libraries;
- use `WorkerScript` (no background threads);
- be bigger than 40 MB or hold more than 400 files.

On the mirror (and in the simulator) every game also runs in its own process, `game_host.py`. There:

- a file outside your package folder, any `http(s)://` address and any picture provider simply do not load
  (they are reported as "blocked");
- the network is switched off;
- a script that runs for more than a second is stopped, and the game is closed with a short message;
- a game that uses too much memory (about 1.5 GB) or crashes is closed; the mirror itself carries on;
- `mirror.score()` is limited to 20 calls a second.

## The package folder

    io.github.yourname.mygame/      the folder is named after the id
      manifest.json                 required; manifest.schema.json describes every field
      main.qml                      the entry (manifest "entry")
      js/ qml/ art/ sfx/ fonts/     your own files, any folders
      thumb.png                     the tile picture, 512 x 288 PNG (optional; the tile is 16:9)
      LICENSE                       required: your code licence
      README.md                     how to play, which skill it trains, where the art came from

### The manifest's optional fields

| field | meaning |
|---|---|
| `description` | one or two sentences for the game's page |
| `homepage` | your repository (https) |
| `sponsor` | a GitHub Sponsors / Ko-fi / Open Collective page (https); shown as credit, the mirror takes no money |
| `thumb` | the tile picture in the package, 512 x 288 PNG (shown on the Installed tab) |
| `cover` | a portrait picture, 1080 x 1920 PNG. Reserved: checked, not shown yet |
| `levels` | how many levels the game has, 0..20 (0 = no levels). Shown as "Levels" on the game's page |
| `ageRange` | `[youngest, oldest]` in years, e.g. `[6, 12]`: for reviewers and for later filtering; not shown yet |
| `techniques` | technique ids from the mirror's etude list (e.g. `"rh.spiccato.controlled"`), so a lesson mode can recommend the game later; may be empty `[]` |
| `instruments` | any of `violin`, `viola`, `cello`, `bass`, `flute`, `voice`, `any` (shown as "For") |

## Sharing your game

[../CONTRIBUTING.md](../CONTRIBUTING.md) is the whole path, numbered: zip, host it as a release in your own
repository, make the entry, open a pull request, review, signing, live. The review group (the
MagicMirror_MusicLearningCompanion contributors) review the code and the art.

You can credit yourself and add a sponsor link; the mirror shows it on your game's tile. The
MagicMirror_MusicLearningCompanion contributors take no cut and sell nothing.

## Names and addresses

The product name, the credit line and the GitHub organisation and repository names are kept in one
place, `project.json` (the mirror reads the same file).

## Licences

Code: MIT (`LICENSE`). Art: CC BY 4.0 (`ART-LICENSE.md`). Your package chooses its own, from the list in
`ART-LICENSE.md`.
