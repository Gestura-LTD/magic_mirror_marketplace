#!/usr/bin/env python3
"""game_host.py -- ONE Marketplace game package, in its own process (Marketplace phase 1, card MK1).

Plan: docs/MARKETPLACE-PHASE1-PLAN.md cards P1-1 (this process), P1-2 (the stream socket), P1-3 (the watchdog), P1-4
(the lockdown). Design: docs/reports/2026-11-03-R5-marketplace-and-modes.md A2 ("only a separate process really
contains this").

    python game_host.py --package DIR --server NAME --token T [--geometry x,y,w,h] [--bare] [--hidden]

The mirror (mirror_qt/market.py) starts this file with QProcess; the SDK simulator (sdk/simulator/simulate.py) starts
an exact copy of it (sdk/simulator/game_host.py, a test keeps the two identical). It imports the standard library and
PySide6 only -- never a mirror module -- so it can ship in the public SDK.

What it is
  * its own QGuiApplication and ONE frameless window above the mirror (Qt.WindowStaysOnTopHint), the mirror's own
    geometry (--geometry), drawn with the approved Marketplace frame (mockup A, page 03): the strip with the name,
    the coral community frame for outside games (--community), the hamburger with Close. `--bare` = the game area
    only (734 x 1273, scale 1; the simulator).
  * its own QQmlEngine. The root context holds ONE object, `mirror` (sdk 1) -- no bridge, no camera image provider,
    no settings: they do not exist in this process.
  * the stream: a QLocalSocket to the mirror's QLocalServer (a named pipe on Windows). Mirror -> host, one JSON line
    each: {"k":"d","d":{...the declared streams only...},"now":t,"fixed":bool}, {"k":"close"}, and for tests /
    the simulator {"k":"probe"}, {"k":"shot"}. Host -> mirror: hello, hb (every 500 ms, from the GUI thread),
    score, target, noise, exit, log (QML warnings), blocked, error. Never audio, video or pose: the mirror filters
    the tick to the manifest's streams before it is sent, and this host never asks for anything else.

The lockdown (P1-4)
  * imports: setImportPathList([Qt's own qml folder]) + setPluginPathList(["."]) (a module's plugin only from beside
    its own qmldir) + the URL fence below, which lets a qmldir through only for the allow-list (QtQuick,
    QtQuick.Shapes, QtQuick.Effects, QtQuick.Particles, QtQml and the two modules QtQml itself pulls in) or a folder
    inside the package; a package that ships a native file or a qmldir naming a plugin is refused before loading;
  * files: the fence (a QQmlAbstractUrlInterceptor) rewrites every URL -- Image / Loader / FontLoader sources, JS and
    QML files, imports -- that is not inside the package's own folder (symlinks resolved) to a dead one, and reports
    it ("blocked"). XHR file reads stay off (QML_XHR_ALLOW_FILE_READ is removed from the environment);
  * network: the application proxy AND every engine network manager point at a dead proxy (127.0.0.1:9), so XHR,
    remote images and sockets get "connection refused"; remote URLs never even reach them (the fence). R2b step 8:
    Qt skips proxies for loopback addresses, so every engine network manager is now a RefusingNam whose
    createRequest turns EVERY request into a read of a file that does not exist (an error reply, no socket at all)
    and reports it -- the sound server's 127.0.0.1 port is closed even if the fence had a hole (tested with the
    fence off, --test-no-fence); A Python
    interceptor deadlocked QQuickView.setSource in MK0 -- this host never calls setSource: it compiles the package
    with QQmlComponent, which the test suite (test_mk1_host.py) shows does not deadlock;
  * Qt.openUrlExternally: URL handlers for the common schemes swallow it and report it (it cannot be removed from the
    JS global; check.py refuses the text, too). The fence already rewrites the URL to a dead file:// one first (so the
    "file" handler catches it); R2b adds a catch-all: every scheme the OS has an app registered for (Windows:
    HKEY_CLASSES_ROOT keys with "URL Protocol"; Linux: x-scheme-handler/* in the mimeapps / mimeinfo files) gets the
    same handler at start;
  * Qt.quit() / Qt.exit() only end the game;
  * the working directory is the package folder; the mirror starts this with a clean environment.

The watchdog (P1-3)
  * every 500 ms the GUI thread sends "hb"; the mirror kills this process after 2 s without one;
  * a watchdog thread calls QJSEngine.setInterrupted(True) when the GUI thread has not ticked for 1 s (stops a
    `while (true)` when the GIL is free), then the host reports "froze" and leaves;
  * the same thread ends the process (exit 3) when it uses more memory than --max-mb; the mirror checks too.

Exit codes: 0 closed, 2 did not load, 3 too much memory, 4 froze, 5 the mirror went away.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HOST_VERSION = 1
SDK_VERSION = 1
GAME_W, GAME_H = 734, 1273
HEARTBEAT_MS = 500
FREEZE_S = 1.0
MAX_MB = 1500
MAX_OUT_PER_S = 20                 # score / target / exit calls a package may make per second
MAX_LINE = 256 * 1024
DEAD_PROXY = ("127.0.0.1", 9)      # the discard port: nothing listens, every connection is refused
# the QML modules a package may import (market.py / check.py ALLOWED_IMPORTS) + what QtQml itself pulls in
ALLOWED_MODULES = frozenset(("QtQuick", "QtQuick.Shapes", "QtQuick.Effects", "QtQuick.Particles", "QtQml",
                             "QtQml.Models", "QtQml.WorkerScript"))
URL_SCHEMES = ("http", "https", "file", "ftp", "mailto", "tel", "sms", "data", "javascript", "ms-settings",
               "ms-windows-store", "microsoft-edge", "search-ms", "shell", "steam", "zoommtg", "msteams", "skype",
               "spotify", "whatsapp", "slack", "discord", "itms", "market", "intent", "smb", "ssh", "ws", "wss")
WATCHED_MODULES = ("socket", "ssl", "http", "http.client", "urllib.request", "PySide6.QtWebSockets",
                   "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick", "PySide6.QtHttpServer", "requests")
EXIT_OK, EXIT_LOAD, EXIT_MEMORY, EXIT_FROZE, EXIT_ORPHAN = 0, 2, 3, 4, 5
BLOCKED_FILE = "file:///__mirror_blocked__/x"   # never exists: a request turned into this fails without a socket
MAX_OS_SCHEMES = 2000
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]{0,63}$")


def os_url_schemes() -> list:
    """Every URL scheme an app on this PC is registered for (the openUrlExternally catch-all). Never raises."""
    out: set = set()
    try:
        if sys.platform == "win32":
            import winreg  # noqa: PLC0415
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                i = 0
                while len(out) < MAX_OS_SCHEMES:
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if name.startswith(".") or not SCHEME_RE.match(name.lower()):
                        continue
                    try:
                        with winreg.OpenKey(root, name) as k:
                            winreg.QueryValueEx(k, "URL Protocol")
                        out.add(name.lower())
                    except OSError:
                        pass
        else:
            home = os.path.expanduser("~")
            dirs = [os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                    os.path.join(home, ".local", "share", "applications")]
            dirs += [os.path.join(d, "applications") for d in
                     (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
            for d in dirs:
                for fn in ("mimeapps.list", "mimeinfo.cache", "defaults.list"):
                    try:
                        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                            text = f.read(2_000_000)
                    except OSError:
                        continue
                    for m in re.finditer(r"x-scheme-handler/([A-Za-z][A-Za-z0-9+.\-]{0,63})", text):
                        out.add(m.group(1).lower())
    except Exception:  # noqa: BLE001 - a catch-all that fails leaves the fixed list (and the fence) in place
        pass
    return sorted(out)[:MAX_OS_SCHEMES]


def rss_bytes(pid: int | None = None) -> int:
    """The resident memory of a process (this one by default); 0 when it cannot be read."""
    pid = os.getpid() if pid is None else int(pid)
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/statm", "rb") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return 0
            try:
                c = PMC()
                c.cb = ctypes.sizeof(PMC)
                if not k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                    return 0
                return int(max(c.WorkingSetSize, c.PagefileUsage))   # private bytes count too (a JS heap)
            finally:
                k32.CloseHandle(h)
    except (OSError, ValueError, AttributeError, IndexError):
        return 0
    return 0


def _strip_version(part: str) -> str:
    return re.sub(r"\.\d+(\.\d+)?$", "", part)


def module_of(rel_dir: str) -> str:
    """"QtQml.6.11/WorkerScript" -> "QtQml.WorkerScript" (a qmldir's folder, relative to an import root)."""
    return ".".join(_strip_version(p) for p in rel_dir.strip("/").split("/") if p)


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                              # different drives on Windows
        return False


NATIVE_EXT = (".so", ".dll", ".dylib", ".pyd", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".py", ".node", ".wasm")


def native_code(folder: str) -> str:
    """The first file in the package that could run outside QML (a library, a program, a qmldir plugin line)."""
    n = 0
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            n += 1
            if n > 2000:
                return "too many files"
            low = f.lower()
            if low.endswith(NATIVE_EXT):
                return os.path.relpath(os.path.join(dirpath, f), folder)
            if low == "qmldir":
                try:
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            w = line.split()
                            if w[:1] == ["plugin"] or w[:2] == ["optional", "plugin"]:
                                return os.path.relpath(os.path.join(dirpath, f), folder) + " (plugin)"
                except OSError:
                    return f
    return ""


def main(argv=None) -> int:  # noqa: C901, PLR0915 - one process, read top to bottom
    ap = argparse.ArgumentParser(description="one Marketplace package in its own process")
    ap.add_argument("--package", required=True)
    ap.add_argument("--server", required=True, help="the mirror's QLocalServer name")
    ap.add_argument("--token", default="", help="said back in hello so the mirror knows it is its own host")
    ap.add_argument("--geometry", default="", help="x,y,w,h of the mirror's window")
    ap.add_argument("--bare", action="store_true", help="the game area only (the simulator)")
    ap.add_argument("--hidden", action="store_true", help="no window on screen (fast simulator runs, CI)")
    ap.add_argument("--max-mb", type=int, default=MAX_MB)
    ap.add_argument("--community", action="store_true", help="an outside game: the coral community frame (mockup A)")
    ap.add_argument("--font", action="append", default=[], help="a font file for the host's own frame (Nunito)")
    ap.add_argument("--probe", action="store_true", help="answer probe requests (tests only)")
    ap.add_argument("--allow-shot", action="store_true", help="answer shot requests (the simulator's --shot)")
    ap.add_argument("--test-no-fence", action="store_true",
                    help="tests only: no URL fence, to prove the refusing network manager holds on its own")
    a = ap.parse_args(argv)

    pkg = Path(a.package).resolve()
    pkg_real = os.path.realpath(str(pkg))
    # ---- the environment, before Qt reads it
    for k in ("QML_XHR_ALLOW_FILE_READ", "QML_XHR_ALLOW_FILE_WRITE", "QML_IMPORT_PATH", "QML2_IMPORT_PATH",
              "QT_PLUGIN_PATH", "QML_DISABLE_DISK_CACHE"):
        os.environ.pop(k, None)
    os.environ["QML_DISK_CACHE_PATH"] = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp",
                                                     "mmirror-host-cache")
    try:
        os.chdir(pkg_real)
    except OSError:
        pass

    from PySide6.QtCore import (Q_ARG, QByteArray, QLibraryInfo, QMetaObject, QObject, QTimer, QUrl, Property, Qt,
                                Slot, qInstallMessageHandler)
    from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QGuiApplication
    from PySide6.QtNetwork import QLocalSocket, QNetworkAccessManager, QNetworkProxy
    from PySide6.QtQml import (QQmlAbstractUrlInterceptor, QQmlComponent, QQmlContext, QQmlEngine,
                               QQmlNetworkAccessManagerFactory)
    from PySide6.QtQuick import QQuickItem, QQuickWindow

    out_q: collections.deque = collections.deque(maxlen=400)   # filled from any thread, sent by the GUI thread
    state = {"hello": False, "beat": time.monotonic(), "interrupted": False, "closing": False, "outs": [],
             "logs": 0}

    def say(msg: dict) -> None:
        out_q.append(msg)

    def on_msg(mode, ctx, text):
        if state["logs"] >= 200:
            return
        state["logs"] += 1
        where = f"{ctx.file}:{ctx.line}: " if ctx is not None and ctx.file else ""
        say({"k": "log", "msg": (where + str(text))[:500]})

    qInstallMessageHandler(on_msg)

    try:
        manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"game_host: no manifest: {exc}\n")
        return EXIT_LOAD
    streams = [s for s in manifest.get("streams") or [] if isinstance(s, str)]
    score_ids = [s for s in manifest.get("scoreIds") or [] if isinstance(s, str)]
    outputs = [s for s in manifest.get("outputs") or [] if isinstance(s, str)]
    entry = manifest.get("entry") if isinstance(manifest.get("entry"), str) else "main.qml"

    QGuiApplication.setQuitOnLastWindowClosed(False)
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, *DEAD_PROXY))
    for ttf in a.font:                       # the frame's font, loaded by the host itself (never through the fence)
        if os.path.isfile(ttf):
            QFontDatabase.addApplicationFont(ttf)
    qt_qml = os.path.realpath(QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath))

    class Fence(QQmlAbstractUrlInterceptor):
        """Every URL the engine resolves: inside the package, or an allowed Qt module, or nothing."""

        DEAD = QUrl("file:///__mirror_blocked__/x")

        def __init__(self):
            super().__init__()
            self.seen_block: set[str] = set()

        def verdict(self, url, typ) -> bool:  # noqa: C901, PLR0911
            scheme = url.scheme().lower()
            T = QQmlAbstractUrlInterceptor.DataType
            if scheme == "data":
                return typ == T.UrlString and url.toString().startswith("data:image/")
            if scheme in ("file", ""):
                p = url.toLocalFile() if scheme == "file" else url.toString()
                if not p:
                    return False
                real = os.path.realpath(p)
                if _inside(real, pkg_real):
                    return True
                if _inside(real, qt_qml) and typ != T.UrlString:
                    rel = os.path.relpath(os.path.dirname(real), qt_qml).replace(os.sep, "/")
                    return module_of(rel) in ALLOWED_MODULES
                return False
            if scheme == "qrc" and typ != T.UrlString:
                path = url.path()
                for root in ("/qt/qml/", "/qt-project.org/imports/"):
                    if path.startswith(root):
                        return module_of(os.path.dirname(path[len(root):])) in ALLOWED_MODULES
                return path.startswith("/qt-project.org/") and typ != T.QmldirFile
            return False

        def intercept(self, url, typ):
            try:
                ok = self.verdict(url, typ)
            except Exception:  # noqa: BLE001 - a fence that throws is a closed fence
                ok = False
            if ok:
                return url
            s = url.toString()[:300]
            T = QQmlAbstractUrlInterceptor.DataType
            # Qt probes many candidate qmldir folders for every import: only report what the package asked for
            if typ != T.QmldirFile and url != self.DEAD and s not in self.seen_block and len(self.seen_block) < 100:
                self.seen_block.add(s)
                say({"k": "blocked", "what": "url", "url": s, "type": str(getattr(typ, "name", typ))})
            return self.DEAD

    from PySide6.QtNetwork import QNetworkRequest

    class RefusingNam(QNetworkAccessManager):
        """Every request becomes a read of BLOCKED_FILE: an error reply, no socket (loopback included)."""

        def createRequest(self, op, req, data=None):  # noqa: N802
            try:
                u = req.url().toString()[:300]
                if not u.startswith(BLOCKED_FILE):
                    say({"k": "blocked", "what": "network", "url": u})
            except Exception:  # noqa: BLE001
                pass
            return super().createRequest(QNetworkAccessManager.Operation.GetOperation,
                                         QNetworkRequest(QUrl(BLOCKED_FILE)), None)

    class DeadNam(QQmlNetworkAccessManagerFactory):
        def __init__(self):
            super().__init__()
            self.made = []                    # keep the Python wrappers alive (their createRequest is ours)

        def create(self, parent):
            n = RefusingNam(parent)
            n.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, *DEAD_PROXY))
            self.made.append(n)
            return n

    class UrlTrap(QObject):
        @Slot(QUrl)
        def handle(self, url):
            say({"k": "blocked", "what": "openUrlExternally", "url": url.toString()[:300]})

    trap = UrlTrap()
    for s in sorted(set(URL_SCHEMES) | set(os_url_schemes())):
        QDesktopServices.setUrlHandler(s, trap, "handle")

    engine = QQmlEngine()
    engine.setImportPathList([qt_qml])
    engine.setPluginPathList(["."])          # [] breaks QtQuick's own plugin; "." = beside each qmldir only
    fence = Fence()
    if not a.test_no_fence:
        engine.addUrlInterceptor(fence)
    dead_nam = DeadNam()
    engine.setNetworkAccessManagerFactory(dead_nam)
    engine.setOfflineStoragePath(os.path.join(pkg_real, "__no_storage__"))

    def filtered(tick: dict) -> dict:
        """Defence in depth: the declared streams only, even if the mirror ever sent more."""
        out: dict = {}
        if not isinstance(tick, dict):
            return out
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

    def allow_out() -> bool:
        now = time.monotonic()
        q = state["outs"]
        while q and now - q[0] > 1.0:
            q.pop(0)
        if len(q) >= MAX_OUT_PER_S:
            if not state.get("flood"):
                state["flood"] = True
                say({"k": "blocked", "what": "flood", "url": f"more than {MAX_OUT_PER_S} calls a second"})
            return False
        q.append(now)
        return True

    class Link(QObject):
        """What the `mirror` facade (FACADE_QML) calls. The package never sees this object: it lives only in the
        facade's own context. Ticks go the other way through a QUEUED call into the facade (see `feed`), so the
        package's JavaScript runs from the event loop with the GIL free, and the watchdog thread can interrupt it."""

        def __init__(self):
            super().__init__()
            self.item = None

        @Property(str, constant=True)
        def packageId(self):  # noqa: N802
            return str(manifest.get("id", ""))

        @Property("QVariantList", constant=True)
        def streams(self):
            return list(streams)

        @Slot(result=float)
        def now(self):
            """Live time. A fixed clock (tests, the simulator's --fast) lives in the facade, with each tick."""
            return time.time()

        def t(self) -> float:
            """The time the package sees now: the facade's tick time when the clock is fixed."""
            try:
                if facade.property("fixed"):
                    return float(facade.property("t"))
            except (RuntimeError, TypeError, ValueError):
                pass
            return time.time()

        def refused(self, what: str, name: str) -> bool:
            say({"k": "blocked", "what": what, "url": str(name)[:60]})
            return False

        @Slot(str, float, str, result=bool)
        def score(self, score_id, value, detail=""):
            if str(score_id) not in score_ids:
                return self.refused("score", score_id)
            if not allow_out():
                return False
            say({"k": "score", "id": str(score_id), "value": float(value), "detail": str(detail)[:60],
                 "t": self.t()})
            return True

        @Slot(float, result=bool)
        def setTarget(self, hz):  # noqa: N802
            if "target.pitch" not in outputs:
                return self.refused("output", "target.pitch")
            if not allow_out():
                return False
            say({"k": "target", "hz": float(hz), "t": self.t()})
            return True

        @Slot("QVariant", result=bool)
        def setNoiseTarget(self, body):  # noqa: N802
            if "target.noise" not in outputs:
                return self.refused("output", "target.noise")
            if not allow_out():
                return False
            body = body.toVariant() if hasattr(body, "toVariant") else body
            out = {}
            if isinstance(body, dict):
                for k in ("hnr", "band", "word", "game"):
                    v = body.get(k)
                    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                        out[k] = v if not isinstance(v, str) else v[:40]
            say({"k": "noise", "body": out, "t": self.t()})
            return True

        @Slot()
        def exit(self):
            say({"k": "exit", "why": "package"})
            QTimer.singleShot(0, lambda: leave(EXIT_OK))

        @Slot("QVariant")
        def answer(self, msg):
            """probe / sync / shot, QUEUED behind the ticks sent before them, so they see those ticks' effects."""
            answer(msg.toVariant() if hasattr(msg, "toVariant") else msg)

        @Slot(bool, str, "QVariant")
        def loaded(self, ok, why, obj):
            """The launcher's answer (from the event loop): the package's root object, or why not."""
            on_loaded(bool(ok), str(why), obj)

    link = Link()

    def feed(d: dict, now, fixed: bool) -> None:
        """Queued: the tick's own time travels with it, so a burst of ticks keeps each one's clock."""
        t = float(now) if isinstance(now, (int, float)) and not isinstance(now, bool) else 0.0
        QMetaObject.invokeMethod(facade, "setD", Qt.ConnectionType.QueuedConnection, Q_ARG("QVariant", filtered(d)),
                                 Q_ARG("QVariant", t), Q_ARG("QVariant", bool(fixed) and now is not None))

    keep: list = []                          # the host's own components and contexts live as long as the process

    def qml_object(src: str, ctx, name: str):
        c = QQmlComponent(engine)
        keep.append(c)
        # in the package's folder URL only so the fence lets its `import QtQuick` through
        c.setData(QByteArray(src.encode()), QUrl.fromLocalFile(os.path.join(pkg_real, f"__host_{name}__.qml")))
        o = c.create(ctx)
        if o is None:
            raise RuntimeError(f"{name}: {c.errorString()}")
        QQmlEngine.setObjectOwnership(o, QQmlEngine.ObjectOwnership.CppOwnership)
        return o

    facade_ctx = QQmlContext(engine.rootContext())
    keep.append(facade_ctx)
    facade_ctx.setContextProperty("hostLink", link)
    facade = qml_object(FACADE_QML, facade_ctx, "mirror")
    engine.rootContext().setContextProperty("mirror", facade)
    # created in the ROOT context (no ids, no names of its own): what it creates sees `mirror` and nothing else
    launcher = qml_object(LAUNCHER_QML, engine.rootContext(), "launcher")

    # ---- the window
    win = QQuickWindow()
    win.setColor(QColor("#1A1714"))
    win.setTitle(str(manifest.get("name", "game"))[:60])
    win.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Window)
    geo = [int(float(x)) for x in a.geometry.split(",")] if a.geometry.count(",") == 3 else []
    if a.bare:
        win.resize(GAME_W, GAME_H)
    elif geo:
        win.setGeometry(*geo)
    else:
        win.resize(1080, 1920)

    class Ctl(QObject):
        """What the host's own frame (not the package) may call."""

        @Slot()
        def close(self):
            say({"k": "exit", "why": "close button"})
            QTimer.singleShot(0, lambda: leave(EXIT_OK))

    ctl = Ctl()
    chrome_ctx = QQmlContext(engine.rootContext())
    keep.append(chrome_ctx)
    chrome_ctx.setContextProperty("hostCtl", ctl)
    chrome_ctx.setContextProperty("hostName", str(manifest.get("name", ""))[:60])
    chrome_ctx.setContextProperty("hostBare", bool(a.bare))
    chrome_ctx.setContextProperty("hostCommunity", bool(a.community))
    chrome_ctx.setContextProperty("hostAuthor", str(manifest.get("author", ""))[:60] if a.community else "")
    frame = qml_object(CHROME_QML, chrome_ctx, "frame")
    frame.setParentItem(win.contentItem())

    def fit_frame(*_):
        frame.setWidth(win.width())
        frame.setHeight(win.height())

    win.widthChanged.connect(fit_frame)
    win.heightChanged.connect(fit_frame)
    fit_frame()
    slot = frame.findChild(QQuickItem, "hostSlot")

    # ---- the socket
    sock = QLocalSocket()
    buf = bytearray()
    game = {"item": None}

    def send_now(msg: dict) -> None:
        if sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            sock.write((json.dumps(msg, separators=(",", ":"), default=str) + "\n").encode())

    def flush() -> None:
        while out_q and state["hello"]:
            send_now(out_q.popleft())
        if sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            sock.flush()

    def leave(code: int) -> None:
        if state["closing"]:
            return
        state["closing"] = True
        say({"k": "bye", "code": code})
        flush()
        sock.waitForBytesWritten(300)
        app.exit(code)

    def json_safe(v, depth=0):
        if hasattr(v, "toVariant"):
            v = v.toVariant()
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if depth > 4:
            return str(v)[:80]
        if isinstance(v, dict):
            return {str(k): json_safe(x, depth + 1) for k, x in list(v.items())[:50]}
        if isinstance(v, (list, tuple)):
            return [json_safe(x, depth + 1) for x in list(v)[:200]]
        return str(v)[:80]

    def handle(msg: dict) -> None:
        k = msg.get("k")
        if k == "d":
            feed(msg.get("d") or {}, msg.get("now"), msg.get("fixed", False))
        elif k == "close":
            leave(EXIT_OK)
        elif k in ("probe", "sync", "shot"):
            QMetaObject.invokeMethod(link, "answer", Qt.ConnectionType.QueuedConnection, Q_ARG("QVariant", msg))

    def answer(msg: dict) -> None:
        k = msg.get("k")
        if k == "sync":
            say({"k": "sync", "id": msg.get("id")})
        elif k == "probe" and a.probe:
            it = game["item"]
            props = {}
            for name in msg.get("props") or []:
                try:
                    props[name] = json_safe(it.property(name)) if it is not None else None
                except Exception as exc:  # noqa: BLE001
                    props[name] = f"<{exc}>"
            mods = sorted(m for m in WATCHED_MODULES if m in sys.modules)
            send_now({"k": "probe", "id": msg.get("id"), "props": props, "modules": mods,
                      "rss": rss_bytes(), "window": [win.x(), win.y(), win.width(), win.height()],
                      "onTop": bool(win.flags() & Qt.WindowType.WindowStaysOnTopHint),
                      "frameless": bool(win.flags() & Qt.WindowType.FramelessWindowHint),
                      "importPaths": list(engine.importPathList()), "pluginPaths": list(engine.pluginPathList()),
                      "context": sorted(n for n in ("bridge", "liquidDisc", "liquidBudget", "startFullScreen",
                                                    "hostCtl")
                                        if engine.rootContext().contextProperty(n) is not None),
                      "cwd": os.getcwd(), "env": sorted(os.environ),
                      "chrome": {n: bool(o.property("visible")) for n in ("hostFrame", "hostCommunityTag", "hostMenu")
                                 for o in [frame.findChild(QQuickItem, n)] if o is not None}})
        elif k == "shot" and a.allow_shot and isinstance(msg.get("path"), str):
            ok = win.grabWindow().save(msg["path"])
            send_now({"k": "shot", "ok": bool(ok)})
        flush()

    def on_ready() -> None:
        buf.extend(bytes(sock.readAll()))
        if len(buf) > 8 * MAX_LINE:              # the mirror would never: drop, do not grow
            buf.clear()
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            line = bytes(buf[:i])
            del buf[:i + 1]
            if not line or len(line) > MAX_LINE:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                handle(msg)
        flush()

    sock.readyRead.connect(on_ready)
    sock.disconnected.connect(lambda: leave(EXIT_ORPHAN))
    sock.connectToServer(a.server)
    if not sock.waitForConnected(5000):
        sys.stderr.write(f"game_host: cannot reach the mirror at {a.server}\n")
        return EXIT_ORPHAN

    def beat() -> None:
        state["beat"] = time.monotonic()
        if state["interrupted"]:
            state["interrupted"] = False
            say({"k": "error", "why": "froze", "detail": "stopped a script that ran for more than 1 s"})
            QTimer.singleShot(0, lambda: leave(EXIT_FROZE))

    pulse = QTimer()
    pulse.setInterval(100)
    pulse.timeout.connect(beat)
    pulse.start()
    hb = QTimer()
    hb.setInterval(HEARTBEAT_MS)
    hb.timeout.connect(lambda: (say({"k": "hb", "rss": rss_bytes()}), flush()))
    hb.start()

    limit = max(64, int(a.max_mb)) * 1024 * 1024

    def watchdog() -> None:
        while not state["closing"]:
            time.sleep(0.2)
            if time.monotonic() - state["beat"] > FREEZE_S and not state["interrupted"]:
                state["interrupted"] = True
                engine.setInterrupted(True)
            r = rss_bytes()
            if r > limit:
                sys.stderr.write(f"game_host: {r >> 20} MB > {limit >> 20} MB, leaving\n")
                sys.stderr.flush()
                os._exit(EXIT_MEMORY)

    threading.Thread(target=watchdog, name="host-watchdog", daemon=True).start()

    # ---- the package
    native = native_code(pkg_real)
    send_now({"k": "hello", "token": a.token, "pid": os.getpid(), "version": HOST_VERSION, "ok": True})
    state["hello"] = True
    flush()
    if native:
        send_now({"k": "error", "why": "load", "detail": "native code is not allowed: " + native})
        flush()
        sock.waitForBytesWritten(500)
        return EXIT_LOAD

    def on_loaded(ok: bool, why: str, obj) -> None:
        if not ok or obj is None:
            say({"k": "error", "why": "load", "detail": (why.strip() or "the package did not start")[:400]})
            QTimer.singleShot(0, lambda: leave(EXIT_LOAD))
            return
        item = obj.toVariant() if hasattr(obj, "toVariant") else obj
        game["item"] = item
        if isinstance(item, QQuickItem) and slot is not None:
            def fit_game(*_):
                item.setWidth(slot.width())
                item.setHeight(slot.height())

            slot.widthChanged.connect(fit_game)
            slot.heightChanged.connect(fit_game)
            fit_game()
        if not a.hidden:
            win.show()
            win.raise_()
            win.requestActivate()
        frame.forceActiveFocus()
        say({"k": "ready"})
        flush()

    engine.quit.connect(lambda: (say({"k": "exit", "why": "Qt.quit"}), leave(EXIT_OK)))
    engine.exit.connect(lambda code: (say({"k": "exit", "why": f"Qt.exit({code})"}), leave(EXIT_OK)))
    # created from the event loop by QML (a queued call): a `while (true)` in the package's Component.onCompleted
    # then runs with the GIL free, so the watchdog can interrupt it
    QMetaObject.invokeMethod(launcher, "go", Qt.ConnectionType.QueuedConnection,
                             Q_ARG("QVariant", QUrl.fromLocalFile(os.path.join(pkg_real, entry)).toString()),
                             Q_ARG("QVariant", slot), Q_ARG("QVariant", link))
    code = app.exec()
    state["closing"] = True
    return code


