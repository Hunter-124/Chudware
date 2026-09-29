"""Resumable, checksum-verified fetcher for the ViewNet geometry-label sources.

Everything lands under ``$CHUDWARE_VIEWNET_DATA`` (default ``~/.cache/chudware/viewnet``),
never in git:

    downloads/<source>/<archive>          whole archives (``--full`` / ``--archive`` / small sources)
    raw/<source>/<archive member path>    extracted parts + their label companions
    raw/<source>/fetch_state.json         pinned archive identity, verified members, part index

``--limit N`` takes a deterministic subset (members ordered by sha1 of the part stem) WITHOUT
downloading the whole archive when the server honours HTTP Range: the zip central directory is
read remotely and only the selected members are pulled. Every extracted member is verified
against the CRC-32 recorded in the archive's central directory; whole archives are verified
against the publisher's SHA-256 when one is published, otherwise the first download pins its
SHA-256 in ``fetch_state.json`` and every later run must reproduce it. A run that dies midway
resumes: whole downloads continue from the ``.part`` byte count, extracted members that already
match their CRC are skipped, and the archive identity (size + ETag/Last-Modified) is pinned so a
changed upstream archive is refused instead of silently mixing versions.

A git-hosted source (``Source.git_repo``) is pinned to one commit: its file list comes from one
GitHub trees-API call (cached under ``raw/<source>/``), the selected files are pulled one by one
from raw.githubusercontent.com at that commit, and every file is verified against its git blob
SHA-1 from the tree — no clone, no whole-repository download.

Licences are part of the source table (see DATASETS.md): a non-commercial source needs
``--allow-noncommercial`` and a source whose data licence is unstated needs
``--allow-unverified-license`` — Chudware is a commercial product.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

DATA_ENV = "CHUDWARE_VIEWNET_DATA"
STATE_FILE = "fetch_state.json"
STATE_SCHEMA = "chudware.viewnet.fetch/1"
USER_AGENT = "chudware-viewnet-fetch/1"
_CHUNK = 1 << 20
_RETRIES = 4
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
_GIT_WORKERS = 16
MFCAD_COMMIT = "ef6d58a40164d5192666821ce98d0cc90e379fac"


def data_root() -> Path:
    """Root of the local ViewNet cache ($CHUDWARE_VIEWNET_DATA, default ~/.cache/chudware/viewnet)."""
    env = os.environ.get(DATA_ENV)
    return Path(env).expanduser() if env else Path.home() / ".cache" / "chudware" / "viewnet"


class FetchError(RuntimeError):
    """A fetch that cannot complete (bad checksum, changed upstream, missing member...)."""


class FetchBlocked(FetchError):
    """The server needs a human step (browser challenge); the message says what to do."""


class LicenseRefused(FetchError):
    """The source's licence was not accepted on the command line."""


@dataclass(frozen=True)
class Source:
    name: str
    title: str
    license: str
    license_url: str
    commercial_use: str                    # "yes" | "no" | "unverified"
    landing_url: str
    archive_url: str = ""
    archive_name: str = ""
    archive_bytes: Optional[int] = None    # exact size when known (pinned from a probe)
    archive_sha256: Optional[str] = None   # publisher's SHA-256 when published inline
    sha256_url: Optional[str] = None       # publisher's SHA-256 file
    part_suffixes: tuple = ()
    part_dir: Optional[str] = None         # required parent-directory name of a part member
    companions: tuple = ()                 # templates relative to the part's directory, "{stem}"
    shared_names: tuple = ()               # basenames extracted once (class tables, licences)
    whole_archive: bool = False            # extract everything; no part selection
    git_repo: Optional[str] = None         # GitHub "owner/name": files fetched one by one ...
    git_commit: Optional[str] = None       # ... at this pinned commit, verified by blob SHA-1
    notes: str = ""


