#!/usr/bin/env python3
"""Turn a game release's zip into the files the Cafe Kinship app installs.

Run by .github/workflows/publish-game-files.yml when a release is published.
For the release's zip it:

  1. unzips the game and fingerprints (SHA-256) every file,
  2. uploads only the files the app doesn't already have to the permanent
     `game-files` release (named by fingerprint, so an unchanged file is
     never uploaded twice),
  3. writes manifest.json, the list of every file the game needs. The app
     compares a player's files against it and downloads only what changed.
  4. deletes files that neither this version nor the one before it uses,
     so storage doesn't grow forever.

Files over 1.5 GB are split into parts, because GitHub won't store a single
file over 2 GB. The app joins them back together.

Standard library only, plus the `gh` command that every GitHub runner has.
Local test without GitHub: DRY_RUN=1 ZIP=path/to/game.zip TAG=v0.1 python3 tools/publish_files.py
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone

REPO = os.environ.get("REPO", "Prismultra/cafe-kinship-game")
TAG = os.environ.get("TAG", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FILES_TAG = "game-files"
PART_BYTES = int(os.environ.get("PART_BYTES", str(1536 * 1024 * 1024)))  # 1.5 GiB
WORK = os.path.abspath(os.environ.get("WORK", "ck-publish-work"))
# Not needed to run the game, and often huge.
SKIP_SUFFIXES = (".pdb",)


def log(msg):
    print(msg, flush=True)


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    log(md)


def gh(*args, check=True, capture=True):
    cmd = ["gh", *args]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {(r.stderr or r.stdout or '').strip()[:500]}")
    return r


def gh_json(*args, check=True):
    r = gh("api", *args, check=check)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return json.loads(out) if out else None


def gh_assets(release_id):
    """Every asset on a release (paginated)."""
    r = gh("api", "--paginate", f"repos/{REPO}/releases/{release_id}/assets?per_page=100")
    # --paginate prints one JSON array per page back to back.
    assets = []
    dec = json.JSONDecoder()
    s = r.stdout.strip()
    i = 0
    while i < len(s):
        page, j = dec.raw_decode(s, i)
        assets.extend(page)
        i = j
        while i < len(s) and s[i].isspace():
            i += 1
    return assets


def safe_name(name):
    """Asset names GitHub keeps exactly as given."""
    n = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return n[:90] or "file"


def download_url(asset_name):
    return f"https://github.com/{REPO}/releases/download/{FILES_TAG}/{asset_name}"


def notes_from_body(body):
    """Release-note bullet lines ("- something") become the app's What's new list."""
    notes = []
    for line in (body or "").splitlines():
        m = re.match(r"^\s*[-*•]\s+(.+?)\s*$", line)
        if m:
            notes.append(m.group(1)[:200])
    return notes[:10]


def extract(zip_path, dest):
    """Unzip, then step inside a single top-level folder if that's all there is."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or any(p in (".", "..") or ":" in p for p in parts):
                raise RuntimeError(f"Unsafe path in zip: {info.filename!r}")
            if info.is_dir():
                continue
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    root = dest
    while True:
        entries = [e for e in os.listdir(root) if not e.startswith("__MACOSX")]
        if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
            root = os.path.join(root, entries[0])
        else:
            return root


def find_exe(root):
    forced = os.environ.get("EXE")
    if forced:
        return forced
    exes = [e for e in os.listdir(root) if e.lower().endswith(".exe") and os.path.isfile(os.path.join(root, e))]
    exes = [e for e in exes if not re.search(r"crash|unins|setup|redist", e, re.I)] or exes
    if not exes:
        raise RuntimeError("No .exe in the top folder of the zip. Zip the packaged Windows folder itself.")
    exes.sort(key=lambda e: -os.path.getsize(os.path.join(root, e)))
    return exes[0]


def fingerprint(path, size):
    """Whole-file SHA-256 plus one per part (parts are named by their own hash)."""
    whole = hashlib.sha256()
    parts = []  # an empty file has none: the app just creates it
    with open(path, "rb") as f:
        remaining = size
        while remaining > 0:
            want = min(PART_BYTES, remaining)
            ph = hashlib.sha256()
            got = 0
            while got < want:
                chunk = f.read(min(8 * 1024 * 1024, want - got))
                if not chunk:
                    raise RuntimeError(f"{path} changed while it was being read")
                whole.update(chunk)
                ph.update(chunk)
                got += len(chunk)
            parts.append({"size": got, "sha256": ph.hexdigest()})
            remaining -= got
    return whole.hexdigest(), parts


