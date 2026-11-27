"""Build settings.yml for a native local SearXNG next to MetaSearch Lite (docs/36 §21).

Reads deploy/searxng/settings.yml (the Docker profile's settings) and writes
<dir>/settings.yml with:

- the server bound to 127.0.0.1:<port>, limiter off, a random secret kept stable
  across rebuilds;
- engines that fail from this machine disabled (``--disable``) and engines whose
  setup errors at boot made inactive (``--inactive``);
- a stand-in ``pwd`` module in the venv: SearXNG imports the Unix-only module in
  searx/valkeydb.py, only to name the OS user in a valkey error message (no valkey
  here).

No owner contact data is written: engines that require ``general.contact_url``
(wikipedia, wikidata, openstreetmap) are made inactive instead — MetaSearch has its
own Wikipedia adapter.

Usage: python searxng_local_settings.py --dir D:\\AppAI\\searxng-local --port 8888
       [--disable google qwant brave] [--inactive wikipedia wikidata ahmia torch]
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "deploy" / "searxng" / "settings.yml"

UNIX_USER_MODULE_STUB = '''"""Windows stand-in for the Unix-only ``pwd`` module (local SearXNG).

SearXNG imports ``pwd`` in searx/valkeydb.py only to name the OS user in a valkey
connection error message; this instance runs without valkey (limiter off).
"""

import getpass
from collections import namedtuple

struct_passwd = namedtuple(
    "struct_passwd", "pw_name pw_passwd pw_uid pw_gid pw_gecos pw_dir pw_shell"
)


def getpwuid(uid):
    return struct_passwd(getpass.getuser(), "x", 0, 0, "", "", "")
'''


def _existing_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("secret_key:"):
            value = line.split(":", 1)[1].strip().strip('"')
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
    return None


def build(text: str, *, secret: str, port: int, disable: list[str], inactive: list[str]) -> str:
    text = text.replace('"${SEARXNG_SECRET}"', f'"{secret}"')
    text = text.replace('"${SEARXNG_BASE_URL}"', f'"http://127.0.0.1:{port}/"')
    text, n = re.subn(
        r"(  public_instance: false\n)",
        rf'\g<1>  bind_address: "127.0.0.1"\n  port: {port}\n',
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit("settings source changed: server.public_instance not found")
    extra: list[str] = []
    for name in disable + inactive:
        pattern = re.compile(rf"(  - name: {re.escape(name)}\n    disabled: )false")
        text, n = pattern.subn(r"\g<1>true", text, count=1)
        if n == 0:
            extra.append(f"  - name: {name}\n    disabled: true\n")
    for name in inactive:
        block = f"  - name: {name}\n    disabled: true\n"
        if block in text:
            text = text.replace(block, block + "    inactive: true\n", 1)
        else:
            extra = [b for b in extra if not b.startswith(f"  - name: {name}\n")]
            extra.append(f"  - name: {name}\n    inactive: true\n")
    if extra:
        text = text.rstrip("\n") + "\n  # local-only (searxng_local_settings.py)\n" + "".join(extra)
    return text


_ARCHIVE_URL = "https://github.com/searxng/searxng/archive/{ref}.tar.gz"
_KEEP_FILES = ("requirements.txt", "requirements-server.txt")


def fetch(ref: str, dest: Path) -> int:
    """Download the pinned upstream archive; extract only searx/ + requirement files.

    Not ``git checkout``: upstream ships Linux config templates named "...conf:socket",
    which Git for Windows refuses even outside a sparse cone (core.protectNTFS — a
    protection we keep on). Extraction uses tarfile's ``data`` filter (no absolute
    paths, links or device files) and an explicit allow-list of members.
    """
    import shutil
    import tarfile
    import tempfile
    import urllib.request

    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise SystemExit("--fetch needs a full 40-hex commit id")
    url = _ARCHIVE_URL.format(ref=ref)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "searxng.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as resp, archive.open("wb") as fh:  # nosec B310 - fixed https URL
            shutil.copyfileobj(resp, fh)
        prefix = f"searxng-{ref}/"
        staging = Path(tmp) / "x"
        with tarfile.open(archive, "r:gz") as tar:
            members = [
                m
                for m in tar.getmembers()
                if m.name.startswith(prefix)
                and (m.name.startswith(prefix + "searx/") or m.name[len(prefix) :] in _KEEP_FILES)
                and ":" not in m.name
            ]
            tar.extractall(staging, members=members, filter="data")
        if dest.exists():
            # Read-only files (e.g. .git packs of an older clone) block rmtree on Windows.
            def _clear_readonly(func, path, _exc):
                import os
                import stat

                os.chmod(path, stat.S_IWRITE)
                func(path)

            shutil.rmtree(dest, onexc=_clear_readonly)
        shutil.move(str(staging / f"searxng-{ref}"), str(dest))
    (dest / "REVISION").write_text(ref + "\n", encoding="utf-8")
    print(f"fetched searxng {ref} -> {dest}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--fetch", metavar="COMMIT", help="download upstream source into <dir>/src")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--disable", nargs="*", default=["google", "qwant", "brave"])
    ap.add_argument(
        "--inactive",
        nargs="*",
        default=["wikipedia", "wikidata", "openstreetmap", "ahmia", "torch"],
    )
    args = ap.parse_args(argv)
    if args.fetch:
        return fetch(args.fetch, args.dir / "src")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port must be 1024..65535")
    target = args.dir / "settings.yml"
    secret = _existing_secret(target) or secrets.token_hex(32)
    text = build(
        SOURCE.read_text(encoding="utf-8"),
        secret=secret,
        port=args.port,
        disable=list(args.disable),
        inactive=list(args.inactive),
    )
    args.dir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    site = args.dir / "venv" / "Lib" / "site-packages"
    if site.is_dir():
        (site / "pwd.py").write_text(UNIX_USER_MODULE_STUB, encoding="utf-8")
    print(f"settings: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