SOURCES: dict[str, Source] = {
    s.name: s for s in (
        Source(
            name="mfcadpp",
            title="MFCAD++ (Colligan et al. 2022, Hierarchical CADNet)",
            license="CC-BY-4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/legalcode",
            commercial_use="yes",
            landing_url="https://doi.org/10.17034/d1fec5a0-8c10-4630-b02e-b92dc81df823",
            archive_url="https://pure.qub.ac.uk/files/278385243/MFCAD_dataset.zip",
            archive_name="MFCAD_dataset.zip",
            part_suffixes=(".step", ".stp"),
            shared_names=("train.txt", "val.txt", "test.txt", "feature_labels.txt"),
            notes="59,665 STEP parts; per-face class index stored as the ADVANCED_FACE name. "
                  "The QUB file server sits behind a Cloudflare browser challenge: download "
                  "MFCAD_dataset.zip in a browser once and pass --archive PATH.",
        ),
        Source(
            name="mfcad",
            title="MFCAD (Cao et al. 2020, graph representation of CAD models for MFR)",
            license="MIT",
            license_url=f"https://github.com/hducg/MFCAD/blob/{MFCAD_COMMIT}/LICENSE",
            commercial_use="yes",
            landing_url="https://github.com/hducg/MFCAD",
            git_repo="hducg/MFCAD",
            git_commit=MFCAD_COMMIT,
            part_suffixes=(".step",),
            part_dir="step",
            companions=("{stem}.face_truth",),
            shared_names=("LICENSE",),
            notes="15,488 STEP parts, 16 classes; every ADVANCED_FACE is named with a face id "
                  "and <stem>.face_truth (a pickled list) holds the class index of each id.",
        ),
        Source(
            name="mfinstseg",
            title="MFInstSeg (Wu et al. 2024, AAGNet)",
            license="unstated (AAGNet code is MIT; the data carries no licence)",
            license_url="https://github.com/whjdark/AAGNet",
            commercial_use="unverified",
            landing_url="https://github.com/whjdark/AAGNet#dataset",
            archive_url="https://drive.usercontent.google.com/download"
                        "?id=1T2sHlL-4qlsXTxu3AMBB4h-N1I5VqNky&export=download&confirm=t",
            archive_name="data2.zip",
            archive_bytes=1801353969,
            part_suffixes=(".step",),
            part_dir="steps",
            companions=("../labels/{stem}.json",),
            notes="62,495 STEP parts with per-face semantic, instance and bottom-face labels.",
        ),
        Source(
            name="fusion360_seg",
            title="Fusion 360 Gallery Segmentation, extended STEP (s2.0.1)",
            license="Fusion 360 Gallery Dataset License (non-commercial research only)",
            license_url="https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/LICENSE.md",
            commercial_use="no",
            landing_url="https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/docs/segmentation.md",
            archive_url="https://fusion-360-gallery-dataset.s3.us-west-2.amazonaws.com/"
                        "segmentation/s2.0.1/s2.0.1_extended_step.zip",
            archive_name="s2.0.1_extended_step.zip",
            archive_bytes=506333119,
            part_suffixes=(".stp",),
            part_dir="step",
            companions=("../seg/{stem}.seg", "../../timeline_info/{stem}.json"),
            shared_names=("segment_names.json", "train_test.json",
                          "Fusion 360 Gallery Dataset Public License.docx"),
            notes="42,912 STEP parts; one modelling-operation label per face.",
        ),
        Source(
            name="nist_bestview",
            title="NIST benchmark for best view selection of 3D objects (Dutagaci et al. 2010)",
            license="NIST open data licence (https://www.nist.gov/open/license)",
            license_url="https://www.nist.gov/open/license",
            commercial_use="yes",
            landing_url="https://doi.org/10.18434/M32209",
            archive_url="https://data.nist.gov/od/ds/mds2-2209/3D_BestView.zip",
            archive_name="3D_BestView.zip",
            sha256_url="https://data.nist.gov/od/ds/mds2-2209/3D_BestView.zip.sha256",
            whole_archive=True,
            notes="68 meshes with the best views chosen by 26 people; a human view-preference "
                  "sanity set (no face labels).",
        ),
    )
}


