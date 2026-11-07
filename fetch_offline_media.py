"""
Downloads YouTube videos for OFFLINE playback inside the library site.

Usage (run from the repository root by .github/workflows/offline-video.yml):
    python fetch_offline_media.py

Environment:
    VIDEO_IDS     space / comma / newline separated YouTube video ids (11 chars each)
    QUALITY       max height: 144 | 240 | 360 | 480 | 720           (default 360)
    FORCE         "true" = download again even if media/<id>.mp4 already exists
    YOUTUBE_COOKIES / YOUTUBE_COOKIES_B64 / YOUTUBE_MAX_MINUTES   see download_youtube_audio.py

Output:
    media/<videoId>.mp4     (or media/<videoId>.p1.mp4, .p2.mp4 ... when longer than the cap: split into parts)
                            one phone-friendly H.264/AAC file per video (<= 97 MB each,
                            the height is lowered automatically when a file would be bigger)
    media/index.json        {"<videoId>": {"s": bytes, "h": height, "q": requested_height, "d": seconds, "t": unix_time}
                            split videos also have "n": parts, "ps": [part bytes], "pd": [part seconds]}

The site reads media/index.json from raw.githubusercontent.com, downloads the file of the
video being watched into IndexedDB and then plays it from the device, so playback no longer
depends on the network.
"""

import glob
import json
import math
import os
import re
import subprocess
import sys
import time

import download_youtube_audio as base
import download_youtube_video as dv

MEDIA_DIR = "media"
INDEX_PATH = os.path.join(MEDIA_DIR, "index.json")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HEIGHTS = (720, 480, 360, 240, 144)
MAX_SRC_MB = 1500.0     # حد تنزيل المصدر (يُقسَّم لاحقاً إلى أجزاء)
MAX_FILE_MB = 97.0      # GitHub يرفض أي ملف فوق 100 MB (حد صارم لا يمكن رفعه)
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


