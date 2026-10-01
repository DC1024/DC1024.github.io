#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描原始摄影作品（png/jpg），生成站点用的多尺寸 WebP 与 photos.json。

设计原则（和 fetch_bilibili_top.py 保持一致）：
  1) 幂等：源图没变就不重新编码，避免每天一个无意义 commit。
  2) 失败安全：任何一张图出错只跳过它，不影响其它照片；一张都读不到时不覆盖已有 photos.json。
  3) 源图与产物分离：png 原图**不入库**（体积大），仓库里只留 WebP 产物。

目录约定：
  photos-src/                       <- 你把 png 丢这里（不进 git）
    ├─ 01-kyoto-rain.png
    ├─ 02-...
    └─ photos.meta.json             <- 可选：手工补充标题/地点/顺序

产物：
  assets/photos/<slug>-thumb.webp   <- 卡片缩略图（约 20-60 KiB）
  assets/photos/<slug>-mid.webp     <- 灯箱中图（约 150-400 KiB）
  assets/photos/<slug>-full.webp    <- 灯箱大图（约 500-1500 KiB，按需要才加载）
  photos.json                       <- 站点数据

用法：
  python scripts/build_photos.py            # 增量构建
  python scripts/build_photos.py --force    # 强制重编码（改了尺寸参数后用）
  python scripts/build_photos.py --check    # 只检查是否需要更新，不写文件

依赖：Python 3.8+；Pillow（必需，没有它无法转 WebP）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 仓库根 = index.html 所在目录
SRC_DIR = os.path.join(ROOT, "photos-src")
OUT_DIR = os.path.join(ROOT, "assets", "photos")
JSON_PATH = os.path.join(ROOT, "photos.json")
META_PATH = os.path.join(SRC_DIR, "photos.meta.json")

CST = timezone(timedelta(hours=8))

# 三档尺寸。数值是「长边」——竖构图与横构图都能用同一套规则，不必分别配。
# 卡片一排三张：1440 视口下每张约 420px 宽，高 DPR 屏要 840px，所以 thumb 给 900。
THUMB_LONG = 900
MID_LONG = 1800
FULL_LONG = 2560

# 画质：缩略图可以激进一点（它是首屏成本），大图要克制（它是你调色的成品）。
Q_THUMB = 78
Q_MID = 82
Q_FULL = 86

SRC_EXT = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff")


def log(msg: str) -> None:
    print("[photos] %s" % msg, flush=True)


def slugify(name: str) -> str:
    """把文件名变成安全的 URL 片段。

    中文文件名直接用也能跑（GitHub Pages 支持），但百分号编码后很长、
    且在不同平台上的规范化行为不一致 —— 所以统一转成 ASCII slug，
    真正的标题放在 photos.json 里，展示不受影响。
    """
    base = os.path.splitext(os.path.basename(name))[0]
    # 去掉开头的排序序号（01- 、1_ 、001. 等）—— 它是给你自己排序用的，不该进 URL
    base = re.sub(r"^\s*\d{1,3}\s*[-_.、]\s*", "", base)
    # 全角转半角，再取 ASCII
    base = unicodedata.normalize("NFKD", base)
    base = base.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return base or "photo"


def load_meta() -> dict:
    if not os.path.exists(META_PATH):
        return {}
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:                                       # noqa: BLE001
        log("!! photos.meta.json 解析失败，忽略：%r" % (e,))
        return {}


def fit(img, long_edge: int):
    """按长边等比缩放；本来就比目标小就不放大（放大只会变糊、白增体积）。"""
    from PIL import Image
    w, h = img.size
    cur = max(w, h)
    if cur <= long_edge:
        return img
    scale = long_edge / float(cur)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def encode_webp(img, dest: str, quality: int) -> int:
    """写 WebP，返回字节数。method=6 是最慢但压得最小的档位 —— 构建期一次性，值。"""
    img.save(dest, "WEBP", quality=quality, method=6)
    return os.path.getsize(dest)