# ------------------------------------------------------------------------------------ http

def _request(url: str, headers: Optional[dict] = None, method: str = "GET"):
    """urlopen with retries on transient failures; a Cloudflare challenge raises FetchBlocked."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    delay = 1.0
    for attempt in range(_RETRIES):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=hdrs, method=method),
                                          timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 403 and (e.headers.get("cf-mitigated") or "").lower() == "challenge":
                raise FetchBlocked(f"{url} answers with a Cloudflare browser challenge") from e
            if e.code == 416:
                return e
            if e.code < 500 or attempt == _RETRIES - 1:
                raise FetchError(f"GET {url}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == _RETRIES - 1:
                raise FetchError(f"GET {url}: {e}") from e
        time.sleep(delay)
        delay *= 2
    raise FetchError(f"GET {url}: retries exhausted")


@dataclass
class RemoteIdentity:
    url: str
    bytes: int
    etag: Optional[str]
    last_modified: Optional[str]
    ranges: bool

    def pin(self) -> dict:
        return {"url": self.url, "bytes": self.bytes, "etag": self.etag,
                "last_modified": self.last_modified}


def probe(url: str) -> RemoteIdentity:
    """Size, validators and Range support of a remote archive (one 1-byte ranged GET)."""
    resp = _request(url, {"Range": "bytes=0-0"})
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        cr = resp.headers.get("Content-Range") or ""
        if _is_html(resp):
            raise FetchError(f"{url} returned an HTML page instead of the archive")
        if status == 206 and "/" in cr:
            total = int(cr.rsplit("/", 1)[1])
            ranges = True
        else:
            total = int(resp.headers.get("Content-Length") or 0)
            ranges = False
        return RemoteIdentity(url, total, resp.headers.get("ETag"),
                              resp.headers.get("Last-Modified"), ranges)


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only view of a remote file through HTTP Range requests.

    `If-Range` carries the pinned validator, so an archive replaced upstream between two reads
    answers 200 instead of 206 and the read fails rather than splicing two archives."""

    def __init__(self, ident: RemoteIdentity):
        super().__init__()
        self._id = ident
        self._pos = 0
        self.bytes_fetched = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self._id.bytes + offset
        self._pos = max(0, self._pos)
        return self._pos

    def readinto(self, b) -> int:
        n = len(b)
        if n == 0 or self._pos >= self._id.bytes:
            return 0
        end = min(self._id.bytes, self._pos + n) - 1
        hdrs = {"Range": f"bytes={self._pos}-{end}"}
        validator = self._id.etag or self._id.last_modified
        if validator:
            hdrs["If-Range"] = validator
        resp = _request(self._id.url, hdrs)
        with resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 206:
                raise FetchError(f"{self._id.url} ignored a ranged read (HTTP {status}); "
                                 "the archive changed upstream — rerun with --refresh")
            data = resp.read()
        got = len(data)
        b[:got] = data
        self._pos += got
        self.bytes_fetched += got
        return got


