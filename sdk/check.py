#!/usr/bin/env python3
"""check.py -- does my game package follow the MagicMirror_MusicLearningCompanion rules?

    python check.py path/to/io.github.you.mygame          # one package
    python check.py path/to/folder/*                       # several
    python check.py --json path/to/pkg                     # machine-readable

It checks the same things the mirror checks before it shows a package (and the same things the
Marketplace's review CI will check): manifest.json against schema 1 (see manifest.schema.json), the
file types, the size, the QML import allow-list, and text a package must not contain (no `bridge`,
no network, no paths outside its own folder).

Exit 0 when every package given is valid, 1 otherwise. Python 3.9+, standard library only.
To run the game itself against a recorded stream, use simulator/simulate.py.

This file's rules are a copy of the mirror's own (mirror_qt/market.py); a test in the mirror's repo
(test_mk0_market.py) fails if the two ever disagree.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SDK_VERSION = 1
SCHEMA_VERSION = 1
MAX_BYTES = 40 * 1024 * 1024          # at most 40 MB per package
MAX_FILES = 400

# what a package may read: the sound tick's top-level blocks and the bow's fields
SOUND_STREAMS = ("pitch", "tuning", "signal", "level", "tone", "rhythm", "source", "source2", "pulse", "shape",
                 "tempo", "goodness", "quality", "instrument")
BOW_STREAMS = ("stroke", "level", "lean", "straight", "mounted", "t")
STREAMS = tuple(["sound." + s for s in SOUND_STREAMS] + ["bow"] + ["bow." + s for s in BOW_STREAMS])
# never (packages read sound + bow only)
FORBIDDEN_STREAM_HEADS = ("pose", "body", "hands", "face", "camera", "video", "frame", "mic", "audio", "raw")
OUTPUTS = ("target.pitch", "target.noise")
PERMISSIONS: tuple[str, ...] = ()     # none in v1: no network, no files, no storage yet
KINDS = ("game",)
CODE_LICENSES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause")
ART_LICENSES = ("CC-BY-4.0", "CC0-1.0")
INSTRUMENTS = ("violin", "viola", "cello", "bass", "flute", "voice", "any")

ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){2,}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SCORE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

ALLOWED_EXT = {".qml", ".js", ".json", ".png", ".webp", ".jpg", ".jpeg", ".svg", ".ogg", ".wav", ".ttf", ".otf",
               ".md", ".txt", ""}
# QML modules a package may import; a quoted import must be a folder inside the package
ALLOWED_IMPORTS = ("QtQuick", "QtQuick.Shapes", "QtQuick.Effects", "QtQuick.Particles", "QtQml")
# text that must not appear in a package's QML / JS (what cannot be removed from the JS global is
# grepped for; what reaches the app is shadowed AND grepped for)
FORBIDDEN_TEXT = (
    (re.compile(r"\bbridge\b"), "uses `bridge` (packages get only `mirror`)"),
    (re.compile(r"XMLHttpRequest"), "uses XMLHttpRequest (no network in packages)"),
    (re.compile(r"WebSocket"), "uses WebSocket (no network in packages)"),
    (re.compile(r"Qt\.openUrlExternally"), "calls Qt.openUrlExternally"),
    (re.compile(r"Qt\.quit|Qt\.exit"), "calls Qt.quit / Qt.exit"),
    (re.compile(r"Qt\.createQmlObject"), "calls Qt.createQmlObject"),
    (re.compile(r"LocalStorage"), "uses LocalStorage (storage is phase 1, through mirror.store)"),
    (re.compile(r"WorkerScript"), "uses WorkerScript (no background threads in packages)"),
    (re.compile(r"\b(?:https?|ftp|file|qrc|image)://", re.I), "names a URL outside the package (http, file, qrc, image://)"),
    (re.compile(r"\.\./"), "reaches outside its own folder (../)"),
    (re.compile(r"liquidDisc|liquidBudget|startFullScreen"), "names a mirror context property"),
)


def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def check_manifest(m) -> list[str]:
    """The manifest alone (schema v1, sdk/manifest.schema.json says the same). [] = valid."""
    if not isinstance(m, dict):
        return ["manifest.json is not a JSON object"]
    why: list[str] = []
    known = {"schema", "id", "version", "sdk", "kind", "name", "author", "homepage", "license", "artLicense",
             "entry", "instruments", "streams", "outputs", "permissions", "scoreIds", "howTo", "levels",
             "ageRange", "techniques", "sponsor", "thumb", "cover", "description"}
    for k in ("schema", "id", "version", "sdk", "kind", "name", "author", "license", "artLicense", "entry", "streams",
              "scoreIds"):
        if k not in m:
            why.append(f"missing `{k}`")
    extra = sorted(set(m) - known)
    if extra:
        why.append("unknown field(s): " + ", ".join(extra))
    if "schema" in m and m["schema"] != SCHEMA_VERSION:
        why.append(f"`schema` must be {SCHEMA_VERSION}")
    if "id" in m and not (isinstance(m["id"], str) and ID_RE.match(m["id"])):
        why.append("`id` must be reverse-DNS, lower case (e.g. io.github.you.mygame)")
    if "version" in m and not (isinstance(m["version"], str) and SEMVER_RE.match(m["version"])):
        why.append("`version` must be semver x.y.z")
    if "sdk" in m and not (isinstance(m["sdk"], int) and not isinstance(m["sdk"], bool) and 1 <= m["sdk"]):
        why.append("`sdk` must be a whole number >= 1")
    elif isinstance(m.get("sdk"), int) and m["sdk"] > SDK_VERSION:
        why.append(f"needs sdk {m['sdk']}; this mirror has sdk {SDK_VERSION}")
    if "kind" in m and m["kind"] not in KINDS:
        why.append("`kind` must be " + " or ".join(KINDS) + " (modules come later)")
    for k in ("name", "author"):
        if k in m and not (isinstance(m[k], str) and 0 < len(m[k].strip()) <= 60):
            why.append(f"`{k}` must be text, 1-60 characters")
    if "license" in m and m["license"] not in CODE_LICENSES:
        why.append("`license` (code) must be one of " + ", ".join(CODE_LICENSES))
    if "artLicense" in m and m["artLicense"] not in ART_LICENSES:
        why.append("`artLicense` must be one of " + ", ".join(ART_LICENSES))
    if "entry" in m and not (isinstance(m["entry"], str) and re.match(r"^[A-Za-z0-9_\-]+\.qml$", m["entry"])):
        why.append("`entry` must be a .qml file in the package's top folder")
    if "streams" in m:
        st = m["streams"]
        if not _strs(st) or not st:
            why.append("`streams` must be a non-empty list of names")
        else:
            for s in st:
                head = s.split(".")[0].lower()
                if head in FORBIDDEN_STREAM_HEADS:
                    why.append(f"stream `{s}`: packages read sound and bow only, never camera, microphone or pose")
                elif s not in STREAMS:
                    why.append(f"unknown stream `{s}`")
    if "outputs" in m:
        if not _strs(m["outputs"]) and m["outputs"] != []:
            why.append("`outputs` must be a list of names")
        else:
            for o in m["outputs"]:
                if o not in OUTPUTS:
                    why.append(f"unknown output `{o}` (sdk 1 has " + ", ".join(OUTPUTS) + ")")
    if "permissions" in m:
        if m["permissions"] != []:
            why.append("`permissions` must be empty in sdk 1 (no network, no files, no storage)")
    if "scoreIds" in m:
        sid = m["scoreIds"]
        if not _strs(sid) or not sid or not all(SCORE_ID_RE.match(x) for x in sid):
            why.append("`scoreIds` must list 1+ short lower-case ids")
    if "instruments" in m:
        if not _strs(m["instruments"]) or any(x not in INSTRUMENTS for x in m["instruments"]):
            why.append("`instruments` must be from " + ", ".join(INSTRUMENTS))
    for k in ("homepage", "sponsor"):
        if k in m and not (isinstance(m[k], str) and m[k].startswith("https://")):
            why.append(f"`{k}` must be an https:// address")
    for k in ("thumb", "cover"):
        if k in m and not (isinstance(m[k], str) and re.match(r"^[A-Za-z0-9_\-]+\.png$", m[k])):
            why.append(f"`{k}` must be a .png in the package's top folder")
    if "howTo" in m and not isinstance(m["howTo"], str):
        why.append("`howTo` must be text")
    if "levels" in m and not (isinstance(m["levels"], int) and not isinstance(m["levels"], bool) and 0 <= m["levels"] <= 20):
        why.append("`levels` must be 0-20")
    if "techniques" in m and not _strs(m["techniques"]) and m["techniques"] != []:
        why.append("`techniques` must be a list of taxonomy ids")
    return why


def check_files(folder: Path, m: dict) -> list[str]:
    """The package's files: types, size, the entry, the import allow-list and the forbidden text."""
    why: list[str] = []
    total, n = 0, 0
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            why.append(f"{p.relative_to(folder)}: links are not allowed")
            continue
        if not p.is_file():
            continue
        n += 1
        total += p.stat().st_size
        rel = p.relative_to(folder).as_posix()
        if p.suffix.lower() not in ALLOWED_EXT:
            why.append(f"{rel}: file type {p.suffix} is not allowed (QML, JS, pictures, sounds, fonts only)")
            continue
        if p.name == "qmldir" and re.search(r"^\s*(optional\s+)?plugin\b",
                                             p.read_text(encoding="utf-8", errors="replace"), re.M):
            why.append(f"{rel}: a qmldir may not name a plugin (no native code)")
            continue
        if p.suffix.lower() in (".qml", ".js"):
            text = p.read_text(encoding="utf-8", errors="replace")
            for rx, msg in FORBIDDEN_TEXT:
                if rx.search(text):
                    why.append(f"{rel}: {msg}")
            for line in text.splitlines():
                s = line.strip()
                if not s.startswith("import ") and not s.startswith(".import "):
                    continue
                mq = re.match(r'^\.?import\s+"([^"]+)"', s)
                if mq:
                    target = mq.group(1)
                    if target.startswith("/") or ".." in target or ":" in target:
                        why.append(f"{rel}: import \"{target}\" is outside the package")
                    continue
                mod = s.split()[1]
                if mod not in ALLOWED_IMPORTS:
                    why.append(f"{rel}: import {mod} is not on the allow-list (" + ", ".join(ALLOWED_IMPORTS) + ")")
    if n > MAX_FILES:
        why.append(f"{n} files (at most {MAX_FILES})")
    if total > MAX_BYTES:
        why.append(f"{total / 1e6:.1f} MB (at most {MAX_BYTES / 1e6:.0f} MB)")
    entry = m.get("entry")
    if isinstance(entry, str) and not (folder / entry).is_file():
        why.append(f"entry {entry} is missing")
    for k in ("thumb", "cover"):
        if isinstance(m.get(k), str) and not (folder / m[k]).is_file():
            why.append(f"{k} {m[k]} is missing")
    if not (folder / "LICENSE").is_file():
        why.append("LICENSE is missing")
    return why


def validate(folder: Path) -> tuple[dict, list[str]]:
    """(manifest or {}, reasons). No reasons = the package may be loaded."""
    folder = Path(folder).resolve()
    mf = folder / "manifest.json"
    if not mf.is_file():
        return {}, ["no manifest.json"]
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"manifest.json does not parse: {exc}"]
    why = check_manifest(m)
    if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id") != folder.name:
        why.append(f"the folder must be named after the id ({m.get('id')})")
    if isinstance(m, dict):
        why += check_files(folder, m)
    return (m if isinstance(m, dict) else {}), why


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    results = []
    for p in paths:
        m, why = validate(Path(p))
        results.append({"package": str(p), "id": m.get("id", ""), "ok": not why, "why": why})
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(("OK      " if r["ok"] else "REFUSED ") + r["package"])
            for w in r["why"]:
                print("        - " + w)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
