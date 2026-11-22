#!/usr/bin/env python3
"""market_tool.py -- the Marketplace index's own tool: check submitted packages, build and sign index.json.

    python tools/market_tool.py check-zip  game.zip                    # is my zip a valid package?
    python tools/market_tool.py check-entry packages/<id>/<version>.json   # what the CI runs on a pull request
    python tools/market_tool.py ci                                    # every entry + the signed index (the CI)
    python tools/market_tool.py verify-index                          # index.json + index.json.sig vs keys/
    python tools/market_tool.py build-index --key PRIVATE.pem         # maintainers: rebuild + sign the index
    python tools/market_tool.py keygen --out PRIVATE.pem --pub keys/marketplace_ed25519.pub

How a game is trusted (the mirror checks the same, offline, with the public key in keys/):

  * index.json is signed as a whole (Ed25519, the signature in index.json.sig). A mirror refuses an index whose
    signature does not verify, or whose `serial` is lower than one it has already seen (no replay of an old list,
    for example one from before a take-down).
  * every package in the index carries its own Ed25519 signature `sig` over
        "magicmirror-package-v1\\n<id>\\n<version>\\n<sha256 of the zip>\\n"
    so the zip a mirror downloads must have exactly the reviewed bytes, for exactly that id and version.
  * the mirror then unzips into a staging folder (no links, no paths outside, size limits), runs the same checker
    as tools/check.py, and only then moves the game into its packages folder.

The private key never enters this repository. Maintainers keep it offline (or as the MARKET_SIGNING_KEY secret,
PEM text). Python 3.9+, standard library + `cryptography` (only for keygen / signing / verifying).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import check as C  # noqa: E402  (tools/check.py: a byte-identical copy of the SDK's check.py)

INDEX_SCHEMA = 1
PACKAGE_SIG_CONTEXT = "magicmirror-package-v1"
MAX_ZIP_BYTES = C.MAX_BYTES + 4 * 1024 * 1024     # the zip of a <= 40 MB package
MAX_THUMB_BYTES = 512 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/Gestura-LTD/magic_mirror_marketplace/main/"
KEYS_DIR = REPO / "keys"
PACKAGES_DIR = REPO / "packages"
ENTRY_KEYS = {"manifest", "source", "sha256", "thumb", "notes"}


# ---------------------------------------------------------------- keys and signatures
def key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def package_message(pkg_id: str, version: str, sha256: str) -> bytes:
    return f"{PACKAGE_SIG_CONTEXT}\n{pkg_id}\n{version}\n{sha256}\n".encode("utf-8")


def read_public_keys(path: Path = KEYS_DIR) -> dict:
    """{key id: raw 32 bytes} from a .pub file or every .pub in a folder. A line: base64 of the raw key, then an
    optional comment."""
    path = Path(path)
    files = sorted(path.glob("*.pub")) if path.is_dir() else [path]
    out = {}
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.split("#")[0].strip()
            if not s:
                continue
            raw = base64.b64decode(s.split()[0], validate=True)
            if len(raw) != 32:
                raise ValueError(f"{f}: not an Ed25519 public key")
            out[key_id(raw)] = raw
    return out


def load_private_key(path: str | None):
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    if path:
        pem = Path(path).read_bytes()
    elif os.environ.get("MARKET_SIGNING_KEY"):
        pem = os.environ["MARKET_SIGNING_KEY"].encode()
    elif os.environ.get("MARKET_SIGNING_KEY_FILE"):
        pem = Path(os.environ["MARKET_SIGNING_KEY_FILE"]).read_bytes()
    else:
        raise SystemExit("no signing key: --key PRIVATE.pem, or MARKET_SIGNING_KEY (PEM text)")
    return serialization.load_pem_private_key(pem, password=None)


def public_raw(priv) -> bytes:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(priv, data: bytes) -> str:
    return base64.b64encode(priv.sign(data)).decode()


def verify(raw_pub: bytes, data: bytes, sig_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415
    try:
        Ed25519PublicKey.from_public_bytes(raw_pub).verify(base64.b64decode(sig_b64, validate=True), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def keygen(out: Path, pub: Path | None, comment: str = "") -> str:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; not overwritten")
    priv = Ed25519PrivateKey.generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    raw = public_raw(priv)
    line = base64.b64encode(raw).decode() + (f"  # {comment}" if comment else "") + f"  (key id {key_id(raw)})\n"
    if pub:
        Path(pub).parent.mkdir(parents=True, exist_ok=True)
        Path(pub).write_text(line, encoding="utf-8")
    return line


# ---------------------------------------------------------------- zips
def zip_problems(zf: zipfile.ZipFile) -> tuple[list[str], str]:
    """(reasons, prefix to strip). The same rules as the mirror (mirror_qt/market_store.py safe_extract)."""
    why: list[str] = []
    names = []
    total = 0
    for zi in zf.infolist():
        n = zi.filename
        if zi.flag_bits & 0x1:
            why.append(f"{n}: encrypted entries are not allowed")
        mode = (zi.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            why.append(f"{n}: links are not allowed")
        if not n or "\\" in n or "\x00" in n or n.startswith("/") or re.match(r"^[A-Za-z]:", n) \
                or any(p in ("..", ".") for p in n.rstrip("/").split("/")) or "//" in n:
            why.append(f"{n!r}: a path outside the package")
            continue
        if n.endswith("/"):
            continue
        names.append(n)
        total += zi.file_size
    if len(names) != len(set(names)):
        why.append("the zip names the same file twice")
    if len(names) > C.MAX_FILES:
        why.append(f"{len(names)} files (at most {C.MAX_FILES})")
    if total > C.MAX_BYTES:
        why.append(f"{total / 1e6:.1f} MB unpacked (at most {C.MAX_BYTES / 1e6:.0f} MB)")
    prefix = ""
    if "manifest.json" not in names:
        tops = {n.split("/")[0] for n in names}
        if len(tops) == 1 and all("/" in n for n in names):
            prefix = next(iter(tops)) + "/"
        if prefix + "manifest.json" not in names:
            why.append("no manifest.json at the top of the zip (or inside its one top folder)")
    return why, prefix


def extract(data: bytes, dest: Path) -> list[str]:
    """Unzip a package's bytes into dest (created). [] = done."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return [f"not a zip: {exc}"]
    why, prefix = zip_problems(zf)
    if why:
        return why
    dest.mkdir(parents=True, exist_ok=False)
    budget = C.MAX_BYTES
    for zi in zf.infolist():
        if zi.filename.endswith("/"):
            continue
        rel = zi.filename[len(prefix):]
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(zi) as src, open(out, "wb") as dst:
            left = zi.file_size
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                left -= len(chunk)
                budget -= len(chunk)
                if left < 0 or budget < 0:
                    return [f"{zi.filename}: bigger than the zip says (or than {C.MAX_BYTES // 1_000_000} MB)"]
                dst.write(chunk)
    return []


