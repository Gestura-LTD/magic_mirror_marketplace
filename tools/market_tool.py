#!/usr/bin/env python3
"""market_tool.py -- the Marketplace index's own tool: check submitted packages, build and sign index.json.

    python3 tools/market_tool.py make-zip   path/to/<id>                # authors: a clean <id>-<version>.zip
    python3 tools/market_tool.py check-zip  game.zip                    # is my zip a valid package?
    python3 tools/market_tool.py make-entry --zip <id>-<version>.zip --source https://.../<id>-<version>.zip \
                                            [--thumb tile.png] [--notes "First version."]   # writes packages/<id>/
    python3 tools/market_tool.py check-entry packages/<id>/<version>.json [--zip local.zip]   # what the CI runs
    python3 tools/market_tool.py ci                                    # every entry + the signed index (the CI)
    python3 tools/market_tool.py verify-index                          # index.json + index.json.sig vs keys/
    python3 tools/market_tool.py build-index --key PRIVATE.pem [--days 30]   # maintainers: rebuild + sign the index
    python3 tools/market_tool.py keygen --out PRIVATE.pem --pub keys/marketplace_ed25519.pub [--passphrase]
    python3 tools/market_tool.py make-root --version 1 --root-pub a.pub --root-pub b.pub --root-pub c.pub \
                                          --threshold 2 --index-pub keys/marketplace_ed25519.pub --out root/1.json
    python3 tools/market_tool.py sign-root root/1.json --key ROOT_A.pem       # each root key holder, offline
    python3 tools/market_tool.py verify-root                                  # root/1.json .. root/N.json, as a chain

How a game is trusted (the mirror checks the same, offline, with the public key in keys/):

  * index.json is signed as a whole (Ed25519, the signature in index.json.sig). A mirror refuses an index whose
    signature does not verify, or whose `serial` is lower than one it has already seen (no replay of an old list,
    for example one from before a take-down).
  * schema 2 (R2b, 2026-11-22): the signature covers "magicmirror-index-v2\n" + the exact bytes (a signature made
    for anything else can never pass as an index signature), and the index carries `expires` (`generated` + 30 days
    by default, `--days`) and `root` (the root.json version it was signed under). A mirror refuses an expired list
    for installs, so a network that keeps serving an old list ("freeze") cannot hide a take-down for ever.
    Mirrors accept schema 1 (signature over the bare bytes, no expiry) and schema 2 during the switch.
  * blocked.json entries: {"id"?, "version"?, "sha256"?, "author"?, "minVersion"?, "why"} -- every field given must
    match; "minVersion" blocks only versions below it; an entry needs at least one of id / sha256 / author.
  * every package in the index carries its own Ed25519 signature `sig` over
        "magicmirror-package-v1\\n<id>\\n<version>\\n<sha256 of the zip>\\n"
    so the zip a mirror downloads must have exactly the reviewed bytes, for exactly that id and version.
  * the mirror then unzips into a staging folder (no links, no paths outside, size limits), runs the same checker
    as tools/check.py, and only then moves the game into its packages folder.

Root of trust (R2b, 2026-11-22): root/<N>.json lists the root keys (threshold, e.g. 2 of 3, kept offline by three
people), the index key(s) that may sign index.json, and `revoked` key ids. It is signed over
"magicmirror-root-v1\n" + the canonical JSON of its "signed" part. Root N+1 must carry a threshold of root N's root
keys AND of its own. The mirror ships root/1.json as mirror_qt/market_trust/root.json and follows the chain
(root/2.json, root/3.json, ... until a 404) at every refresh, so a stolen index key is revoked by publishing one new
root file -- no new app. Until a root exists, keys/*.pub is the trust (the mirror falls back the same way).

The private keys never enter this repository. Keys are made with `keygen --passphrase` (an encrypted PEM; the
passphrase is asked for, or read from MARKET_KEY_PASSPHRASE in scripts). Python 3.9+, standard library +
`cryptography` (only for keygen / signing / verifying).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import getpass
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

INDEX_SCHEMA = 2                    # what build-index writes
ACCEPTED_SCHEMAS = (1, 2)           # what a mirror accepts during the switch (then drop 1)
PACKAGE_SIG_CONTEXT = "magicmirror-package-v1"
INDEX_SIG_CONTEXT = "magicmirror-index-v2"
INDEX_DAYS = 30
ROOT_SIG_CONTEXT = "magicmirror-root-v1"
ROOT_DAYS = 730
ROOT_DIR = REPO / "root"
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
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


def index_message(data: bytes) -> bytes:
    """What a schema 2 index signature covers: the role, a newline, the exact bytes of index.json."""
    return (INDEX_SIG_CONTEXT + "\n").encode("utf-8") + data


def parse_time(v) -> float | None:
    """"2026-11-22T00:00:00Z" -> seconds since 1970 (UTC), or None."""
    if not (isinstance(v, str) and TIME_RE.match(v)):
        return None
    try:
        return _dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def fmt_time(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_blocked(bl, schema: int) -> list[str]:
    """The take-down list's shape. The same function is in mirror_qt/market_store.py and market_tool.py."""
    if not isinstance(bl, list):
        return ["`blocked` must be a list"]
    why = []
    for i, b in enumerate(bl):
        if not isinstance(b, dict):
            why.append(f"blocked[{i}] is not an object")
            continue
        if schema < 2 and not isinstance(b.get("id"), str):
            why.append(f"blocked[{i}]: schema 1 needs an id")
            continue
        if not any(isinstance(b.get(k), str) and b[k] for k in ("id", "sha256", "author")):
            why.append(f"blocked[{i}]: needs an id, a sha256 or an author")
        for k in ("id", "author", "why"):
            if k in b and not isinstance(b[k], str):
                why.append(f"blocked[{i}]: `{k}` must be text")
        for k in ("version", "minVersion"):
            if k in b and not (isinstance(b[k], str) and SEMVER_RE.match(b[k])):
                why.append(f"blocked[{i}]: `{k}` must be x.y.z")
        if "sha256" in b and not (isinstance(b["sha256"], str) and SHA_RE.match(b["sha256"])):
            why.append(f"blocked[{i}]: bad sha256")
    return why