def probe(path):
    """Returns {"v": codec|None, "a": codec|None, "d": seconds} using ffprobe ({} on failure)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration",
             "-of", "json", path], capture_output=True, text=True, timeout=120).stdout
        data = json.loads(out or "{}")
    except Exception:
        return {}
    res = {"v": None, "a": None, "d": 0.0}
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not res["v"]:
            res["v"] = st.get("codec_name")
        elif st.get("codec_type") == "audio" and not res["a"]:
            res["a"] = st.get("codec_name")
    try:
        res["d"] = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        pass
    return res


def is_valid(path):
    """Playable everywhere: H.264 video + AAC audio."""
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        return False
    p = probe(path)
    return p.get("v") == "h264" and p.get("a") == "aac"


def fetch_audio(url, vid, cookie_path):
    """Separate AAC audio download (used when the video file came without any audio track)."""
    stem = f"tmpaud_{vid}"
    for f in os.listdir("."):
        if f.startswith(stem):
            os.remove(f)
    cmd = ["yt-dlp", "-f", "ba[acodec^=mp4a]/ba", "--no-playlist", "--retries", "5",
           "-o", stem + ".%(ext)s", url]
    if cookie_path:
        cmd[1:1] = ["--cookies", cookie_path]
    subprocess.run(cmd, check=True, timeout=3600)
    for f in os.listdir("."):
        if f.startswith(stem):
            return f
    raise base.UserFacingError("no audio stream", retryable=True)


def normalize(path, vid, url, cookie_path, height):
    """Makes the file universally playable: H.264 + AAC, faststart, with sound. Raises if no audio."""
    p = probe(path)
    if not p.get("v"):
        raise base.UserFacingError("corrupt video", retryable=True)
    aud = None
    if not p.get("a"):
        print(f"  {vid}: no audio track, fetching audio separately...")
        aud = fetch_audio(url, vid, cookie_path)
    out = f"tmpnorm_{vid}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path]
    if aud:
        cmd += ["-i", aud, "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    if p.get("v") == "h264":
        cmd += ["-c:v", "copy"]
    else:  # VP9 / AV1 inside mp4 do not play on many phones/browsers
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-vf", f"scale=-2:'min({height},ih)'"]
    if p.get("a") == "aac" and not aud:
        cmd += ["-c:a", "copy"]
    else:  # Opus/other audio inside mp4 plays silently on many devices
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "44100"]
    cmd += ["-movflags", "+faststart", "-shortest", out]
    try:
        subprocess.run(cmd, check=True, timeout=7200)
    finally:
        if aud and os.path.exists(aud):
            os.remove(aud)
    if not is_valid(out):
        if os.path.exists(out):
            os.remove(out)
        raise base.UserFacingError("no audio after processing", retryable=True)
    os.replace(out, path)
    return path


def has_faststart(path):
    """True when the 'moov' atom precedes 'mdat' (playback starts without downloading the whole file)."""
    try:
        with open(path, "rb") as f:
            pos, end = 0, os.path.getsize(path)
            while pos + 8 <= end:
                f.seek(pos)
                hdr = f.read(16)
                size = int.from_bytes(hdr[:4], "big")
                typ = hdr[4:8]
                if typ == b"moov":
                    return True
                if typ == b"mdat":
                    return False
                if size == 1:
                    size = int.from_bytes(hdr[8:16], "big")
                if size < 8:
                    return False
                pos += size
    except Exception:
        return True
    return False


def repair_faststart():
    """One-time maintenance: remux (copy, no re-encoding) any older media file whose moov atom is at the end."""
    fixed = []
    for f in sorted(glob.glob(os.path.join(MEDIA_DIR, "*.mp4"))):
        if has_faststart(f):
            continue
        tmp = f + ".fs.tmp.mp4"
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f, "-map", "0", "-c", "copy",
                            "-movflags", "+faststart", tmp], check=True, timeout=1800)
            if is_valid(tmp):
                os.replace(tmp, f)
                fixed.append(os.path.basename(f))
                print(f"  faststart repaired: {os.path.basename(f)}")
        except Exception as e:
            print(f"::warning::faststart repair failed for {f}: {e}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return fixed


def clear_media(vid):
    """Deletes any previous single file and parts of this video."""
    for f in [os.path.join(MEDIA_DIR, f"{vid}.mp4")] + glob.glob(os.path.join(MEDIA_DIR, f"{vid}.p*.mp4")):
        try:
            os.remove(f)
        except OSError:
            pass


def split_parts(path, vid, duration):
    """Splits an oversized mp4 (no re-encoding) into independent parts, each below MAX_FILE_MB.
    Returns ([sizes], [durations]); the files are written to media/<id>.p<N>.mp4."""
    limit = MAX_FILE_MB * 1_048_576
    size = os.path.getsize(path)
    duration = duration or probe(path).get("d") or 1.0
    n = max(2, math.ceil(size / (limit * 0.85)))
    pattern = f"tmpseg_{vid}_%02d.mp4"
    for _ in range(8):
        for f in glob.glob(f"tmpseg_{vid}_*.mp4"):
            os.remove(f)
        seg = max(20.0, duration / n)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy",
                        "-f", "segment", "-segment_time", f"{seg:.2f}", "-reset_timestamps", "1",
                        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
                        pattern], check=True, timeout=7200)
        files = sorted(glob.glob(f"tmpseg_{vid}_*.mp4"))
        if files and all(os.path.getsize(f) <= limit and is_valid(f) for f in files):
            break
        n = int(n * 1.3) + 1
    else:
        raise base.UserFacingError("could not split below the size cap", retryable=False)
    clear_media(vid)
    sizes, durs = [], []
    for i, f in enumerate(files, 1):
        sizes.append(os.path.getsize(f))
        durs.append(round(probe(f).get("d") or 0, 2))
        os.replace(f, os.path.join(MEDIA_DIR, f"{vid}.p{i}.mp4"))
    os.remove(path)
    return sizes, durs


def entry_ok(vid, ent):
    """True when the stored file(s) of this video exist and are playable (H.264 + AAC)."""
    n = int(ent.get("n") or 1)
    if n <= 1:
        return is_valid(os.path.join(MEDIA_DIR, f"{vid}.mp4"))
    return all(is_valid(os.path.join(MEDIA_DIR, f"{vid}.p{i}.mp4")) for i in range(1, n + 1))


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
                path, actual = dv.download_video(url, stem, cookie_path, max_minutes, h, MAX_SRC_MB)
                path = normalize(path, vid, url, cookie_path, h)
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
    fixed = repair_faststart()   # صيانة: إصلاح الملفات القديمة التي لا تبدأ بسرعة (بدون إعادة ترميز)
    if fixed:
        for name in fixed:
            ent = idx.get(name.split(".")[0])
            if ent:
                ent["t"] = int(time.time())   # يغيّر رابط التشغيل فلا يستخدم المتصفح النسخة القديمة
        save_index(idx)
        print(f"faststart repaired for {len(fixed)} file(s)")
    run_mb = 0.0

    cookie_path = base.write_cookie_file(base.load_cookie_text())
    try:
        for n, vid in enumerate(ids, 1):
            dest = os.path.join(MEDIA_DIR, f"{vid}.mp4")
            prev = idx.get(vid) or {}
            same_q = int(prev.get("q") or prev.get("h") or 0) == height
            if not force and vid in idx and same_q and entry_ok(vid, prev):
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
            ent = {"h": h, "q": height, "d": int(round(actual or 0)), "t": int(time.time())}
            if size > MAX_FILE_MB * 1_048_576:
                try:
                    sizes, durs = split_parts(path, vid, actual)
                except Exception as e:
                    failed.append((vid, f"split failed: {e}"))
                    print(f"::warning::{vid}: split failed: {e}")
                    continue
                ent.update({"s": sum(sizes), "n": len(sizes), "ps": sizes, "pd": durs})
                size = sum(sizes)
                print(f"  saved {len(sizes)} parts ({size / 1_048_576:.1f} MB, {h}p)")
            else:
                clear_media(vid)
                os.replace(path, dest)
                ent["s"] = size
                print(f"  saved {dest} ({size / 1_048_576:.1f} MB, {h}p)")
            run_mb += size / 1_048_576
            idx[vid] = ent
            save_index(idx)
            ok.append(vid)
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