# The package's `mirror` (sdk 1), the same surface as the in-process facade in windows/Market.qml. `hostLink` is in
# THIS object's context only; the package's context (the root context) has `mirror` and nothing else.
FACADE_QML = r"""
import QtQuick
QtObject {
    readonly property int sdk: 1
    readonly property string packageId: hostLink.packageId
    readonly property var streams: hostLink.streams
    property var d: ({})
    property real t: 0
    property bool fixed: false
    function setD(x, tickT, isFixed) { t = tickT; fixed = isFixed; d = x }
    function now() { return fixed ? t : hostLink.now() }
    function score(scoreId, value, detail) {
        return hostLink.score(String(scoreId), Number(value), detail === undefined ? "" : String(detail))
    }
    function setTarget(hz) { return hostLink.setTarget(Number(hz)) }
    function setNoiseTarget(body) { return hostLink.setNoiseTarget(body || ({})) }
    function exit() { hostLink.exit() }
}
"""

# Makes the package's root object under the frame's game slot. No ids and no properties of its own: the package's
# context chain is (its own) -> this file's context -> the root context, and both hold nothing but `mirror`.
LAUNCHER_QML = r"""
import QtQuick
QtObject {
    function go(url, parentItem, reply) {
        var c = Qt.createComponent(url, Component.PreferSynchronous)
        if (c.status !== Component.Ready) { reply.loaded(false, c.errorString(), null); return }
        var o = c.createObject(parentItem)
        if (o === null) { reply.loaded(false, c.errorString() || "the package did not start", null); return }
        reply.loaded(true, "", o)
    }
}
"""