def _semver_t(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (-1,)


def blocked_reason(blocked, pkg_id: str, version: str, sha256: str = "", author: str = "") -> str:
    """Why this game version is taken down ("" = it is not). Every field an entry gives must match; `minVersion`
    blocks only versions below it; an entry without id / sha256 / author never matches (never "block all")."""
    for b in blocked if isinstance(blocked, list) else []:
        if not isinstance(b, dict) or not any(isinstance(b.get(k), str) and b[k] for k in ("id", "sha256", "author")):
            continue
        if "id" in b and b["id"] != pkg_id:
            continue
        if "version" in b and b["version"] != version:
            continue
        if "sha256" in b and b["sha256"] != sha256:
            continue
        if "author" in b and b["author"] != author:
            continue
        if "minVersion" in b and _semver_t(version) >= _semver_t(b["minVersion"]):
            continue
        return str(b.get("why") or "taken down by the group")
    return ""


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


def _passphrase(prompt: str, twice: bool = False) -> bytes:
    env = os.environ.get("MARKET_KEY_PASSPHRASE")
    if env:
        return env.encode("utf-8")
    pw = getpass.getpass(prompt)
    if twice and getpass.getpass("again: ") != pw:
        raise SystemExit("the two passphrases differ")
    if len(pw) < 12:
        raise SystemExit("use a passphrase of at least 12 characters")
    return pw.encode("utf-8")


def load_private_key(path: str | None, passphrase: bytes | None = None):
    """A PEM private key; an encrypted one asks for its passphrase (or reads MARKET_KEY_PASSPHRASE)."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    if path:
        pem = Path(path).read_bytes()
    elif os.environ.get("MARKET_SIGNING_KEY"):
        pem = os.environ["MARKET_SIGNING_KEY"].encode()
    elif os.environ.get("MARKET_SIGNING_KEY_FILE"):
        pem = Path(os.environ["MARKET_SIGNING_KEY_FILE"]).read_bytes()
    else:
        raise SystemExit("no signing key: --key PRIVATE.pem, or MARKET_SIGNING_KEY (PEM text)")
    if b"ENCRYPTED" in pem and passphrase is None:
        passphrase = _passphrase(f"passphrase for {path or 'the signing key'}: ")
    return serialization.load_pem_private_key(pem, password=passphrase if b"ENCRYPTED" in pem else None)


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


def keygen(out: Path, pub: Path | None, comment: str = "", passphrase: bytes | None = None) -> str:
    """A new Ed25519 key: the private PEM (mode 0600; encrypted with `passphrase` when given -- always for real
    keys, see --passphrase) and the public line."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; not overwritten")
    priv = Ed25519PrivateKey.generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    raw = public_raw(priv)
    line = base64.b64encode(raw).decode() + (f"  # {comment}" if comment else "") + f"  (key id {key_id(raw)})\n"
    if pub:
        Path(pub).parent.mkdir(parents=True, exist_ok=True)
        Path(pub).write_text(line, encoding="utf-8")
    return line


