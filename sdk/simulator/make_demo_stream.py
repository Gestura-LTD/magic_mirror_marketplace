#!/usr/bin/env python3
"""Writes streams/glass_demo.jsonl: a SCRIPTED 20 s practice take, made up, no person's recording.

Three bows on the violin, 16 readings a second, in the mirror's reading format (only the blocks the
example games read, with the field shapes of the real sound server: signal.state is one of silent /
clipped / pitched / unpitched; pitch.note is "A4" / "C#5" or "\u2014" when there is no note; f0_hz and
cents are null when there is no note): a 6 s smooth bow on A4, a 2 s bow (too short for Glass), and an
8 s bow on D5 with a rough patch in the middle. Silence between them.

    python3 make_demo_stream.py            # rewrites streams/glass_demo.jsonl
"""
import json
import math
from pathlib import Path

HZ = 16
A4 = 440.0
NOTE_MIDI = {"A4": 69, "D5": 74, "E5": 76}


def hz(note, cents):
    return round(A4 * 2 ** ((NOTE_MIDI[note] - 69 + cents / 100.0) / 12), 2)
OUT = Path(__file__).resolve().parent / "streams" / "glass_demo.jsonl"


def silent(t):
    return {"t": round(t, 4), "signal": {"sounding": False, "state": "silent"},
            "level": {"db": -62.0, "above_room_db": 2.0},
            "tone": {"noise": {"live": False, "held": False, "phase": "silent"}},
            "source": {"label": "room", "music": False},
            "pitch": {"f0_hz": None, "conf": 0.12, "note": "\u2014", "cents": None, "poly": False,
                      "speech": {"on": False}}}


def bow_end(t, hnr):
    r = silent(t)
    r["tone"]["noise"] = {"live": False, "held": True, "phase": "silent", "hnr_db": hnr}
    return r


def playing(t, t0, note, cents, hnr):
    phase = "attack" if t - t0 < 0.2 else "sustain"
    grain = max(0.0, min(1.0, (25 - hnr) / 25))
    return {"t": round(t, 4), "signal": {"sounding": True, "state": "pitched"},
            "level": {"db": -24.0, "above_room_db": 32.0},
            "tone": {"noise": {"live": True, "held": False, "phase": phase, "hnr_db": hnr, "grain": round(grain, 3),
                               "word": "clean" if hnr >= 15 else "rough"}},
            "source": {"label": "violin", "music": True},
            "pitch": {"f0_hz": hz(note, cents), "conf": 0.95, "note": note, "cents": round(cents, 1), "poly": False,
                      "speech": {"on": False}}}


def main():
    rows, t, dt = [], 0.0, 1.0 / HZ
    while t < 1.0:
        rows.append(silent(t)); t += dt
    t0 = t
    while t < 7.0:                                    # bow A: 6 s, smooth
        rows.append(playing(t, t0, "A4", 4 * math.sin(t * 5), 19.0)); t += dt
    rows.append(bow_end(t, 19.0)); t += dt
    while t < 8.0:
        rows.append(silent(t)); t += dt
    t0 = t
    while t < 10.0:                                   # bow B: 2 s, too short for Glass
        rows.append(playing(t, t0, "E5", 2.0, 22.0)); t += dt
    rows.append(bow_end(t, 22.0)); t += dt
    while t < 11.0:
        rows.append(silent(t)); t += dt
    t0 = t
    while t < 19.0:                                   # bow C: 8 s, a rough patch at 14.0-14.6 s
        hnr = 13.0 if 14.0 <= t < 14.6 else 20.0
        rows.append(playing(t, t0, "D5", 15.0 if t - t0 < 1 else 3.0, hnr)); t += dt
    rows.append(bow_end(t, 20.0)); t += dt
    while t < 20.0:
        rows.append(silent(t)); t += dt
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} readings -> {OUT}")


if __name__ == "__main__":
    main()