def check_zip_bytes(data: bytes, want_id: str = "") -> tuple[dict, list[str]]:
    """(manifest, reasons): unzip into a temporary <id> folder and run check.py on it."""
    with tempfile.TemporaryDirectory(prefix="mmpkg_") as tmp:
        stage = Path(tmp) / "stage"
        why = extract(data, stage)
        if why:
            return {}, why
        try:
            m = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}, [f"manifest.json does not parse: {exc}"]
        pid = m.get("id") if isinstance(m, dict) and isinstance(m.get("id"), str) else ""
        if not pid or not C.ID_RE.match(pid):
            return (m if isinstance(m, dict) else {}), C.check_manifest(m) or ["no valid id"]
        folder = Path(tmp) / pid
        stage.rename(folder)
        m, why = C.validate(folder)
        if want_id and m.get("id") != want_id:
            why.append(f"the zip's id is {m.get('id')}, not {want_id}")
        return m, why


def fetch(url: str, limit: int) -> bytes:
    if not re.match(r"^https?://", url):
        raise ValueError("only http(s) sources")
    req = urllib.request.Request(url, headers={"User-Agent": "magicmirror-market-tool"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - http(s) only, checked above
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"bigger than {limit // 1_000_000} MB")
    return data


# ---------------------------------------------------------------- entries (packages/<id>/<version>.json)
def check_entry(path: Path, zip_bytes: bytes | None = None) -> tuple[dict, list[str], bytes]:
    """(entry, reasons, zip bytes) for one submitted file. Downloads the source unless zip_bytes is given."""
    path = Path(path)
    why: list[str] = []
    try:
        e = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"{path.name}: does not parse: {exc}"], b""
    if not isinstance(e, dict):
        return {}, ["the entry is not a JSON object"], b""
    extra = sorted(set(e) - ENTRY_KEYS)
    if extra:
        why.append("unknown field(s): " + ", ".join(extra))
    m = e.get("manifest")
    mwhy = C.check_manifest(m)
    why += ["manifest: " + w for w in mwhy]
    if not isinstance(m, dict):
        return e, why, b""
    pid, ver = m.get("id", ""), m.get("version", "")
    if path.parent.name != pid or path.name != f"{ver}.json":
        why.append(f"the file must be packages/{pid}/{ver}.json")
    src, sha = e.get("source"), e.get("sha256")
    if not (isinstance(src, str) and src.startswith("https://")):
        why.append("`source` must be an https:// address of the zip (a GitHub release asset)")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        why.append("`sha256` must be the zip's sha256, 64 lower-case hex characters")
    th = e.get("thumb")
    if th is not None:
        tp = path.parent / str(th)
        if not (isinstance(th, str) and re.match(r"^[A-Za-z0-9_\-]+\.png$", th) and tp.is_file()):
            why.append("`thumb` must name a .png beside the entry")
        elif tp.stat().st_size > MAX_THUMB_BYTES:
            why.append(f"thumb larger than {MAX_THUMB_BYTES // 1024} kB")
    if why:
        return e, why, b""
    if zip_bytes is None:
        try:
            zip_bytes = fetch(src, MAX_ZIP_BYTES)
        except Exception as exc:  # noqa: BLE001 - any download problem is a reason
            return e, [f"could not download the source: {exc}"], b""
    got = hashlib.sha256(zip_bytes).hexdigest()
    if got != sha:
        return e, [f"sha256 differs: the zip is {got}"], zip_bytes
    zm, zwhy = check_zip_bytes(zip_bytes, pid)
    why += zwhy
    if not zwhy and zm != m:
        why.append("the entry's manifest is not the zip's manifest.json")
    return e, why, zip_bytes


