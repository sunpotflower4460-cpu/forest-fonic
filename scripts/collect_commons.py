#!/usr/bin/env python3
"""Collect Wikimedia Commons reference photographs with source-level credit.

Designed for reference study, not automatic import into the game. The source
metadata is checked for an explicit reuse license before download.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import html
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
TOPICS = ROOT / "catalog" / "topics.json"
MANIFEST = ROOT / "catalog" / "images.json"
GALLERY = ROOT / "gallery"
API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "forest-fonic/1.0 (https://github.com/sunpotflower4460-cpu/forest-fonic; Wikimedia Commons reference research)"
LICENSES = {
    "CC0": 0,
    "CC BY 4.0": 1, "CC BY 3.0": 2, "CC BY 2.0": 3,
    "CC BY-SA 4.0": 4, "CC BY-SA 3.0": 5, "CC BY-SA 2.0": 6,
}
ALLOWED_HOSTS = {"commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"}
EXCLUDE_TITLE = re.compile(r"\b(map|logo|flag|diagram|screenshot|poster|drawing|painting|cartoon|svg|etching|sketch|watercolor|watercolour|engraving|illustration|depiction|studies|artwork|museum print|NGA [0-9]+|MET DP[0-9]+)\b", re.I)
EXCLUDE_ARTIST = re.compile(r"Rijksmuseum|Yale Center for British Art|National Gallery of Art|The Metropolitan Museum", re.I)
MAX_SOURCE_BYTES = 9_000_000
MAX_IMAGE_BYTES = 750_000


def plain(value: str) -> str:
    stripped = re.sub(r"<[^>]*>", "", value or "")
    return re.sub(r"\s+", " ", html.unescape(stripped)).strip()


def url_read(url: str, maximum: int, retries: int = 3) -> bytes:
    host = urlsplit(url).hostname
    if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"Refusing non-Wikimedia source: {host!r}")
    last = None
    for n in range(retries):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=40) as resp:
                final = urlsplit(resp.geturl())
                if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                    raise ValueError("Redirect to an unexpected host")
                content = resp.read(maximum + 1)
                if len(content) > maximum:
                    raise ValueError(f"Source larger than {maximum} bytes")
                return content
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last = exc
            if n + 1 < retries:
                time.sleep(2 ** (n + 1))
    raise RuntimeError(f"Download failed: {host}: {last}")


def search_topic(topic: dict, per_topic: int, excluded_ids: set[int]) -> list[dict]:
    found = {}
    for query in topic["queries"]:
        params = urlencode({
            "action": "query", "generator": "search",
            "gsrsearch": query + " filetype:bitmap",
            "gsrnamespace": "6", "gsrlimit": "40",
            "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "1280", "format": "json",
        })
        try:
            response = json.loads(url_read(API + "?" + params, 8_000_000))
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"WARN search {topic['slug']}: {exc}", file=sys.stderr)
            continue
        pages = sorted(response.get("query", {}).get("pages", {}).values(), key=lambda x: x.get("index", 9999))
        for p in pages:
            info = (p.get("imageinfo") or [None])[0]
            if not info:
                continue
            meta = info.get("extmetadata") or {}
            license_name = plain(meta.get("LicenseShortName", {}).get("value", ""))
            title = p.get("title", "")
            artist = plain(meta.get("Artist", {}).get("value", ""))
            if (license_name not in LICENSES or EXCLUDE_TITLE.search(title)
                    or EXCLUDE_ARTIST.search(artist) or p.get("pageid") in excluded_ids):
                continue
            if not title.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")):
                continue
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp", "image/tiff"):
                continue
            if min(info.get("width", 0), info.get("height", 0)) < 480:
                continue
            source_image = info.get("thumburl") or info.get("url")
            if not source_image or urlsplit(source_image).hostname not in ALLOWED_HOSTS:
                continue
            pageid = p["pageid"]
            if pageid in found:
                continue
            license_url = meta.get("LicenseUrl", {}).get("value", "")
            if license_url.startswith("http://"):
                license_url = "https://" + license_url[7:]
            found[pageid] = {
                "pageid": pageid,
                "title": title.removeprefix("File:"),
                "source_page": "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":/"),
                "image_url": source_image,
                "license": license_name,
                "license_url": license_url,
                "artist": artist[:250] or "See source file page",
                "source_width": info.get("width"),
                "source_height": info.get("height"),
                "_rank": p.get("index", 9999),
            }
        if len(found) >= per_topic * 3:
            break
        time.sleep(0.2)
    return sorted(found.values(), key=lambda x: (x["_rank"], LICENSES[x["license"]]))[: per_topic * 4]


def unique_slug(value: str) -> str:
    stem = Path(value).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:45]
    return stem or "reference"


def download_photo(job: tuple[dict, dict]) -> dict:
    topic, image = job
    raw = url_read(image["image_url"], MAX_SOURCE_BYTES)
    try:
        with Image.open(BytesIO(raw)) as source:
            source.load()
            if min(source.size) < 480:
                raise ValueError("Image dimensions too small")
            photo = ImageOps.exif_transpose(source)
            photo.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            out = BytesIO()
            photo.convert("RGB").save(out, "WEBP", quality=75, method=5)
            data = out.getvalue()
            width, height = photo.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError(f"Image decode error: {exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Transcoded image too large: {len(data)} bytes")
    dest = Path("assets") / topic["group"] / topic["slug"] / (
        str(image["pageid"]) + "-" + unique_slug(image["title"]) + ".webp"
    )
    absolute = ROOT / dest
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(data)
    return {
        **{k: v for k, v in image.items() if not k.startswith("_")},
        "path": dest.as_posix(),
        "group": topic["group"], "topic": topic["slug"],
        "topic_label": topic["label"], "study_focus": topic["focus"],
        "related_code": topic["related_code"],
        "width": width, "height": height, "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "conversion": "Wikimedia thumbnail scaled to <=1280px; WebP quality 75; EXIF stripped",
        "visual_review": "pending",
    }


def md_safe(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def write_indices(images: list[dict], topics: list[dict], generated: str) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "generated_utc": generated,
               "total": len(images), "images": images}
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    credits = [
        "# Image credits / 画像の出典とライセンス",
        "",
        "These are reference photographs, not unlicensed game textures. Every image retains its own license.",
        "Photos are resized and transcoded to WebP (quality 75); the originals remain at the linked Wikimedia pages.",
        "Creative Commons BY / BY-SA licenses require credit; ShareAlike conditions may apply if you adapt an image.",
        "",
    ]
    for img in images:
        credits += [
            f"- [{md_safe(img['title'])}]({img['source_page']}) — "
            f"{md_safe(img['artist'])}; [{md_safe(img['license'])}]({img['license_url']}); "
            f"local file: `{img['path']}` (resized/converted to WebP)."
        ]
    (ROOT / "catalog" / "CREDITS.md").write_text("\n".join(credits) + "\n", encoding="utf-8")
    GALLERY.mkdir(exist_ok=True)
    grouped = defaultdict(list)
    for img in images:
        grouped[img["group"]].append(img)
    intro = [
        "# Forest Fonic — gallery / 写真ギャラリー", "",
        f"現在 {len(images)} 枚。各写真の作者・ライセンスは [CREDITS](../catalog/CREDITS.md) を参照。", "",
        "写真は制作資料です。『こころのえほん』のコードやゲームには自動で取り込みません。", "",
    ]
    for group in dict.fromkeys(t["group"] for t in topics):
        group_items = grouped.get(group, [])
        intro.append(f"- [{group}](./{group}.md) — {len(group_items)} 枚")
        lines = [f"# {group} — reference photos", "",
                 "[← Gallery index](./README.md) | [Credits](../catalog/CREDITS.md)", ""]
        for topic in (t for t in topics if t["group"] == group):
            selected = [img for img in group_items if img["topic"] == topic["slug"]]
            lines += [f"## {topic['label']} — {topic['slug']}", "",
                      f"制作で見るところ：{topic['focus']}", "",
                      "関連コード：" + "、".join(f"`{x}`" for x in topic["related_code"]), ""]
            for img in selected:
                lines += [f"![{md_safe(img['title'])}](../{img['path']})", "",
                          f"{md_safe(img['title'])} — {md_safe(img['artist'])} / "
                          f"[{md_safe(img['license'])}]({img['license_url']}) / "
                          f"[原典]({img['source_page']})", ""]
            if not selected:
                lines += ["現時点でライセンス・画像条件を満たす写真なし（今後の収集対象）。", ""]
        (GALLERY / f"{group}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (GALLERY / "README.md").write_text("\n".join(intro) + "\n", encoding="utf-8")


def verify() -> int:
    if not MANIFEST.exists():
        print("Missing catalog/images.json", file=sys.stderr)
        return 1
    images = json.loads(MANIFEST.read_text(encoding="utf-8")).get("images", [])
    errors = []
    for i in images:
        if i.get("license") not in LICENSES:
            errors.append(f"Unsupported license: {i.get('path')}")
        path = ROOT / i["path"]
        if not path.is_file():
            errors.append(f"Missing: {path}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != i.get("sha256"):
            errors.append(f"Checksum mismatch: {path}")
    for issue in errors:
        print("ERROR " + issue, file=sys.stderr)
    print(f"Verified {len(images) - len(errors)}/{len(images)} images")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-total", type=int, default=240)
    parser.add_argument("--per-topic", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--only", help="Select one topic slug")
    args = parser.parse_args()
    if args.verify:
        return verify()
    all_topics = json.loads(TOPICS.read_text(encoding="utf-8"))["topics"]
    topics = all_topics
    if args.only:
        topics = [t for t in all_topics if t["slug"] == args.only]
        if not topics:
            parser.error(f"Unknown topic: {args.only}")
    if not (1 <= args.max_total <= 250 and 1 <= args.per_topic <= 6):
        parser.error("Limits outside safe range")
    existing = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    exclusions_file = ROOT / "catalog" / "exclusions.json"
    exclusions = json.loads(exclusions_file.read_text(encoding="utf-8")) if exclusions_file.exists() else {}
    excluded_ids = {int(p) for p in exclusions.get("excluded", {})}
    images = []
    assets_root = (ROOT / "assets").resolve()
    for item in existing.get("images", []):
        path = (ROOT / item["path"]).resolve()
        if item["pageid"] in excluded_ids:
            # Only discard our own generated file if its checksum is unchanged.
            if path.is_relative_to(assets_root) and path.is_file():
                if hashlib.sha256(path.read_bytes()).hexdigest() == item.get("sha256"):
                    path.unlink()
                    print(f"Curated out generated image: {item['path']}", flush=True)
                    continue
                print(f"WARN not removing modified file: {path}", file=sys.stderr)
            if path.is_file():
                images.append({**item, "visual_review": "needs_manual_review"})
        elif path.is_file():
            images.append(item)
    already = {i["pageid"] for i in images}
    topic_counts = Counter(i["topic"] for i in images)
    print(f"Existing: {len(images)} images; searching {len(topics)} topics", flush=True)
    proposals = {}
    pending_topics = [t for t in topics if topic_counts[t["slug"]] < args.per_topic]
    print(f"Topics needing new references: {len(pending_topics)}", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(search_topic, topic, args.per_topic, excluded_ids): topic for topic in pending_topics}
        for job in as_completed(jobs):
            topic = jobs[job]
            try:
                proposals[topic["slug"]] = job.result()
            except Exception as exc:
                print(f"WARN topic {topic['slug']}: {exc}", file=sys.stderr)
                proposals[topic["slug"]] = []
    selected = []
    chosen = set(already)
    for _round in range(args.per_topic):
        for topic in topics:
            if len(images) + len(selected) >= args.max_total:
                break
            slug = topic["slug"]
            if topic_counts[slug] >= args.per_topic:
                continue
            candidate = next((c for c in proposals[slug] if c["pageid"] not in chosen), None)
            if candidate:
                selected.append((topic, candidate))
                chosen.add(candidate["pageid"])
                topic_counts[slug] += 1
    print(f"New candidates: {len(selected)}", flush=True)
    if args.dry_run:
        for topic, img in selected:
            print(f"{topic['group']}/{topic['slug']}: {img['title']} ({img['license']})")
        return 0
    if selected:
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(download_photo, row): row for row in selected}
            for index, job in enumerate(as_completed(jobs), 1):
                topic, image = jobs[job]
                try:
                    saved = job.result()
                    images.append(saved)
                    print(f"[{index}/{len(jobs)}] saved {saved['path']}", flush=True)
                except Exception as exc:
                    print(f"WARN skipped {topic['slug']}: {image['title']}: {exc}", file=sys.stderr)
    order = {t["slug"]: n for n, t in enumerate(all_topics)}
    images.sort(key=lambda item: (order.get(item["topic"], 9999), item["pageid"]))
    if images or not MANIFEST.exists():
        generated = (datetime.now(timezone.utc).isoformat(timespec="seconds")
                     if selected else existing.get("generated_utc", datetime.now(timezone.utc).isoformat()))
        write_indices(images, all_topics, generated)
    print(f"Done. Catalog has {len(images)} images. Visual inspection is still required.", flush=True)
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
