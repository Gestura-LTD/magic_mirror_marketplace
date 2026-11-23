#!/usr/bin/env python3
"""simulate.py -- play a recorded sound stream into a game package, without a mirror.

    python3 simulate.py path/to/mypackage                                  # a window, the demo stream, real time
    python3 simulate.py path/to/mypackage --stream my_take.jsonl           # your own recording
    python3 simulate.py path/to/mypackage --fast --shot end.png            # no waiting; a picture at the end
    QT_QPA_PLATFORM=offscreen python3 simulate.py pkg --fast --json        # headless, for CI

What it does, the same way the mirror does it:
  * checks the package first (check.py) and refuses an invalid one;
  * runs the game in the SAME separate process the mirror uses, game_host.py (a copy of the mirror's own):
    its own QML engine with a single object, `mirror` (sdk 1); a fence that blocks every file outside the
    package and every network address; a watchdog that stops a game that freezes or eats memory;
  * gives `mirror.d` ONLY the streams the manifest declares (filtered here and again in the host);
  * replays the stream: one JSON object per line, each with "t" (seconds) and the reading's blocks
    ("pitch", "signal", "tone", "level", "bow", ...). In --fast mode mirror.now() follows the
    stream's "t", so a 60 s recording runs in a second and scores the same. mirror.now() is wall-clock
    seconds (like Date.now() / 1000) on the mirror; the simulator keeps it so in both modes: the stream's
    "t" is shifted to start at the wall-clock moment the first reading is sent, so a time taken in
    Component.onCompleted (before any reading) and times taken in onDChanged are on one clock. Printed
    event times are seconds of the stream ("load" = sent while the game was loading);
  * prints every score and target the game sends, everything the host blocked, and every QML warning.
    Exit 1 on a QML error, an invalid package, a game that froze / crashed / ran out of memory, or a
    score id not in the manifest.

Needs Python 3.9+ and PySide6 (python3 -m pip install PySide6). Window size: the game's area on the mirror,
734 x 1273 LOGICAL pixels. The glass is 2160 x 3840 physical pixels (portrait 9:16); the mirror lays out a
1080 x 1920 logical screen on it (2 physical pixels each) and draws a game at a 1.4 scale inside the
Marketplace frame, so one game pixel is about 2.8 physical pixels: use shapes, text and pictures drawn at 3x.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import check  # noqa: E402  (the rules, ../check.py)

from PySide6.QtCore import (QCoreApplication, QElapsedTimer, QEventLoop, QProcess, QTimer)  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402

GAME_W, GAME_H = 734, 1273
DEMO = HERE / "streams" / "glass_demo.jsonl"
HOST = HERE / "game_host.py"          # an exact copy of the mirror's mirror_qt/game_host.py
HOST_WORDS = {3: "used too much memory", 4: "froze (a script ran for more than 1 s)", 2: "did not load"}


def filtered(tick: dict, streams: list[str]) -> dict:
    """The declared streams only, exactly as the mirror sends them."""
    out: dict = {}
    if "seq" in tick:
        out["seq"] = tick["seq"]
    whole = "bow" in streams
    for s in streams:
        if s.startswith("sound."):
            k = s[6:]
            if k in tick and k != "bow":
                out[k] = tick[k]
        elif s == "bow":
            if "bow" in tick:
                out["bow"] = tick["bow"]
        elif s.startswith("bow.") and not whole:
            f, b = s[4:], tick.get("bow")
            if isinstance(b, dict) and f in b:
                out.setdefault("bow", {})[f] = b[f]
    return out


class Link:
    """The simulator's end of the host's socket: a tiny stand-in for the mirror's side (mirror_qt/market.py)."""

    def __init__(self, manifest: dict, log, warn):
        self.m = manifest
        self.log, self.warn = log, warn
        self.sock = None
        self.buf = bytearray()
        self.token = secrets.token_hex(8)
        self.hello = self.ready = self.exited = False
        self.error = ""
        self.blocked: list[dict] = []
        self.bad_score_ids: list[str] = []
        self.answers: dict = {}
        self.seq = 0
        self.base = None        # wall-clock time the first reading was sent (None = still loading)
        self.t0 = 0.0           # that reading's "t"

    def attach(self, sock):
        self.sock = sock
        sock.readyRead.connect(self.read)

    def send(self, msg: dict) -> None:
        if self.sock is not None:
            self.sock.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            self.sock.flush()

    def read(self) -> None:
        self.buf.extend(bytes(self.sock.readAll()))
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                return
            line = bytes(self.buf[:i])
            del self.buf[:i + 1]
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.on(msg)

    def on(self, msg: dict) -> None:  # noqa: C901
        k = msg.get("k")
        raw = float(msg.get("t") or 0.0)
        t = round(raw - self.base + self.t0, 2) if self.base is not None else "load"
        if k == "hello":
            self.hello = msg.get("token") == self.token
        elif k == "ready":
            self.ready = True
        elif k == "score":
            v = float(msg.get("value") or 0.0)
            self.log({"event": "score", "id": msg.get("id"), "value": round(max(0.0, min(1.0, v)), 3),
                      "detail": str(msg.get("detail", ""))[:60], "t": t, "accepted": True})
        elif k == "target":
            self.log({"event": "target.pitch", "hz": msg.get("hz"), "t": t, "accepted": True})
        elif k == "noise":
            self.log({"event": "target.noise", "body": msg.get("body"), "t": t, "accepted": True})
        elif k == "exit":
            self.exited = True
            self.log({"event": "exit", "why": msg.get("why", ""), "t": t})
        elif k == "blocked":
            self.blocked.append({"what": msg.get("what"), "url": msg.get("url")})
            if msg.get("what") == "score":
                self.bad_score_ids.append(str(msg.get("url")))
            self.log({"event": "blocked", "what": msg.get("what"), "url": msg.get("url")})
        elif k == "error":
            self.error = (str(msg.get("why", "")) + ": " + str(msg.get("detail", ""))).strip(": ")
        elif k == "log":
            self.warn(str(msg.get("msg", "")))
        elif k in ("sync", "shot"):
            self.answers[(k, msg.get("id"))] = msg

    def feed(self, tick: dict, t: float, fast: bool) -> None:
        if self.base is None:
            self.base, self.t0 = time.time(), t
        # --fast: the stream's clock, shifted onto the wall clock (see the top of this file)
        self.send({"k": "d", "d": filtered(tick, self.m.get("streams", [])),
                   "now": self.base + (t - self.t0) if fast else None, "fixed": fast})

    def wait(self, cond, ms: int) -> bool:
        loop = QEventLoop()
        end = time.monotonic() + ms / 1000.0
        while not cond() and time.monotonic() < end:
            QTimer.singleShot(5, loop.quit)
            loop.exec()
        return bool(cond())

    def sync(self, ms: int = 10000) -> bool:
        self.seq += 1
        n = self.seq
        self.send({"k": "sync", "id": n})
        return self.wait(lambda: ("sync", n) in self.answers or self.exited or self.error != "", ms)