def entries(base: Path = PACKAGES_DIR) -> list[Path]:
    return sorted(p for p in Path(base).glob("*/*.json") if C.SEMVER_RE.match(p.stem))


def _semver(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


# ---------------------------------------------------------------- the index
def check_index(idx) -> list[str]:
    """The signed index's shape (index.schema.json says the same). [] = fine."""
    if not isinstance(idx, dict):
        return ["the index is not a JSON object"]
    why = []
    if idx.get("schema") != INDEX_SCHEMA:
        why.append(f"`schema` must be {INDEX_SCHEMA}")
    if not (isinstance(idx.get("serial"), int) and not isinstance(idx.get("serial"), bool) and idx["serial"] >= 0):
        why.append("`serial` must be a whole number")
    pk = idx.get("packages")
    if not isinstance(pk, list):
        why.append("`packages` must be a list")
        pk = []
    seen = set()
    for i, p in enumerate(pk):
        if not isinstance(p, dict):
            why.append(f"packages[{i}] is not an object")
            continue
        m = p.get("manifest")
        mw = C.check_manifest(m)
        if mw:
            why.append(f"packages[{i}]: " + mw[0])
            continue
        if (m["id"], m["version"]) in seen:
            why.append(f"packages[{i}]: {m['id']} {m['version']} twice")
        seen.add((m["id"], m["version"]))
        if p.get("id") != m["id"] or p.get("version") != m["version"]:
            why.append(f"packages[{i}]: id / version differ from its manifest")
        if not (isinstance(p.get("sha256"), str) and SHA_RE.match(p["sha256"])):
            why.append(f"packages[{i}]: bad sha256")
        if not (isinstance(p.get("source"), str) and re.match(r"^https?://", p["source"])):
            why.append(f"packages[{i}]: bad source")
        if not (isinstance(p.get("size"), int) and 0 < p["size"] <= MAX_ZIP_BYTES):
            why.append(f"packages[{i}]: bad size")
        if not isinstance(p.get("sig"), str):
            why.append(f"packages[{i}]: no package signature")
        th = p.get("thumb")
        if th is not None and not (isinstance(th, dict) and isinstance(th.get("url"), str)
                                   and isinstance(th.get("sha256"), str) and SHA_RE.match(th["sha256"])):
            why.append(f"packages[{i}]: bad thumb")
    bl = idx.get("blocked", [])
    if not isinstance(bl, list) or not all(isinstance(b, dict) and isinstance(b.get("id"), str) for b in bl):
        why.append("`blocked` must be a list of {id, why}")
    return why


def index_bytes(idx: dict) -> bytes:
    return (json.dumps(idx, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sign_index(idx: dict, priv) -> tuple[bytes, bytes]:
    data = index_bytes(idx)
    raw = public_raw(priv)
    sig = {"alg": "ed25519", "key": key_id(raw), "sig": sign(priv, data)}
    return data, (json.dumps(sig, sort_keys=True) + "\n").encode()


def verify_index_bytes(data: bytes, sig_data: bytes, keys: dict) -> tuple[dict, list[str]]:
    try:
        sig = json.loads(sig_data)
    except ValueError:
        return {}, ["index.json.sig does not parse"]
    if not isinstance(sig, dict) or sig.get("alg") != "ed25519" or sig.get("key") not in keys:
        return {}, ["index.json.sig: unknown key or algorithm"]
    if not verify(keys[sig["key"]], data, str(sig.get("sig", ""))):
        return {}, ["index.json: the signature does not verify"]
    try:
        idx = json.loads(data)
    except ValueError:
        return {}, ["index.json does not parse"]
    why = check_index(idx)
    for p in idx.get("packages", []) if not why else []:
        if not verify(keys[sig["key"]], package_message(p["id"], p["version"], p["sha256"]), p["sig"]):
            why.append(f"{p['id']} {p['version']}: the package signature does not verify")
    return idx, why


def build_index(priv, base: Path = REPO, raw_base: str = DEFAULT_RAW_BASE, zips: dict | None = None,
                serial: int | None = None, now: str | None = None) -> tuple[bytes, bytes, list[str]]:
    """Rebuild index.json from packages/*/*.json (each checked again) + blocked.json; sign it."""
    base = Path(base)
    out, problems = [], []
    for p in entries(base / "packages"):
        e, why, data = check_entry(p, (zips or {}).get(str(p.relative_to(base))))
        if why:
            problems.append(f"{p.relative_to(base)}: " + "; ".join(why))
            continue
        m = e["manifest"]
        row = {"id": m["id"], "version": m["version"], "manifest": m, "source": e["source"], "sha256": e["sha256"],
               "size": len(data), "sig": sign(priv, package_message(m["id"], m["version"], e["sha256"]))}
        if e.get("thumb"):
            tp = p.parent / e["thumb"]
            row["thumb"] = {"url": raw_base + tp.relative_to(base).as_posix(),
                            "sha256": hashlib.sha256(tp.read_bytes()).hexdigest()}
        out.append(row)
    out.sort(key=lambda r: (r["id"], _semver(r["version"])))
    blocked = []
    bf = base / "blocked.json"
    if bf.is_file():
        blocked = json.loads(bf.read_text(encoding="utf-8"))
    old = 0
    if serial is None and (base / "index.json").is_file():
        try:
            old = int(json.loads((base / "index.json").read_text(encoding="utf-8")).get("serial", 0))
        except (ValueError, TypeError):
            old = 0
    idx = {"schema": INDEX_SCHEMA, "serial": serial if serial is not None else old + 1,
           "generated": now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "packages": out, "blocked": blocked}
    data, sig = sign_index(idx, priv)
    return data, sig, problems


# ---------------------------------------------------------------- the CI
def ci(base: Path = REPO, only: list[str] | None = None) -> int:
    base = Path(base)
    bad = 0
    todo = [Path(x) for x in only] if only else entries(base / "packages")
    for p in todo:
        if not p.is_file():
            continue
        _, why, _ = check_entry(p)
        print(("OK      " if not why else "REFUSED ") + str(p))
        for w in why:
            print("        - " + w)
        bad += bool(why)
    idx_f, sig_f = base / "index.json", base / "index.json.sig"
    if idx_f.is_file():
        idx, why = verify_index_bytes(idx_f.read_bytes(), sig_f.read_bytes() if sig_f.is_file() else b"",
                                      read_public_keys(base / "keys"))
        print(("OK      " if not why else "REFUSED ") + "index.json (signature, shape, package signatures)")
        for w in why:
            print("        - " + w)
        bad += bool(why)
        for p in idx.get("packages", []) if not why else []:
            ef = base / "packages" / p["id"] / f"{p['version']}.json"
            if not ef.is_file():
                print(f"REFUSED index.json names {p['id']} {p['version']} but {ef.relative_to(base)} is missing")
                bad += 1
            elif json.loads(ef.read_text(encoding="utf-8")).get("sha256") != p["sha256"]:
                print(f"REFUSED index.json: {p['id']} {p['version']} sha256 differs from its entry")
                bad += 1
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("check-zip"); a.add_argument("zip")  # noqa: E702
    a = sub.add_parser("check-entry"); a.add_argument("entry"); a.add_argument("--zip")  # noqa: E702
    a = sub.add_parser("ci"); a.add_argument("entries", nargs="*")  # noqa: E702
    a = sub.add_parser("verify-index")
    a.add_argument("--index", default=str(REPO / "index.json"))
    a.add_argument("--keys", default=str(KEYS_DIR))
    a = sub.add_parser("build-index")
    a.add_argument("--key")
    a.add_argument("--raw-base", default=DEFAULT_RAW_BASE)
    a.add_argument("--serial", type=int)
    a = sub.add_parser("keygen")
    a.add_argument("--out", required=True)
    a.add_argument("--pub")
    a.add_argument("--comment", default="")
    a = sub.add_parser("pubkey"); a.add_argument("--key")  # noqa: E702
    ns = ap.parse_args(argv)
    if ns.cmd == "check-zip":
        m, why = check_zip_bytes(Path(ns.zip).read_bytes())
        print(("OK      " if not why else "REFUSED ") + ns.zip + (f"  ({m.get('id')} {m.get('version')})" if m else ""))
        for w in why:
            print("        - " + w)
        print("sha256  " + hashlib.sha256(Path(ns.zip).read_bytes()).hexdigest())
        return 1 if why else 0
    if ns.cmd == "check-entry":
        _, why, _ = check_entry(Path(ns.entry), Path(ns.zip).read_bytes() if ns.zip else None)
        print(("OK      " if not why else "REFUSED ") + ns.entry)
        for w in why:
            print("        - " + w)
        return 1 if why else 0
    if ns.cmd == "ci":
        return ci(REPO, ns.entries or None)
    if ns.cmd == "verify-index":
        f = Path(ns.index)
        sf = Path(str(f) + ".sig")
        idx, why = verify_index_bytes(f.read_bytes(), sf.read_bytes() if sf.is_file() else b"",
                                      read_public_keys(Path(ns.keys)))
        print(("OK      " if not why else "REFUSED ") + f"{f}  serial {idx.get('serial')}, "
              f"{len(idx.get('packages', []))} package(s)")
        for w in why:
            print("        - " + w)
        return 1 if why else 0
    if ns.cmd == "build-index":
        priv = load_private_key(ns.key)
        if key_id(public_raw(priv)) not in read_public_keys(KEYS_DIR):
            print("REFUSED: this private key's public half is not in keys/")
            return 1
        data, sig, problems = build_index(priv, REPO, ns.raw_base, serial=ns.serial)
        for p in problems:
            print("SKIPPED " + p)
        (REPO / "index.json").write_bytes(data)
        (REPO / "index.json.sig").write_bytes(sig)
        print(f"wrote index.json ({len(json.loads(data)['packages'])} package(s)) and index.json.sig")
        return 0
    if ns.cmd == "keygen":
        print(keygen(Path(ns.out), Path(ns.pub) if ns.pub else None, ns.comment), end="")
        return 0
    if ns.cmd == "pubkey":
        raw = public_raw(load_private_key(ns.key))
        print(base64.b64encode(raw).decode() + f"  (key id {key_id(raw)})")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
