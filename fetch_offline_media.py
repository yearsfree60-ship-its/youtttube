"""
Downloads YouTube videos for OFFLINE playback inside the library site.

Usage (run from the repository root by .github/workflows/offline-video.yml):
    python fetch_offline_media.py

Environment:
    VIDEO_IDS     space / comma / newline separated YouTube video ids (11 chars each)
    QUALITY       max height: 240 | 360 | 480            (default 360)
    FORCE         "true" = download again even if media/<id>.mp4 already exists
    YOUTUBE_COOKIES / YOUTUBE_COOKIES_B64 / YOUTUBE_MAX_MINUTES   see download_youtube_audio.py

Output:
    media/<videoId>.mp4     one phone-friendly H.264/AAC file per video (<= 90 MB each,
                            the height is lowered automatically when a file would be bigger)
    media/index.json        {"<videoId>": {"s": bytes, "h": height, "d": seconds, "t": unix_time}}

The site reads media/index.json from raw.githubusercontent.com, downloads the file of the
video being watched into IndexedDB and then plays it from the device, so playback no longer
depends on the network.
"""

import json
import os
import re
import sys
import time

import download_youtube_audio as base
import download_youtube_video as dv

MEDIA_DIR = "media"
INDEX_PATH = os.path.join(MEDIA_DIR, "index.json")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HEIGHTS = (480, 360, 240, 144)
MAX_FILE_MB = 90.0      # GitHub rejects files over 100 MB
MAX_RUN_MB = 1200.0     # keep one push comfortably under GitHub's limits


def load_index():
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(idx):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, INDEX_PATH)


def parse_ids(raw):
    seen, out, bad = set(), [], []
    for tok in re.split(r"[\s,;]+", raw or ""):
        if not tok:
            continue
        if not ID_RE.match(tok):
            bad.append(tok[:20])
        elif tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out, bad


def fetch_one(vid, cookie_path, height, max_minutes):
    """Returns (path, duration, height_used). Lowers the height when the file is too big."""
    url = f"https://www.youtube.com/watch?v={vid}"
    stem = f"tmp_{vid}"
    heights = [h for h in HEIGHTS if h <= height] or [HEIGHTS[-1]]
    last = "download failed"
    for h in heights:
        too_big = False
        for attempt in range(1, base.ATTEMPTS + 1):
            try:
                path, actual = dv.download_video(url, stem, cookie_path, max_minutes, h, MAX_FILE_MB)
                if os.path.getsize(path) > MAX_FILE_MB * 1_048_576:
                    os.remove(path)
                    raise base.UserFacingError("too big", retryable=False)
                return path, actual, h
            except base.UserFacingError as e:
                last, retryable = str(e), e.retryable
            except Exception as e:  # yt-dlp / network errors
                last, retryable = base.classify_error(str(e), bool(cookie_path))
            too_big = last == "too big" or last.startswith("No .mp4")
            if not retryable:
                break
            if attempt < base.ATTEMPTS:
                time.sleep(base.RETRY_PAUSE_S * attempt)
        if not too_big:
            break  # a lower height only helps when the problem is the file size
        print(f"  {vid}: too large at {h}p, trying a lower quality...")
    raise RuntimeError(last)


def main():
    ids, bad = parse_ids(os.environ.get("VIDEO_IDS", ""))
    if bad:
        print(f"::warning::Ignored invalid ids: {', '.join(bad)}")
    if not ids:
        print("::error::No valid video ids were given.")
        sys.exit(1)
    try:
        height = int(os.environ.get("QUALITY") or 360)
    except ValueError:
        height = 360
    force = (os.environ.get("FORCE") or "").lower() == "true"
    try:
        max_minutes = float(os.environ.get("YOUTUBE_MAX_MINUTES") or base.DEFAULT_MAX_MINUTES)
    except ValueError:
        max_minutes = float(base.DEFAULT_MAX_MINUTES)

    os.makedirs(MEDIA_DIR, exist_ok=True)
    idx = load_index()
    ok, skipped, failed, deferred = [], [], [], []
    run_mb = 0.0

    cookie_path = base.write_cookie_file(base.load_cookie_text())
    try:
        for n, vid in enumerate(ids, 1):
            dest = os.path.join(MEDIA_DIR, f"{vid}.mp4")
            if not force and vid in idx and os.path.exists(dest):
                skipped.append(vid)
                continue
            if run_mb >= MAX_RUN_MB:
                deferred.append(vid)
                continue
            print(f"[{n}/{len(ids)}] {vid}")
            try:
                path, actual, h = fetch_one(vid, cookie_path, height, max_minutes)
            except Exception as e:
                failed.append((vid, str(e)))
                print(f"::warning::{vid}: {e}")
                continue
            size = os.path.getsize(path)
            os.replace(path, dest)
            run_mb += size / 1_048_576
            idx[vid] = {"s": size, "h": h, "d": int(round(actual or 0)), "t": int(time.time())}
            save_index(idx)
            ok.append(vid)
            print(f"  saved {dest} ({size / 1_048_576:.1f} MB, {h}p)")
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass

    lines = [f"### Offline media: {len(ok)} saved, {len(skipped)} already present, {len(failed)} failed, {len(deferred)} deferred"]
    for vid, why in failed:
        lines.append(f"- failed `{vid}`: {why}")
    if deferred:
        lines.append(f"- deferred (run size cap {MAX_RUN_MB:.0f} MB, run again): " + " ".join(f"`{v}`" for v in deferred))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if failed and not ok and not skipped:
        sys.exit(1)


if __name__ == "__main__":
    main()