def download(url: str, dest: Path, expected_bytes: Optional[int] = None,
             log: Callable[[str], None] = print) -> Path:
    """Download `url` to `dest`, resuming a previous `.part` via HTTP Range."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (expected_bytes is None or dest.stat().st_size == expected_bytes):
        return dest
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    resp = _request(url, {"Range": f"bytes={have}-"} if have else None)
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if status == 416:
            total = have
        else:
            if _is_html(resp):
                raise FetchError(f"{url} returned an HTML page instead of the archive")
            if have and status != 206:
                have = 0  # server ignored the range: start over
            cr = resp.headers.get("Content-Range") or ""
            total = int(cr.rsplit("/", 1)[1]) if "/" in cr else have + int(
                resp.headers.get("Content-Length") or 0)
            mode = "ab" if have else "wb"
            last_report = have
            with open(part, mode) as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    have += len(chunk)
                    if have - last_report >= 64 * _CHUNK:
                        log(f"  {dest.name}: {have / 1e6:.0f} / {total / 1e6:.0f} MB")
                        last_report = have
    size = part.stat().st_size if part.exists() else 0
    if total and size != total:
        raise FetchError(f"{dest.name}: downloaded {size} of {total} bytes; rerun to resume")
    if expected_bytes is not None and size != expected_bytes:
        raise FetchError(f"{dest.name}: {size} bytes, expected {expected_bytes}")
    os.replace(part, dest)
    return dest


def _is_html(resp) -> bool:
    return (resp.headers.get("Content-Type") or "").lower().startswith("text/html")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def crc32_file(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def published_sha256(src: Source) -> Optional[str]:
    if src.archive_sha256:
        return src.archive_sha256.lower()
    if not src.sha256_url:
        return None
    with _request(src.sha256_url) as resp:
        text = resp.read().decode("utf-8", "replace")
    for token in text.split():
        t = token.strip().lower()
        if len(t) == 64 and all(c in "0123456789abcdef" for c in t):
            return t
    raise FetchError(f"{src.sha256_url} does not contain a SHA-256 digest")


# ----------------------------------------------------------------------------------- state

def source_dir(name: str, root: Optional[Path] = None) -> Path:
    return (root or data_root()) / "raw" / name


def load_state(name: str, root: Optional[Path] = None) -> dict:
    p = source_dir(name, root) / STATE_FILE
    if p.exists():
        return json.loads(p.read_text())
    return {"schema": STATE_SCHEMA, "source": name, "archive": None, "members": {}, "parts": {}}


def save_state(name: str, state: dict, root: Optional[Path] = None) -> None:
    d = source_dir(name, root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, d / STATE_FILE)


@dataclass(frozen=True)
class LocalPart:
    """One fetched part: the STEP file, its label companions and the source's shared files."""
    source: str
    original_id: str
    step: Path
    companions: tuple = ()
    shared: dict = field(default_factory=dict)


def local_parts(name: str, root: Optional[Path] = None) -> list[LocalPart]:
    """Parts already fetched for source `name`, in selection order (sha1 of the stem)."""
    state = load_state(name, root)
    base = source_dir(name, root)
    shared = {PurePosixPath(m).name: base / m for m in state.get("shared", [])}
    out = []
    for member, rec in state.get("parts", {}).items():
        step = base / member
        comps = tuple(base / c for c in rec.get("companions", []))
        if step.exists() and all(c.exists() for c in comps):
            out.append(LocalPart(name, PurePosixPath(member).stem, step, comps, dict(shared)))
    out.sort(key=lambda p: (_order_key(p.original_id), p.original_id))
    return out


# ----------------------------------------------------------------------------- selection

def _order_key(stem: str) -> str:
    return hashlib.sha1(stem.encode("utf-8")).hexdigest()


def select_parts(src: Source, names: list[str], limit: Optional[int]) -> list[tuple[str, list[str]]]:
    """(part member, companion members) for the first `limit` parts in sha1(stem) order.

    A part whose companions are missing from the archive is skipped — it has no labels."""
    present = set(names)
    candidates = []
    for n in names:
        p = PurePosixPath(n)
        if n.endswith("/") or p.suffix.lower() not in src.part_suffixes:
            continue
        if src.part_dir is not None and p.parent.name != src.part_dir:
            continue
        candidates.append(n)
    candidates.sort(key=lambda n: (_order_key(PurePosixPath(n).stem), n))
    out = []
    for n in candidates:
        p = PurePosixPath(n)
        comps = [posixpath.normpath(posixpath.join(str(p.parent), t.format(stem=p.stem)))
                 for t in src.companions]
        if all(c in present for c in comps):
            out.append((n, comps))
            if limit is not None and len(out) >= limit:
                break
    return out


def _safe_dest(base: Path, member: str) -> Path:
    dest = (base / PurePosixPath(member)).resolve()
    if base.resolve() not in dest.parents:
        raise FetchError(f"archive member escapes the cache directory: {member!r}")
    return dest