def load_stream(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        if "t" not in row:
            raise ValueError(f"{path}:{i + 1}: every line needs \"t\" (seconds)")
        rows.append(row)
    return rows


def run(pkg: Path, stream: Path, fast: bool, shot: str | None, as_json: bool, show: bool) -> int:  # noqa: C901
    events: list[dict] = []
    warnings: list[str] = []

    def log(e):
        events.append(e)
        if not as_json:
            print("  " + json.dumps(e))

    def warn(msg):
        warnings.append(msg)
        if not as_json:
            print("  QML: " + msg)

    manifest, why = check.validate(pkg)
    if why:
        if as_json:
            print(json.dumps({"ok": False, "why": why}))
        else:
            print("REFUSED " + str(pkg))
            for w in why:
                print("  - " + w)
        return 1
    if not HOST.is_file():
        print(json.dumps({"ok": False, "why": ["game_host.py is missing next to simulate.py"]}) if as_json
              else "game_host.py is missing next to simulate.py")
        return 1

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    link = Link(manifest, log, warn)
    server = QLocalServer()
    name = f"mmirror-sim-{os.getpid()}-{secrets.token_hex(4)}"
    if not server.listen(name):
        QLocalServer.removeServer(name)
        server.listen(name)
    server.newConnection.connect(lambda: link.attach(server.nextPendingConnection()))
    proc = QProcess()
    proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    args = ["-u", str(HOST), "--package", str(pkg.resolve()), "--server", server.fullServerName(),
            "--token", link.token, "--bare", "--allow-shot"]
    if not (show or shot):
        args.append("--hidden")
    proc.start(sys.executable, args)
    ok_start = link.wait(lambda: link.ready or link.error != "" or proc.state() == QProcess.ProcessState.NotRunning,
                         30000)
    rows = load_stream(stream)
    if not as_json:
        print(f"{manifest['name']} {manifest['version']}: {len(rows)} readings from {stream.name}"
              f"{' (fast)' if fast else ''}")

    seq = [0]

    def feed(i):
        r = dict(rows[i])
        t = float(r.pop("t"))
        seq[0] += 1
        r.setdefault("seq", seq[0])
        link.feed(r, t, fast)

    if ok_start and link.ready:
        if fast:
            for i in range(len(rows)):
                feed(i)
                if i % 50 == 49:
                    link.sync()                     # let the game keep up; a frozen game stops here
                if link.exited or link.error:
                    break
            link.sync()
        else:
            t0 = float(rows[0]["t"]) if rows else 0.0
            clock = QElapsedTimer()
            clock.start()
            idx = [0]
            timer = QTimer()
            timer.setInterval(5)
            loop = QEventLoop()

            def step():
                now = clock.elapsed() / 1000.0
                while idx[0] < len(rows) and float(rows[idx[0]]["t"]) - t0 <= now:
                    feed(idx[0])
                    idx[0] += 1
                if idx[0] >= len(rows) or link.exited or link.error \
                        or proc.state() == QProcess.ProcessState.NotRunning:
                    timer.stop()
                    QTimer.singleShot(300, loop.quit)
            timer.timeout.connect(step)
            timer.start()
            loop.exec()
            link.sync()
        if shot:
            link.send({"k": "shot", "id": None, "path": str(Path(shot).resolve())})
            link.wait(lambda: ("shot", None) in link.answers, 5000)
    link.send({"k": "close"})
    if not proc.waitForFinished(5000):
        proc.kill()
        proc.waitForFinished(2000)
    code = proc.exitCode()
    crashed = proc.exitStatus() == QProcess.ExitStatus.CrashExit
    host_out = bytes(proc.readAll()).decode("utf-8", "replace")[-1500:]
    problem = link.error
    if not problem and (crashed or code not in (0, 5)):
        problem = "the game " + HOST_WORDS.get(code, f"stopped (exit {code})")
    if not problem and not link.ready:
        problem = "the game did not start" + (": " + host_out.strip()[-300:] if host_out.strip() else "")
    del app
    errors = [w for w in warnings if "Error" in w or "is not defined" in w or "TypeError" in w or "ReferenceError" in w]
    result = {"ok": not errors and not link.bad_score_ids and not problem, "package": manifest["id"],
              "readings": len(rows), "events": events, "warnings": warnings, "blocked": link.blocked,
              "bad_score_ids": link.bad_score_ids, "problem": problem}
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        scores = [e for e in events if e["event"] == "score"]
        print(f"{len(scores)} score(s), {len(warnings)} QML warning(s)"
              + (f", score ids not in the manifest: {link.bad_score_ids}" if link.bad_score_ids else "")
              + (f", {len(link.blocked)} blocked" if link.blocked else "")
              + (f"\nPROBLEM: {problem}" if problem else ""))
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", type=Path)
    ap.add_argument("--stream", type=Path, default=DEMO, help="JSONL, one reading per line with \"t\" (default: the demo)")
    ap.add_argument("--fast", action="store_true", help="no waiting: mirror.now() follows the stream's t")
    ap.add_argument("--shot", help="save a PNG of the game at the end")
    ap.add_argument("--json", action="store_true", help="print one JSON result (for CI)")
    ap.add_argument("--show", action="store_true", help="show the window even with --fast")
    a = ap.parse_args(argv)
    return run(a.package, a.stream, a.fast, a.shot, a.json, a.show or not a.fast)


if __name__ == "__main__":
    raise SystemExit(main())