# The host's own frame (Marketplace mockup A, 03_market_running.png, approved 10 Nov): the strip on top (a coral
# "community game" pill for outside games, the name, "by" the author), the coral community frame around the game
# (outside games only: our own games never wear it), the hamburger bottom right (the one approved for all games)
# with Close, and Esc. It is the HOST's code, drawn around the package: a package cannot draw over it or remove it.
# k = the window's width / 1080 (the mockup is half the 2160 x 3840 glass). The game's own area stays about
# 734 x 1273 logical pixels at any screen size (scale 1.4 at 1080 wide), as the SDK README promises.
CHROME_QML = r"""
import QtQuick
FocusScope {
    id: f
    focus: true
    readonly property real k: hostBare ? 1 : Math.max(0.3, width / 1080)
    readonly property color coral: "#E2704A"
    readonly property color buff: "#ECE6DB"
    readonly property color mute: "#93A3B3"
    readonly property string face: "Nunito"
    Keys.onEscapePressed: menu.open ? menu.open = false : hostCtl.close()
    Rectangle { anchors.fill: parent; color: hostBare ? "#1A1714" : "#141B23" }
    Row {
        id: strip
        visible: !hostBare
        x: 24 * f.k; height: 96 * f.k; spacing: 18 * f.k
        Rectangle {
            objectName: "hostCommunityTag"
            visible: hostCommunity
            anchors.verticalCenter: parent.verticalCenter
            height: 46 * f.k; width: cg.width + 36 * f.k; radius: height / 2; color: f.coral
            Text { id: cg; anchors.centerIn: parent; text: "community game"; color: "#2A1F1A"
                   font.family: f.face; font.pixelSize: 24 * f.k; font.weight: Font.Black }
        }
        Text { anchors.verticalCenter: parent.verticalCenter; text: hostName; color: f.buff
               font.family: f.face; font.pixelSize: 40 * f.k; font.weight: Font.Black }
        Text { anchors.verticalCenter: parent.verticalCenter; anchors.verticalCenterOffset: 4 * f.k
               visible: hostAuthor !== ""; text: "by " + hostAuthor; color: f.mute
               font.family: f.face; font.pixelSize: 26 * f.k; font.weight: Font.Bold }
    }
    Item {
        id: field
        x: hostBare ? 0 : 14 * f.k; y: hostBare ? 0 : 100 * f.k
        width: f.width - 2 * x; height: f.height - y - (hostBare ? 0 : 14 * f.k)
        readonly property real pad: hostBare ? 0 : 12 * f.k          // the game's corners stay under the frame
        Item {
            id: slot
            objectName: "hostSlot"
            readonly property real uiScale: hostBare ? 1.0 : 1.4 * f.k
            x: field.pad; y: field.pad
            width: (field.width - 2 * field.pad) / uiScale; height: (field.height - 2 * field.pad) / uiScale
            scale: uiScale; transformOrigin: Item.TopLeft
            clip: true
        }
        Rectangle {
            objectName: "hostFrame"
            visible: !hostBare && hostCommunity
            anchors.fill: parent; radius: 36 * f.k; color: "transparent"; border.width: 10 * f.k; border.color: f.coral
        }
    }
    // the hamburger (bottom right, within a child's reach) and its menu: Close; Esc does the same
    Item {
        id: burger
        objectName: "hostMenu"
        visible: !hostBare
        width: 112 * f.k; height: width
        x: f.width - width - 32 * f.k; y: f.height - height - 36 * f.k; z: 60
        Rectangle { y: 7 * f.k; width: parent.width; height: parent.height; radius: 32 * f.k; color: "#141B23"; opacity: 0.8 }
        Rectangle {
            width: parent.width; height: parent.height; radius: 32 * f.k; color: menu.open ? "#2A8F84" : "#2E4257"
            border.width: 3 * f.k; border.color: f.buff
            Column { anchors.centerIn: parent; spacing: 12 * f.k
                Repeater { model: 3; Rectangle { width: 54 * f.k; height: 8 * f.k; radius: height / 2
                                                 color: menu.open ? "white" : f.buff } } }
        }
        MouseArea { anchors.fill: parent; onClicked: menu.open = !menu.open }
    }
    Rectangle {
        id: menu
        objectName: "hostMenuPanel"
        property bool open: false
        visible: open && !hostBare
        z: 61
        width: 420 * f.k; height: col.height + 40 * f.k; radius: 28 * f.k
        x: f.width - width - 32 * f.k; y: burger.y - height - 20 * f.k
        color: "#1F2A36"; border.width: 3 * f.k; border.color: "#4B6B88"
        Column {
            id: col
            x: 20 * f.k; y: 20 * f.k; width: parent.width - 40 * f.k; spacing: 16 * f.k
            Repeater {
                model: [["Back to the game", false], ["Close the game", true]]
                Rectangle {
                    objectName: modelData[1] ? "hostClose" : "hostResume"
                    width: col.width; height: 96 * f.k; radius: height / 2
                    color: modelData[1] ? "#2A8F84" : "transparent"; border.width: 3 * f.k; border.color: f.buff
                    Text { anchors.centerIn: parent; text: modelData[0]; color: modelData[1] ? "white" : f.buff
                           font.family: f.face; font.pixelSize: 34 * f.k; font.weight: Font.Black }
                    MouseArea { anchors.fill: parent; onClicked: modelData[1] ? hostCtl.close() : (menu.open = false) }
                }
            }
        }
    }
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