def extract_member(zf: zipfile.ZipFile, member: str, base: Path) -> bool:
    """Extract one member atomically, verified against its central-directory CRC-32.

    Returns False when an identical (size + CRC) file is already in place."""
    info = zf.getinfo(member)
    dest = _safe_dest(base, member)
    if dest.exists() and dest.stat().st_size == info.file_size and crc32_file(dest) == info.CRC:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    crc = 0
    with zf.open(info) as src, open(tmp, "wb") as out:
        for chunk in iter(lambda: src.read(_CHUNK), b""):
            crc = zlib.crc32(chunk, crc)
            out.write(chunk)
    if (crc & 0xFFFFFFFF) != info.CRC:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"{member}: CRC-32 mismatch after extraction")
    os.replace(tmp, dest)
    return True


# ------------------------------------------------------------------------- git sources

def git_blob_sha1(data: bytes) -> str:
    """The git object id of a file's content (what the tree records for it)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _get(url: str, headers: Optional[dict] = None) -> bytes:
    with _request(url, headers) as resp:
        if _is_html(resp):
            raise FetchError(f"{url} returned an HTML page instead of a file")
        return resp.read()


def git_tree(src: Source, root: Optional[Path] = None) -> dict:
    """{path: blob SHA-1} of every file at the pinned commit (one API call, then cached)."""
    cache = source_dir(src.name, root) / f"git_tree.{src.git_commit}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = f"{GITHUB_API}/repos/{src.git_repo}/git/trees/{src.git_commit}?recursive=1"
    raw = json.loads(_get(url, {"Accept": "application/vnd.github+json"}))
    if raw.get("truncated"):
        raise FetchError(f"{url}: tree listing truncated by the API")
    tree = {e["path"]: e["sha"] for e in raw["tree"] if e["type"] == "blob"}
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    tmp.write_text(json.dumps(tree, sort_keys=True))
    os.replace(tmp, cache)
    return tree


def fetch_git_member(src: Source, member: str, blob: str, base: Path) -> int:
    """Download one file at the pinned commit, verified against its blob SHA-1; returns the
    bytes downloaded (0 when a verified copy is already in place)."""
    dest = _safe_dest(base, member)
    if dest.exists() and git_blob_sha1(dest.read_bytes()) == blob:
        return 0
    data = _get(f"{GITHUB_RAW}/{src.git_repo}/{src.git_commit}/{urllib.parse.quote(member)}")
    got = git_blob_sha1(data)
    if got != blob:
        raise FetchError(f"{member}: git blob SHA-1 {got} != {blob} recorded at "
                         f"{src.git_repo}@{src.git_commit}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return len(data)


def _fetch_git(src: Source, state: dict, base: Path, limit: Optional[int],
               root: Optional[Path], log: Callable[[str], None]) -> dict:
    pin = f"git+https://github.com/{src.git_repo}@{src.git_commit}"
    held = (state.get("archive") or {}).get("url")
    if held not in (None, pin):
        raise FetchError(f"{base} holds files from {held}, not {pin}; rerun with --refresh")
    state["archive"] = {"url": pin, "commit": src.git_commit}
    tree = git_tree(src, root)
    members: dict = state.setdefault("members", {})
    parts: dict = state.setdefault("parts", {})
    shared = sorted(n for n in tree if PurePosixPath(n).name in src.shared_names)
    for n in shared:
        fetch_git_member(src, n, tree[n], base)
        members[n] = tree[n]
    state["shared"] = shared
    selected = select_parts(src, list(tree), limit)
    extracted = skipped = downloaded = 0

    def pull(part: str, comps: list) -> list:
        return [(m, fetch_git_member(src, m, tree[m], base)) for m in (part, *comps)]

    pool = ThreadPoolExecutor(_GIT_WORKERS)
    try:
        pending = {pool.submit(pull, p, c): (p, c) for p, c in selected}
        done_parts = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                part, comps = pending.pop(fut)
                for m, nbytes in fut.result():
                    members[m] = tree[m]
                    downloaded += nbytes
                    extracted += nbytes > 0
                    skipped += nbytes == 0
                parts[part] = {"companions": comps}
                done_parts += 1
                if done_parts % 200 == 0:
                    save_state(src.name, state, root)
                    log(f"  {src.name}: {done_parts}/{len(selected)} parts")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        save_state(src.name, state, root)
    return {"source": src.name, "parts_selected": len(selected), "parts_total_local": len(parts),
            "members_extracted": extracted, "members_already_verified": skipped,
            "remote_bytes": downloaded, "commit": src.git_commit, "dir": str(base)}


# ---------------------------------------------------------------------------------- fetch

def check_license(src: Source, allow_noncommercial: bool, allow_unverified: bool) -> None:
    if src.commercial_use == "no" and not allow_noncommercial:
        raise LicenseRefused(
            f"{src.name}: {src.license} forbids commercial use; Chudware is commercial. "
            f"Pass --allow-noncommercial only for non-commercial research ({src.license_url}).")
    if src.commercial_use == "unverified" and not allow_unverified:
        raise LicenseRefused(
            f"{src.name}: data licence is {src.license}; pass --allow-unverified-license "
            f"only after the rights question is settled ({src.license_url}).")


def _verify_whole(src: Source, path: Path, state: dict, log: Callable[[str], None]) -> str:
    digest = sha256_file(path)
    want = published_sha256(src)
    pinned = (state.get("archive") or {}).get("sha256")
    if want and digest != want:
        raise FetchError(f"{path.name}: SHA-256 {digest} != published {want}")
    if pinned and digest != pinned:
        raise FetchError(f"{path.name}: SHA-256 {digest} != pinned {pinned} from an earlier "
                         "fetch; rerun with --refresh to accept a new upstream archive")
    if not want and not pinned:
        log(f"  {src.name}: no published checksum; pinned SHA-256 {digest}")
    return digest


def fetch(name: str, *, limit: Optional[int] = None, archive: Optional[Path] = None,
          full: bool = False, refresh: bool = False, allow_noncommercial: bool = False,
          allow_unverified: bool = False, root: Optional[Path] = None,
          log: Callable[[str], None] = print) -> dict:
    """Fetch (a `limit`-part subset of) source `name` into the cache; returns a summary."""
    if name not in SOURCES:
        raise FetchError(f"unknown source {name!r} (known: {', '.join(SOURCES)})")
    src = SOURCES[name]
    check_license(src, allow_noncommercial, allow_unverified)
    base = source_dir(name, root)
    base.mkdir(parents=True, exist_ok=True)
    state = load_state(name, root)
    if refresh:
        state.update(archive=None, members={})
    state.update(license=src.license, license_url=src.license_url,
                 commercial_use=src.commercial_use, url=src.landing_url)
    pinned = state.get("archive") or {}
    if src.git_repo:
        if archive is not None or full:
            raise FetchError(f"{name} is fetched file by file from {src.landing_url} at a "
                             "pinned commit; --archive/--full do not apply")
        return _fetch_git(src, state, base, limit, root, log)

    remote_file: Optional[HttpRangeFile] = None
    if archive is None and pinned.get("url") and Path(pinned["url"]).is_file():
        # A source fetched from a browser-downloaded archive keeps using that file.
        archive = Path(pinned["url"])
    if archive is not None:
        archive = Path(archive).expanduser()
        if not archive.is_file():
            raise FetchError(f"--archive {archive} is not a file")
        if src.archive_bytes is not None and archive.stat().st_size != src.archive_bytes:
            raise FetchError(f"{archive}: {archive.stat().st_size} bytes, expected "
                             f"{src.archive_bytes}")
        digest = _verify_whole(src, archive, state, log)
        state["archive"] = {"url": str(archive), "bytes": archive.stat().st_size,
                            "sha256": digest}
        zf = zipfile.ZipFile(archive)
    else:
        try:
            ident = probe(src.archive_url)
        except FetchBlocked as e:
            raise FetchBlocked(
                f"{e}. Download {src.archive_name} in a browser from {src.landing_url} and "
                f"rerun with --archive PATH.") from e
        if src.archive_bytes is not None and ident.bytes != src.archive_bytes:
            raise FetchError(f"{src.archive_url}: {ident.bytes} bytes, expected "
                             f"{src.archive_bytes}")
        if pinned.get("bytes") not in (None, ident.bytes) or \
                (pinned.get("etag") and ident.etag and pinned["etag"] != ident.etag):
            raise FetchError(f"{src.archive_url} changed since the pinned fetch "
                             f"({pinned}); rerun with --refresh")
        if full or src.whole_archive or not ident.ranges:
            local = download(src.archive_url, (root or data_root()) / "downloads" / name /
                             src.archive_name, ident.bytes or None, log)
            digest = _verify_whole(src, local, state, log)
            state["archive"] = dict(ident.pin(), sha256=digest)
            zf = zipfile.ZipFile(local)
        else:
            state["archive"] = dict(ident.pin(), sha256=pinned.get("sha256"))
            remote_file = HttpRangeFile(ident)
            zf = zipfile.ZipFile(io.BufferedReader(remote_file, buffer_size=_CHUNK))

    extracted = skipped = 0
    with zf:
        names = zf.namelist()
        members: dict = state.setdefault("members", {})
        parts: dict = state.setdefault("parts", {})

        def pull(member: str) -> None:
            nonlocal extracted, skipped
            if extract_member(zf, member, base):
                extracted += 1
            else:
                skipped += 1
            members[member] = zf.getinfo(member).CRC

        if src.whole_archive:
            for n in names:
                if not n.endswith("/"):
                    pull(n)
            selected = []
        else:
            shared = [n for n in names if PurePosixPath(n).name in src.shared_names]
            for n in shared:
                pull(n)
            state["shared"] = sorted(shared)
            selected = select_parts(src, names, limit)
            for i, (part, comps) in enumerate(selected, 1):
                for m in (part, *comps):
                    pull(m)
                parts[part] = {"companions": comps}
                if i % 200 == 0:
                    save_state(name, state, root)
                    log(f"  {name}: {i}/{len(selected)} parts")
    save_state(name, state, root)
    return {"source": name, "parts_selected": len(selected), "parts_total_local": len(parts),
            "members_extracted": extracted, "members_already_verified": skipped,
            "remote_bytes": remote_file.bytes_fetched if remote_file else None,
            "dir": str(base)}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", help=f"one of: {', '.join(SOURCES)}")
    ap.add_argument("--list", action="store_true", help="list sources with licence and exit")
    ap.add_argument("--limit", type=int, default=None,
                    help="fetch only the first N parts (sha1(stem) order); default all")
    ap.add_argument("--archive", type=Path, default=None,
                    help="use an already-downloaded zip archive (e.g. after a browser "
                         "challenge); zip sources only")
    ap.add_argument("--full", action="store_true",
                    help="download the whole zip instead of ranged member reads; zip sources "
                         "only (git sources always pull verified files one by one)")
    ap.add_argument("--refresh", action="store_true",
                    help="accept a changed upstream archive (forget the pinned identity)")
    ap.add_argument("--allow-noncommercial", action="store_true",
                    help="permit a non-commercial-only source (research use, never shipped)")
    ap.add_argument("--allow-unverified-license", action="store_true",
                    help="permit a source whose data licence is unstated")
    args = ap.parse_args(argv)

    if args.list or not args.source:
        for s in SOURCES.values():
            print(f"{s.name:15s} commercial_use={s.commercial_use:10s} {s.license}")
            print(f"{'':15s} {s.landing_url}")
        return 0 if args.list else 2
    try:
        summary = fetch(args.source, limit=args.limit, archive=args.archive, full=args.full,
                        refresh=args.refresh, allow_noncommercial=args.allow_noncommercial,
                        allow_unverified=args.allow_unverified_license)
    except FetchError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