def build_file_list(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.lower().endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            size = os.path.getsize(full)
            sha, parts = fingerprint(full, size)
            if len(parts) == 1:
                parts[0]["asset"] = safe_name(f"{sha[:24]}-{fn}")
            else:
                for i, p in enumerate(parts):
                    p["asset"] = f"{p['sha256'][:24]}-part{i + 1}.bin"
            files.append({"path": rel, "size": size, "sha256": sha, "parts": parts, "_full": full})
    return files


def write_parts(f, upload_dir, needed):
    """Put the bytes for each needed asset of file f into upload_dir/<asset>."""
    made = []
    if not f["parts"]:
        return made
    if len(f["parts"]) == 1:
        p = f["parts"][0]
        if p["asset"] in needed:
            dst = os.path.join(upload_dir, p["asset"])
            if not os.path.exists(dst):
                try:
                    os.link(f["_full"], dst)
                except OSError:
                    shutil.copyfile(f["_full"], dst)
            made.append(dst)
        return made
    with open(f["_full"], "rb") as src:
        for p in f["parts"]:
            if p["asset"] not in needed:
                src.seek(p["size"], 1)
                continue
            dst = os.path.join(upload_dir, p["asset"])
            with open(dst, "wb") as out:
                left = p["size"]
                while left:
                    chunk = src.read(min(8 * 1024 * 1024, left))
                    out.write(chunk)
                    left -= len(chunk)
            made.append(dst)
    return made


def upload(release_tag, paths):
    for path in paths:
        for attempt in range(1, 4):
            r = gh("release", "upload", release_tag, path, "--clobber", "-R", REPO, check=False)
            if r.returncode == 0:
                break
            log(f"  upload of {os.path.basename(path)} failed (try {attempt}): {r.stderr.strip()[:300]}")
            time.sleep(10 * attempt)
        else:
            raise RuntimeError(f"Couldn't upload {os.path.basename(path)}")
        log(f"  uploaded {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.1f} MB)")


def ensure_files_release():
    rel = gh_json(f"repos/{REPO}/releases/tags/{FILES_TAG}", check=False)
    if rel:
        return rel
    log("Creating the game-files release (it holds the files the app downloads)")
    return gh_json(
        f"repos/{REPO}/releases", "-X", "POST",
        "-f", f"tag_name={FILES_TAG}",
        "-f", "name=Game files for the Cafe Kinship app",
        "-f", "body=Used by the Games tab in the Cafe Kinship app for Windows to install and update the game. "
              "Filled in automatically: don't edit or delete it. To get the game, use the newest version release instead.",
        "-F", "prerelease=true",
        "-f", "make_latest=false",
    )


def fetch_manifest(release_assets, name="manifest.json"):
    a = next((x for x in release_assets if x["name"] == name), None)
    if not a:
        return None
    r = gh("api", "-H", "Accept: application/octet-stream", f"repos/{REPO}/releases/assets/{a['id']}", check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def main():
    if not TAG:
        sys.exit("TAG is required (the release tag to publish)")
    if TAG == FILES_TAG:
        log("That's the game-files release itself; nothing to do.")
        return
    os.makedirs(WORK, exist_ok=True)

    # 1. The release and its zip
    if DRY_RUN:
        zip_path = os.environ["ZIP"]
        body = os.environ.get("BODY", "")
    else:
        rel = gh_json(f"repos/{REPO}/releases/tags/{TAG}")
        zips = [a for a in rel["assets"] if a["name"].lower().endswith(".zip")]
        if not zips:
            summary(f"**{TAG}** has no .zip attached, so there was nothing to publish. Attach the game's zip and run this again.")
            return
        zip_asset = max(zips, key=lambda a: a["size"])
        log(f"Downloading {zip_asset['name']} ({zip_asset['size'] / 1e9:.2f} GB)")
        gh("release", "download", TAG, "-R", REPO, "-p", zip_asset["name"], "-D", WORK, "--clobber", capture=False)
        zip_path = os.path.join(WORK, zip_asset["name"])
        body = rel.get("body") or ""

    log("Unzipping")
    root = extract(zip_path, os.path.join(WORK, "game"))
    if not DRY_RUN:
        os.remove(zip_path)  # room on the runner's disk
    exe = find_exe(root)
    log(f"Game program: {exe}")

    log("Fingerprinting every file")
    files = build_file_list(root)
    total = sum(f["size"] for f in files)
    log(f"  {len(files)} files, {total / 1e9:.2f} GB")

    # 2. What's already stored, and what the previous version was
    if DRY_RUN:
        existing = set(json.loads(os.environ.get("EXISTING", "[]")))
        prev = json.loads(open(os.environ["PREV"]).read()) if os.environ.get("PREV") else None
        files_rel = None
    else:
        files_rel = ensure_files_release()
        assets = gh_assets(files_rel["id"])
        existing = {a["name"] for a in assets}
        prev = fetch_manifest(assets)

    needed = []
    seen = set()
    for f in files:
        for p in f["parts"]:
            if p["asset"] not in existing and p["asset"] not in seen:
                needed.append(p["asset"])
                seen.add(p["asset"])
    needed_set = set(needed)

    upload_dir = os.path.join(WORK, "upload")
    os.makedirs(upload_dir, exist_ok=True)
    uploaded_bytes = 0
    log(f"Uploading {len(needed)} new file(s)")
    for f in files:
        paths = write_parts(f, upload_dir, needed_set)
        if not paths:
            continue
        uploaded_bytes += sum(os.path.getsize(p) for p in paths)
        if not DRY_RUN:
            upload(FILES_TAG, paths)
        for p in paths:
            needed_set.discard(os.path.basename(p))
            os.remove(p)

    # 3. The manifest
    manifest = {
        "schema": 1,
        "game": "cafe-kinship",
        "name": "Cafe Kinship",
        "version": TAG[1:] if TAG[:1] in "vV" else TAG,
        "tag": TAG,
        "published": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exe": exe,
        "notes": notes_from_body(body),
        "totalBytes": total,
        "files": [
            {
                "path": f["path"],
                "size": f["size"],
                "sha256": f["sha256"],
                "parts": [{"url": download_url(p["asset"]), "size": p["size"]} for p in f["parts"]],
            }
            for f in files
        ],
    }
    manifest_path = os.path.join(WORK, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, separators=(",", ":"))

    # What a player on the previous version downloads
    prev_hashes = {pf["path"]: pf["sha256"] for pf in (prev or {}).get("files", [])}
    update_bytes = sum(f["size"] for f in files if prev_hashes.get(f["path"]) != f["sha256"])

    if DRY_RUN:
        log(json.dumps({"uploaded": len(needed), "uploadedBytes": uploaded_bytes, "updateBytes": update_bytes,
                        "manifest": manifest_path}))
        return

    # Swap manifest.json in last, so the app never sees a list of files that
    # aren't uploaded yet. Upload under a temporary name, then rename.
    nxt = os.path.join(WORK, "manifest.next.json")
    shutil.copyfile(manifest_path, nxt)
    upload(FILES_TAG, [nxt])
    assets = gh_assets(files_rel["id"])
    old = next((a for a in assets if a["name"] == "manifest.json"), None)
    new = next(a for a in assets if a["name"] == "manifest.next.json")
    if old:
        gh("api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{old['id']}")
    gh("api", "-X", "PATCH", f"repos/{REPO}/releases/assets/{new['id']}", "-f", "name=manifest.json")
    # A copy on the version's own release, for the record.
    upload(TAG, [manifest_path])

    # 4. Tidy up files no longer used by this version or the previous one
    keep = {"manifest.json"}
    for m in (manifest, prev or {}):
        for pf in m.get("files", []):
            for p in pf.get("parts", []):
                keep.add(p["url"].rsplit("/", 1)[-1])
    removed = 0
    for a in gh_assets(files_rel["id"]):
        if a["name"] not in keep:
            gh("api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{a['id']}")
            removed += 1

    summary(
        f"### Published Cafe Kinship {manifest['version']}\n"
        f"- {len(files)} files, {total / 1e9:.2f} GB in all\n"
        f"- Uploaded this time: {len(needed)} file(s), {uploaded_bytes / 1e6:.0f} MB\n"
        + (f"- Players on {prev.get('version')} download **{update_bytes / 1e6:.0f} MB**\n" if prev else "")
        + f"- Old files removed: {removed}\n"
        f"- The app sees the update now."
    )


if __name__ == "__main__":
    main()