def encrypt_key(src: Path, out: Path, passphrase: bytes) -> None:
    """Re-write an unencrypted PEM (today's dev key) as a passphrase-encrypted one. The source is left alone."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    priv = load_private_key(str(src))
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; not overwritten")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.BestAvailableEncryption(passphrase)))


# ---------------------------------------------------------------- the root of trust (root/<N>.json)
def canonical(obj) -> bytes:
    """The one byte form that is signed (the mirror's market_store.canonical is the same)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def root_message(signed: dict) -> bytes:
    return (ROOT_SIG_CONTEXT + "\n").encode("utf-8") + canonical(signed)


def check_root(root) -> list[str]:
    """The shape of a root file: {"signed": {...}, "signatures": [{"key", "sig"}]}. The mirror checks the same."""
    if not isinstance(root, dict) or not isinstance(root.get("signed"), dict) \
            or not isinstance(root.get("signatures"), list):
        return ["a root file is {signed, signatures}"]
    s = root["signed"]
    why = []
    if s.get("type") != "root":
        why.append("`type` must be root")
    if not (isinstance(s.get("version"), int) and not isinstance(s.get("version"), bool) and s["version"] >= 1):
        why.append("`version` must be 1 or more")
    if parse_time(s.get("expires")) is None:
        why.append("`expires` must be a UTC time")
    keys = s.get("keys")
    if not isinstance(keys, dict) or not keys:
        return why + ["`keys` must map key ids to public keys"]
    for kid, b64 in keys.items():
        try:
            raw = base64.b64decode(str(b64), validate=True)
        except ValueError:
            raw = b""
        if len(raw) != 32 or key_id(raw) != kid:
            why.append(f"key {kid}: not an Ed25519 public key with that id")
    rev = s.get("revoked", [])
    if not (isinstance(rev, list) and all(isinstance(r, str) for r in rev)):
        why.append("`revoked` must be a list of key ids")
        rev = []
    roles = s.get("roles")
    for role in ("root", "index"):
        r = roles.get(role) if isinstance(roles, dict) else None
        if not isinstance(r, dict) or not isinstance(r.get("keyids"), list) or not r["keyids"]:
            why.append(f"roles.{role} needs keyids")
            continue
        ids = r["keyids"]
        if len(set(ids)) != len(ids) or not all(i in keys for i in ids):
            why.append(f"roles.{role}: every key id once, and listed in `keys`")
        if set(ids) & set(rev):
            why.append(f"roles.{role}: names a revoked key")
        t = r.get("threshold")
        if not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= len(ids)):
            why.append(f"roles.{role}: threshold must be 1 .. {len(ids)}")
    return why


def role_keys(signed: dict, role: str) -> dict:
    """{key id: raw} of a role, revoked keys left out."""
    rev = set(signed.get("revoked", []))
    out = {}
    for kid in signed["roles"][role]["keyids"]:
        if kid not in rev:
            out[kid] = base64.b64decode(signed["keys"][kid])
    return out


def good_sigs(root: dict, keys: dict) -> set:
    """The key ids (of `keys`) with a valid signature on this root."""
    msg = root_message(root["signed"])
    ok = set()
    for s in root.get("signatures", []):
        if isinstance(s, dict) and s.get("key") in keys and s["key"] not in ok \
                and verify(keys[s["key"]], msg, str(s.get("sig", ""))):
            ok.add(s["key"])
    return ok


def verify_root(old: dict | None, new: dict) -> list[str]:
    """Is `new` the next trusted root after `old` (None = the first one: signed by its own threshold only)?
    Root N+1 needs version N+1 and a threshold of root N's root keys AND of its own. Expiry is checked by the
    caller (only the newest root must be unexpired)."""
    why = check_root(new)
    if why:
        return why
    s = new["signed"]
    if old is not None:
        if s["version"] != old["signed"]["version"] + 1:
            return [f"root version {s['version']} does not follow {old['signed']['version']}"]
        n_old = len(good_sigs(new, role_keys(old["signed"], "root")))
        if n_old < old["signed"]["roles"]["root"]["threshold"]:
            return [f"root {s['version']}: {n_old} of the old root keys signed it, "
                    f"{old['signed']['roles']['root']['threshold']} needed"]
    n_new = len(good_sigs(new, role_keys(s, "root")))
    if n_new < s["roles"]["root"]["threshold"]:
        return [f"root {s['version']}: {n_new} of its own root keys signed it, {s['roles']['root']['threshold']} needed"]
    return []


def make_root(version: int, root_pubs: dict, index_pubs: dict, threshold: int, index_threshold: int = 1,
              revoked=(), days: int = ROOT_DAYS, now: float | None = None) -> dict:
    """A new, unsigned root file (each root key holder then runs sign-root)."""
    now = _dt.datetime.now(_dt.timezone.utc).timestamp() if now is None else now
    keys = {k: base64.b64encode(v).decode() for k, v in {**root_pubs, **index_pubs}.items()}
    signed = {"type": "root", "version": int(version), "expires": fmt_time(now + int(days) * 86400), "keys": keys,
              "roles": {"root": {"keyids": sorted(root_pubs), "threshold": int(threshold)},
                        "index": {"keyids": sorted(index_pubs), "threshold": int(index_threshold)}},
              "revoked": sorted(set(revoked))}
    root = {"signed": signed, "signatures": []}
    why = check_root(root)
    if why:
        raise SystemExit("make-root: " + "; ".join(why))
    return root