def build_one(src_path: str, slug: str, force: bool) -> dict | None:
    """处理一张图，返回它的 photos.json 条目；失败返回 None。"""
    from PIL import Image

    outs = {
        "thumb": os.path.join(OUT_DIR, "%s-thumb.webp" % slug),
        "mid": os.path.join(OUT_DIR, "%s-mid.webp" % slug),
        "full": os.path.join(OUT_DIR, "%s-full.webp" % slug),
    }

    src_mtime = os.path.getmtime(src_path)
    all_exist = all(os.path.exists(p) and os.path.getsize(p) > 512 for p in outs.values())

    # 幂等：产物都在、且都比源图新，就跳过。--force 时无条件重做。
    # 注意这里不能只看「文件存在」—— 改过 THUMB_LONG 之类的参数后必须能重编码。
    if all_exist and not force:
        freshest_src = src_mtime
        oldest_out = min(os.path.getmtime(p) for p in outs.values())
        if oldest_out >= freshest_src:
            with Image.open(src_path) as im:
                w, h = im.size
            return {
                "slug": slug,
                "thumb": rel(outs["thumb"]), "mid": rel(outs["mid"]), "full": rel(outs["full"]),
                "width": w, "height": h,
                "reused": True,
            }

    try:
        with Image.open(src_path) as im:
            # png 可能是 RGBA / P 模式，WebP 支持透明，但我们要的是不透明照片；
            # 统一转到 RGB，避免带 alpha 的图在深色底上出现黑边。
            src_w, src_h = im.size
            img = im.convert("RGB")

            sizes = {}
            for key, long_edge, q in (("thumb", THUMB_LONG, Q_THUMB),
                                      ("mid", MID_LONG, Q_MID),
                                      ("full", FULL_LONG, Q_FULL)):
                v = fit(img, long_edge)
                sizes[key] = encode_webp(v, outs[key], q)
    except Exception as e:                                       # noqa: BLE001
        log("  !! %s 处理失败，跳过：%r" % (os.path.basename(src_path), e))
        return None

    log("  %s  %dx%d → thumb %dKB / mid %dKB / full %dKB" % (
        slug, src_w, src_h,
        sizes["thumb"] // 1024, sizes["mid"] // 1024, sizes["full"] // 1024))

    return {
        "slug": slug,
        "thumb": rel(outs["thumb"]), "mid": rel(outs["mid"]), "full": rel(outs["full"]),
        "width": src_w, "height": src_h,
    }


def rel(p: str) -> str:
    """统一成 posix 相对路径 —— 站点里直接当 URL 用。"""
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def main() -> int:
    force = "--force" in sys.argv[1:]
    check_only = "--check" in sys.argv[1:]

    try:
        import PIL  # noqa: F401
    except Exception:                                            # noqa: BLE001
        log("!! 没装 Pillow，无法生成 WebP。pip install pillow")
        return 0                    # 返回 0：缺依赖不该 fail 掉整条流水线

    if not os.path.isdir(SRC_DIR):
        log("没有 %s 目录，跳过（先在 photos-src/ 里放几张图）" % os.path.relpath(SRC_DIR, ROOT))
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    meta = load_meta()

    names = sorted(f for f in os.listdir(SRC_DIR) if f.lower().endswith(SRC_EXT))
    if not names:
        log("%s 里没有图片，跳过" % os.path.relpath(SRC_DIR, ROOT))
        return 0

    items, skipped = [], 0
    log("发现 %d 个源文件" % len(names))
    for n in names:
        slug = slugify(n)
        entry = build_one(os.path.join(SRC_DIR, n), slug, force)
        if entry is None:
            skipped += 1
            continue
        entry.pop("reused", None)
        # 标题与地点来自 photos.meta.json，键是「源文件名」，这样改名后仍能对上
        m = meta.get(n) or meta.get(slug) or {}
        entry["title"] = (m.get("title") or "").strip()
        entry["place"] = (m.get("place") or "").strip()
        entry["sort"] = m.get("sort")
        items.append(entry)

    if not items:
        log("!! 一张都没成功，保留现有 photos.json 不动")
        return 0

    # 排序：meta 里给了 sort 的优先按它排，其余按文件名顺序（已经 sorted 过）
    items.sort(key=lambda x: (x.get("sort") is None, x.get("sort") or 0, x["slug"]))
    for i, it in enumerate(items):
        it["rank"] = i + 1
        it.pop("sort", None)

    payload = {
        "generated_at": datetime.now(CST).replace(microsecond=0).isoformat(),
        "count": len(items),
        "stale_skipped": skipped,
        "photos": items,
    }

    # 幂等：generated_at 每次都会变，比较时把它排除掉，否则每天都会产生一个无意义 commit
    def strip_ts(d):
        d = dict(d)
        d.pop("generated_at", None)
        return d

    old = None
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:                                        # noqa: BLE001
            old = None

    if old is not None and json.dumps(strip_ts(old), sort_keys=True, ensure_ascii=False) == \
            json.dumps(strip_ts(payload), sort_keys=True, ensure_ascii=False):
        log("内容没变化（%d 张），photos.json 不动" % len(items))
        return 0

    if check_only:
        log("需要更新（--check 模式，不写文件）")
        return 1

    with open(JSON_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    total = sum(os.path.getsize(os.path.join(ROOT, it["thumb"])) for it in items)
    log("已写入 %s：%d 张（%s）缩略图合计 %.1f KiB" % (
        os.path.relpath(JSON_PATH, ROOT), len(items),
        "、".join(it["slug"] for it in items), total / 1024.0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