def sign_root(root: dict, priv) -> dict:
    """Add (or replace) this key's signature."""
    kid = key_id(public_raw(priv))
    sigs = [s for s in root.get("signatures", []) if s.get("key") != kid]
    sigs.append({"key": kid, "sig": sign(priv, root_message(root["signed"]))})
    return {"signed": root["signed"], "signatures": sorted(sigs, key=lambda s: s["key"])}


def root_bytes(root: dict) -> bytes:
    return (json.dumps(root, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def root_chain(base: Path = REPO) -> tuple[dict | None, list[str]]:
    """(the newest root, reasons): root/1.json .. root/N.json checked as a chain. (None, []) = no root yet."""
    d = Path(base) / "root"
    n = latest_root_version(base)
    if not n:
        return None, []
    cur = None
    for v in range(1, n + 1):
        f = d / f"{v}.json"
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, [f"root/{v}.json: {exc}"]
        why = verify_root(cur, r)
        if why:
            return None, [f"root/{v}.json: " + w for w in why]
        cur = r
    return cur, []


def index_trust(base: Path = REPO) -> tuple[dict, set, list[str]]:
    """({key id: raw} that may sign index.json, revoked ids, reasons): the newest root's index role, or keys/*.pub
    while there is no root yet."""
    root, why = root_chain(base)
    if why:
        return {}, set(), why
    if root is None:
        return read_public_keys(Path(base) / "keys"), set(), []
    return role_keys(root["signed"], "index"), set(root["signed"].get("revoked", [])), []


# ---------------------------------------------------------------- zips
WIN_RESERVED = frozenset(["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)]
                         + [f"LPT{i}" for i in range(1, 10)])


def win_name_problem(name: str) -> str:
    """Why a zip path would misbehave on Windows ("" = fine): a ':' (an NTFS alternate data stream), a reserved
    device name with any extension (CON, NUL.qml, aux.png, COM1..9, LPT1..9), a part ending in a dot or a space,
    or a control character. The same function is in mirror_qt/market_store.py and market_tool.py."""
    for part in name.rstrip("/").split("/"):
        if ":" in part:
            return "a ':' in a name (Windows alternate data stream)"
        if any(ord(c) < 32 for c in part) or any(c in part for c in '<>"|?*'):
            return "a character Windows does not allow in a name"
        if part.split(".")[0].upper().rstrip(" ") in WIN_RESERVED:
            return "a Windows reserved name (CON, PRN, AUX, NUL, COM1-9, LPT1-9)"
        if part.endswith(".") or part.endswith(" "):
            return "a name ending in a dot or a space"
    return ""


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
        wp = win_name_problem(n)
        if wp:
            why.append(f"{n!r}: {wp}")
            continue
        if n.endswith("/"):
            continue
        names.append(n)
        total += zi.file_size
    if len(names) != len(set(names)):
        why.append("the zip names the same file twice")
    elif len({n.casefold() for n in names}) != len(names):
        why.append("two names differ only by upper / lower case (one would replace the other on Windows)")
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
    if not url.startswith("https://"):
        raise ValueError("only https:// sources (R2b: no plain http)")
    req = urllib.request.Request(url, headers={"User-Agent": "magicmirror-market-tool"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - https only, checked above
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
    schema = idx.get("schema")
    if schema not in ACCEPTED_SCHEMAS or isinstance(schema, bool):
        why.append(f"`schema` must be one of {ACCEPTED_SCHEMAS}")
        schema = INDEX_SCHEMA
    if not (isinstance(idx.get("serial"), int) and not isinstance(idx.get("serial"), bool) and idx["serial"] >= 0):
        why.append("`serial` must be a whole number")
    if schema >= 2:
        gen, exp = parse_time(idx.get("generated")), parse_time(idx.get("expires"))
        if gen is None:
            why.append("`generated` must be a UTC time like 2026-11-22T00:00:00Z")
        if exp is None:
            why.append("`expires` must be a UTC time like 2026-12-22T00:00:00Z")
        elif gen is not None and exp <= gen:
            why.append("`expires` must be after `generated`")
        if not (isinstance(idx.get("root"), int) and not isinstance(idx.get("root"), bool) and idx["root"] >= 0):
            why.append("`root` must be the root.json version (a whole number)")
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
        if not (isinstance(p.get("source"), str) and p["source"].startswith("https://")):
            why.append(f"packages[{i}]: bad source")
        if not (isinstance(p.get("size"), int) and 0 < p["size"] <= MAX_ZIP_BYTES):
            why.append(f"packages[{i}]: bad size")
        if not isinstance(p.get("sig"), str):
            why.append(f"packages[{i}]: no package signature")
        th = p.get("thumb")
        if th is not None and not (isinstance(th, dict) and isinstance(th.get("url"), str)
                                   and th["url"].startswith("https://")
                                   and isinstance(th.get("sha256"), str) and SHA_RE.match(th["sha256"])):
            why.append(f"packages[{i}]: bad thumb")
    why += check_blocked(idx.get("blocked", []), schema)
    return why


def index_bytes(idx: dict) -> bytes:
    return (json.dumps(idx, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sign_index(idx: dict, priv) -> tuple[bytes, bytes]:
    """(index.json bytes, index.json.sig bytes). Schema 2 signs "magicmirror-index-v2\n" + bytes; schema 1 (tests and
    the old live list only) signs the bare bytes."""
    data = index_bytes(idx)
    raw = public_raw(priv)
    if int(idx.get("schema", INDEX_SCHEMA)) >= 2:
        sig = {"alg": "ed25519", "ctx": INDEX_SIG_CONTEXT, "key": key_id(raw), "sig": sign(priv, index_message(data))}
    else:
        sig = {"alg": "ed25519", "key": key_id(raw), "sig": sign(priv, data)}
    return data, (json.dumps(sig, sort_keys=True) + "\n").encode()


def index_sig_schema(raw_pub: bytes, data: bytes, sig_b64: str) -> int:
    """2 = a valid domain-separated (schema 2) signature, 1 = a valid bare-bytes (schema 1) one, 0 = neither."""
    if verify(raw_pub, index_message(data), sig_b64):
        return 2
    if 1 in ACCEPTED_SCHEMAS and verify(raw_pub, data, sig_b64):
        return 1
    return 0


def verify_index_bytes(data: bytes, sig_data: bytes, keys: dict, revoked=()) -> tuple[dict, list[str]]:
    try:
        sig = json.loads(sig_data)
    except ValueError:
        return {}, ["index.json.sig does not parse"]
    if isinstance(sig, dict) and sig.get("key") in set(revoked):
        return {}, [f"index.json.sig: signed with a revoked key ({sig.get('key')})"]
    if not isinstance(sig, dict) or sig.get("alg") != "ed25519" or sig.get("key") not in keys:
        return {}, ["index.json.sig: unknown key or algorithm"]
    signed_as = index_sig_schema(keys[sig["key"]], data, str(sig.get("sig", "")))
    if not signed_as:
        return {}, ["index.json: the signature does not verify"]
    try:
        idx = json.loads(data)
    except ValueError:
        return {}, ["index.json does not parse"]
    if isinstance(idx, dict) and idx.get("schema") != signed_as:
        return {}, [f"index.json: signed as schema {signed_as} but says schema {idx.get('schema')}"]
    why = check_index(idx)
    for p in idx.get("packages", []) if not why else []:
        if not verify(keys[sig["key"]], package_message(p["id"], p["version"], p["sha256"]), p["sig"]):
            why.append(f"{p['id']} {p['version']}: the package signature does not verify")
    return idx, why


def build_index(priv, base: Path = REPO, raw_base: str = DEFAULT_RAW_BASE, zips: dict | None = None,
                serial: int | None = None, now: str | None = None, days: int = INDEX_DAYS,
                root_version: int | None = None) -> tuple[bytes, bytes, list[str]]:
    """Rebuild index.json (schema 2) from packages/*/*.json (each checked again) + blocked.json; sign it.
    `expires` = `generated` + `days`; `root` = the newest root/<N>.json in the repo (0 while there is none)."""
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
    bwhy = check_blocked(blocked, INDEX_SCHEMA)
    if bwhy:
        raise SystemExit("blocked.json: " + "; ".join(bwhy))
    old = 0
    if serial is None and (base / "index.json").is_file():
        try:
            old = int(json.loads((base / "index.json").read_text(encoding="utf-8")).get("serial", 0))
        except (ValueError, TypeError):
            old = 0
    if not 1 <= int(days) <= 366:
        raise SystemExit("--days must be 1 .. 366")
    generated = now or fmt_time(_dt.datetime.now(_dt.timezone.utc).timestamp())
    if root_version is None:
        root_version = latest_root_version(base)
    idx = {"schema": INDEX_SCHEMA, "serial": serial if serial is not None else old + 1,
           "generated": generated, "expires": fmt_time(parse_time(generated) + int(days) * 86400),
           "root": int(root_version), "packages": out, "blocked": blocked}
    data, sig = sign_index(idx, priv)
    return data, sig, problems


def latest_root_version(base: Path = REPO) -> int:
    """The highest N of root/<N>.json in the repo (0 = no root yet)."""
    vs = [int(p.stem) for p in (Path(base) / "root").glob("*.json") if p.stem.isdigit()]
    return max(vs) if vs else 0


# ---------------------------------------------------------------- authors' helpers (make-zip, make-entry)
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX", "__pycache__"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)     # a fixed date: the same folder always gives the same bytes (and sha256)


def junk_hint(names) -> str:
    """A friendlier word for the usual cause of 'no manifest.json at the top': a zip made by macOS Finder
    (__MACOSX/, ._files, .DS_Store) or Windows (Thumbs.db). "" = none of these."""
    bad = sorted({n.split("/")[0] if n.startswith("__MACOSX") else n for n in names
                  if n.startswith("__MACOSX") or any(part in JUNK_NAMES or part.startswith("._")
                                                     for part in n.rstrip("/").split("/"))})
    if not bad:
        return ""
    return ("the zip holds system files (" + ", ".join(bad[:4]) + (", ..." if len(bad) > 4 else "") +
            "), usually from Finder's 'Compress' or Explorer. Make the zip with "
            "`python3 tools/market_tool.py make-zip <your folder>` instead")


def make_zip(folder: Path, out_dir: Path) -> tuple[Path | None, list[str]]:
    """Zip a package folder as <id>/... into out_dir/<id>-<version>.zip, leaving out hidden and system files.
    Runs the full package check first (check.py) and refuses an invalid folder."""
    folder = Path(folder).resolve()
    m, why = C.validate(folder)
    if why:
        return None, why
    out = Path(out_dir).resolve() / f"{m['id']}-{m['version']}.zip"
    if folder == out.parent or folder in out.parents:
        return None, ["write the zip outside the package folder (--out)"]
    files = []
    for f in sorted(folder.rglob("*")):
        rel = f.relative_to(folder)
        if any(part in JUNK_NAMES or part.startswith(".") for part in rel.parts):
            continue
        if f.is_symlink():
            return None, [f"{rel.as_posix()}: links are not allowed"]
        if f.is_file():
            files.append((rel.as_posix(), f))
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, f in files:
            zi = zipfile.ZipInfo(f"{m['id']}/{rel}", ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(zi, f.read_bytes())
    _, zwhy = check_zip_bytes(out.read_bytes(), m["id"])
    return out, zwhy


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def make_entry(zip_path: Path, source: str, thumb: Path | None = None, notes: str = "",
               base: Path = REPO) -> tuple[Path | None, list[str], list[str]]:
    """Write packages/<id>/<version>.json (and thumb.png) for a checked zip. (path, problems, warnings)."""
    data = Path(zip_path).read_bytes()
    m, why = check_zip_bytes(data)
    if why:
        return None, why, []
    warn = []
    if not (isinstance(source, str) and source.startswith("https://")):
        return None, ["--source must be the https:// address of the zip (a GitHub release asset)"], []
    if source.rsplit("/", 1)[-1] != Path(zip_path).name:
        warn.append(f"--source ends in {source.rsplit('/', 1)[-1]!r}, the zip is {Path(zip_path).name!r}: "
                    "the release asset must be this exact file")
    if "/releases/download/" not in source:
        warn.append("--source is not a GitHub release asset (https://github.com/<you>/<repo>/releases/download/...)")
    d = Path(base) / "packages" / m["id"]
    ef = d / f"{m['version']}.json"
    if ef.exists():
        return None, [f"{ef.relative_to(base)} exists already: a new version needs a new version number "
                      "(change `version` in manifest.json, zip again)"], []
    e = {"manifest": m, "source": source, "sha256": hashlib.sha256(data).hexdigest()}
    tdata = b""
    if thumb is not None:
        tdata = Path(thumb).read_bytes()
        size = png_size(tdata)
        if size is None:
            return None, [f"{thumb}: not a PNG"], []
        if len(tdata) > MAX_THUMB_BYTES:
            return None, [f"{thumb}: {len(tdata) // 1024} kB (at most {MAX_THUMB_BYTES // 1024} kB)"], []
        if size != (512, 288):
            warn.append(f"the tile picture is {size[0]} x {size[1]}; the mirror shows it in a 16:9 tile "
                        "(512 x 288 fits exactly)")
        e["thumb"] = "thumb.png"
    if notes:
        e["notes"] = notes
    d.mkdir(parents=True, exist_ok=True)
    if tdata:
        (d / "thumb.png").write_bytes(tdata)
    ef.write_text(json.dumps(e, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _, ewhy, _ = check_entry(ef, data)
    if ewhy:                          # leave nothing half-made behind
        ef.unlink()
        if tdata:
            (d / "thumb.png").unlink()
    return ef, ewhy, warn


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
    root, rwhy = root_chain(base)
    if root is not None or rwhy:
        print(("OK      " if not rwhy else "REFUSED ") + f"root/ chain (newest: version "
              f"{root['signed']['version'] if root else '?'})")
        for w in rwhy:
            print("        - " + w)
        bad += bool(rwhy)
    idx_f, sig_f = base / "index.json", base / "index.json.sig"
    if idx_f.is_file():
        keys, revoked, _ = index_trust(base)
        idx, why = verify_index_bytes(idx_f.read_bytes(), sig_f.read_bytes() if sig_f.is_file() else b"",
                                      keys, revoked)
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
    a = sub.add_parser("check-entry"); a.add_argument("entry")  # noqa: E702
    a.add_argument("--zip", help="check against this local zip instead of downloading `source` (before the release exists)")
    a = sub.add_parser("make-zip", help="authors: zip a package folder as <id>-<version>.zip (no system files)")
    a.add_argument("folder")
    a.add_argument("--out", default=".", help="the folder to write the zip into (default: here)")
    a = sub.add_parser("make-entry", help="authors: write packages/<id>/<version>.json for a checked zip")
    a.add_argument("--zip", required=True)
    a.add_argument("--source", required=True, help="the zip's https address (your GitHub release asset)")
    a.add_argument("--thumb", help="a PNG tile picture, 512 x 288, at most 512 kB (copied to packages/<id>/thumb.png)")
    a.add_argument("--notes", default="", help="what is new in this version, for the reviewers")
    a = sub.add_parser("ci"); a.add_argument("entries", nargs="*")  # noqa: E702
    a = sub.add_parser("verify-index")
    a.add_argument("--index", default=str(REPO / "index.json"))
    a.add_argument("--keys", default="", help="a .pub file or folder (default: the newest root, else keys/)")
    a = sub.add_parser("build-index")
    a.add_argument("--key")
    a.add_argument("--raw-base", default=DEFAULT_RAW_BASE)
    a.add_argument("--serial", type=int)
    a.add_argument("--days", type=int, default=INDEX_DAYS, help="the list expires this many days after it is built")
    a = sub.add_parser("keygen")
    a.add_argument("--out", required=True)
    a.add_argument("--pub")
    a.add_argument("--comment", default="")
    a.add_argument("--passphrase", action="store_true", help="encrypt the PEM (asked twice, or MARKET_KEY_PASSPHRASE)")
    a = sub.add_parser("encrypt-key", help="write an encrypted copy of an unencrypted PEM (today's dev key)")
    a.add_argument("--key", required=True)
    a.add_argument("--out", required=True)
    a = sub.add_parser("make-root")
    a.add_argument("--version", type=int, required=True)
    a.add_argument("--root-pub", action="append", required=True, help="a root key's .pub (give each one)")
    a.add_argument("--threshold", type=int, default=2)
    a.add_argument("--index-pub", action="append", required=True, help="an index key's .pub")
    a.add_argument("--index-threshold", type=int, default=1)
    a.add_argument("--revoke", action="append", default=[], help="a key id to revoke (the old root's stay revoked)")
    a.add_argument("--days", type=int, default=ROOT_DAYS)
    a.add_argument("--out", required=True)
    a = sub.add_parser("sign-root")
    a.add_argument("root")
    a.add_argument("--key", required=True)
    a = sub.add_parser("verify-root")
    a.add_argument("--dir", default=str(REPO))
    a = sub.add_parser("pubkey"); a.add_argument("--key")  # noqa: E702
    ns = ap.parse_args(argv)
    if ns.cmd == "check-zip":
        m, why = check_zip_bytes(Path(ns.zip).read_bytes())
        print(("OK      " if not why else "REFUSED ") + ns.zip + (f"  ({m.get('id')} {m.get('version')})" if m else ""))
        for w in why:
            print("        - " + w)
        try:
            hint = junk_hint(zipfile.ZipFile(ns.zip).namelist())
        except zipfile.BadZipFile:
            hint = ""
        if hint:
            print("hint    " + hint)
        print("sha256  " + hashlib.sha256(Path(ns.zip).read_bytes()).hexdigest())
        return 1 if why else 0
    if ns.cmd == "check-entry":
        _, why, _ = check_entry(Path(ns.entry), Path(ns.zip).read_bytes() if ns.zip else None)
        print(("OK      " if not why else "REFUSED ") + ns.entry + ("  (against the local zip)" if ns.zip else ""))
        for w in why:
            print("        - " + w)
        if any("could not download" in w for w in why):
            print("hint    the release is not public yet? Check against your local zip: add --zip <your zip>")
        return 1 if why else 0
    if ns.cmd == "make-zip":
        out, why = make_zip(Path(ns.folder), Path(ns.out))
        print(("OK      " if not why else "REFUSED ") + (str(out) if out else ns.folder))
        for w in why:
            print("        - " + w)
        if out and not why:
            print("sha256  " + hashlib.sha256(out.read_bytes()).hexdigest())
        return 1 if why else 0
    if ns.cmd == "make-entry":
        ef, why, warn = make_entry(Path(ns.zip), ns.source, Path(ns.thumb) if ns.thumb else None, ns.notes)
        print(("OK      " if ef and not why else "REFUSED ") + (str(ef.relative_to(REPO)) if ef else ns.zip))
        for w in why:
            print("        - " + w)
        for w in warn:
            print("note    " + w)
        if ef and not why:
            print(f"next    commit packages/{ef.parent.name}/ on a branch of your fork and open a pull request")
        return 1 if why or not ef else 0
    if ns.cmd == "ci":
        return ci(REPO, ns.entries or None)
    if ns.cmd == "verify-index":
        f = Path(ns.index)
        sf = Path(str(f) + ".sig")
        if ns.keys:
            keys, revoked, kwhy = read_public_keys(Path(ns.keys)), set(), []
        else:
            keys, revoked, kwhy = index_trust(REPO)
        for w in kwhy:
            print("REFUSED " + w)
        idx, why = verify_index_bytes(f.read_bytes(), sf.read_bytes() if sf.is_file() else b"", keys, revoked)
        print(("OK      " if not why else "REFUSED ") + f"{f}  serial {idx.get('serial')}, "
              f"{len(idx.get('packages', []))} package(s)")
        for w in why:
            print("        - " + w)
        return 1 if why else 0
    if ns.cmd == "build-index":
        priv = load_private_key(ns.key)
        keys, revoked, kwhy = index_trust(REPO)
        if kwhy:
            print("REFUSED: the root chain does not verify: " + "; ".join(kwhy))
            return 1
        if key_id(public_raw(priv)) not in keys:
            print("REFUSED: this private key is not an index key of the newest root (or, before a root, not in keys/)")
            return 1
        data, sig, problems = build_index(priv, REPO, ns.raw_base, serial=ns.serial, days=ns.days)
        for p in problems:
            print("SKIPPED " + p)
        (REPO / "index.json").write_bytes(data)
        (REPO / "index.json.sig").write_bytes(sig)
        print(f"wrote index.json ({len(json.loads(data)['packages'])} package(s)) and index.json.sig")
        return 0
    if ns.cmd == "keygen":
        pw = _passphrase(f"passphrase for {ns.out}: ", twice=True) if ns.passphrase else None
        print(keygen(Path(ns.out), Path(ns.pub) if ns.pub else None, ns.comment, pw), end="")
        return 0
    if ns.cmd == "encrypt-key":
        encrypt_key(Path(ns.key), Path(ns.out), _passphrase(f"new passphrase for {ns.out}: ", twice=True))
        print(f"wrote {ns.out} (encrypted); the unencrypted {ns.key} is untouched -- move it to safe storage or shred it")
        return 0
    if ns.cmd == "make-root":
        rp, ip = {}, {}
        for f in ns.root_pub:
            rp.update(read_public_keys(Path(f)))
        for f in ns.index_pub:
            ip.update(read_public_keys(Path(f)))
        revoked = set(ns.revoke)
        prev = Path(ns.out).parent / f"{ns.version - 1}.json"
        if ns.version > 1 and prev.is_file():
            revoked |= set(json.loads(prev.read_text(encoding="utf-8"))["signed"].get("revoked", []))
        root = make_root(ns.version, rp, ip, ns.threshold, ns.index_threshold, revoked, ns.days)
        Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.out).write_bytes(root_bytes(root))
        print(f"wrote {ns.out} (unsigned): root keys {sorted(rp)} ({ns.threshold} needed), index keys {sorted(ip)}, "
              f"revoked {sorted(revoked)}")
        return 0
    if ns.cmd == "sign-root":
        f = Path(ns.root)
        root = sign_root(json.loads(f.read_text(encoding="utf-8")), load_private_key(ns.key))
        f.write_bytes(root_bytes(root))
        v = root["signed"]["version"]
        own = len(good_sigs(root, role_keys(root["signed"], "root")))
        line = f"signed {f}: {own} of {root['signed']['roles']['root']['threshold']} own root signatures"
        prev = f.parent / f"{v - 1}.json"
        if v > 1 and prev.is_file():
            old = json.loads(prev.read_text(encoding="utf-8"))
            n_old = len(good_sigs(root, role_keys(old["signed"], "root")))
            line += f", {n_old} of {old['signed']['roles']['root']['threshold']} from root {v - 1}"
        print(line)
        return 0
    if ns.cmd == "verify-root":
        root, why = root_chain(Path(ns.dir))
        if root is None and not why:
            print("no root/ yet: keys/*.pub is the trust")
            return 0
        exp = parse_time(root["signed"]["expires"]) if root else None
        if root and exp is not None and exp <= _dt.datetime.now(_dt.timezone.utc).timestamp():
            why.append(f"root {root['signed']['version']} expired on {root['signed']['expires']}")
        print(("OK      " if not why else "REFUSED ") + "root chain" +
              (f": version {root['signed']['version']}, expires {root['signed']['expires']}" if root else ""))
        for w in why:
            print("        - " + w)
        return 1 if why else 0
    if ns.cmd == "pubkey":
        raw = public_raw(load_private_key(ns.key))
        print(base64.b64encode(raw).decode() + f"  (key id {key_id(raw)})")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
