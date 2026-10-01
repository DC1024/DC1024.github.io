#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取 B 站指定 UP 主「播放量最高」的 N 个视频，生成 videos.json 与本地素材。

为什么用 App 网关而不是网页 API：
  space.bilibili.com 已改为纯客户端渲染的 SPA（shanks/fresh-space），
  页面里没有 __INITIAL_STATE__；而 /x/space/wbi/arc/search 这类网页接口
  在数据中心 IP 上会被 WAF 拦（412 / -352 / -799）。App 网关走
  appkey + appsec 的 md5 签名，签名正确即放行，是目前最稳的服务端抓取路径。

产物：
  videos.json                      —— 站点直接 fetch 渲染卡片
  assets/videos/<bvid>.jpg         —— 封面（可选降采样到 720 宽）
  assets/videos/<bvid>_sprite.jpg  —— B 站官方视频缩略图雪碧图（hover 预览用）

设计原则：
  1) 只用标准库（Pillow 可选，仅用于压缩封面）。
  2) 失败安全：任何一步拿不到有效数据，都不覆盖已有的 videos.json。
  3) 幂等：素材已存在且非空则跳过下载，避免每天重复拉取。

依赖：Python 3.8+；可选 Pillow（封面降采样）。
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 配置

MID = os.environ.get("BILI_MID", "1970862788")          # UP 主 UID
TOP_N = int(os.environ.get("BILI_TOP_N", "3"))          # 取前几名
BUILD = "7600300"

# 客户端 appkey / appsec 对。签名正确即放行，多个可互为备份
# （实测 android 与 bstar 两套都能通过；android_tv 系列签名算法不同，别用）。
APP_KEYS = [
    ("android", "1d8b6e7d45233436", "560c52ccd288fed045859ed18bffd973"),
    ("bstar", "7d089525d3611b1c", "acd495b248ec528c2eed1e862d393126"),
]

UA_APP = ("Mozilla/5.0 BiliDroid/7.60.0 (bbcallen@gmail.com) os/android "
          "model/Pixel6 mobi_app/android build/%s channel/bili innerVer/%s"
          % (BUILD, BUILD))
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CST = timezone(timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # 仓库根 = index.html 所在目录
ASSET_DIR = os.path.join(ROOT, "assets", "videos")
JSON_PATH = os.path.join(ROOT, "videos.json")

COVER_MAX_W = 720          # 封面降采样目标宽度（Pillow 可用时生效）
COVER_QUALITY = 82

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_SSL))


def log(msg: str) -> None:
    print("[bili] %s" % msg, flush=True)


# ---------------------------------------------------------------- HTTP


def http_bytes(url: str, headers: dict | None = None, timeout: int = 30,
               tries: int = 3) -> bytes:
    """GET 原始字节，带指数退避重试。"""
    h = {"User-Agent": UA_WEB, "Referer": "https://www.bilibili.com/"}
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:          # noqa: BLE001 - 网络异常种类多，统一重试
            last = e
            if i < tries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET failed %s: %r" % (url, last))


def http_json(url: str, headers: dict | None = None, timeout: int = 25,
              tries: int = 3) -> dict:
    raw = http_bytes(url, headers=headers, timeout=timeout, tries=tries)
    return json.loads(raw.decode("utf-8", "replace"))


def app_api(path: str, params: dict, appkey: str, appsec: str) -> dict:
    """调用 App 网关：appkey + ts + md5 签名。"""
    p = dict(params)
    p.update({"appkey": appkey, "ts": str(int(time.time()))})
    qs = urllib.parse.urlencode(sorted(p.items()))
    sign = hashlib.md5((qs + appsec).encode("utf-8")).hexdigest()
    url = "https://app.bilibili.com%s?%s&sign=%s" % (path, qs, sign)
    return http_json(url, headers={"User-Agent": UA_APP,
                                   "Accept": "application/json"})


def web_api(path: str) -> dict:
    return http_json("https://api.bilibili.com" + path,
                     headers={"Accept": "application/json"})


# ---------------------------------------------------------------- 抓取


def _list_with_key(order: str, pages: int, appkey: str, appsec: str) -> list[dict]:
    out: list[dict] = []
    cursor = ""
    for page in range(pages):
        params = {
            "vmid": MID, "mobi_app": "android", "platform": "android",
            "build": BUILD, "order": order, "ps": "20",
        }
        if cursor:
            params["aid"] = cursor
        try:
            j = app_api("/x/v2/space/archive/cursor", params, appkey, appsec)
        except Exception as e:                                  # noqa: BLE001
            log("  列表请求异常(order=%s, page=%d): %r" % (order, page + 1, e))
            break
        if j.get("code") != 0:
            log("  列表返回 code=%s msg=%s (order=%s)" % (
                j.get("code"), j.get("message"), order))
            break
        items = (j.get("data") or {}).get("item") or []
        if not items:
            break
        out.extend(items)
        nxt = str(items[-1].get("param") or "")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.4)                     # 轻微限速，别把接口打急
    return out


def list_videos(order: str, pages: int = 2) -> list[dict]:
    """
    拉取 UP 主的视频列表，多套 appkey 依次尝试。
    该接口是游标式的：pn 不生效，需要用上一页最后一条的 aid 作为 cursor 往后翻。
    """
    raw: list[dict] = []
    for name, ak, asec in APP_KEYS:
        raw = _list_with_key(order, pages, ak, asec)
        if raw:
            log("  使用 %s 客户端取到 %d 条" % (name, len(raw)))
            break
        log("  %s 客户端无数据，换下一套 key" % name)
    # 去重（cursor 翻页可能回带首条）
    seen, uniq = set(), []
    for x in raw:
        k = x.get("bvid") or x.get("param")
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def classify(raw: dict) -> dict:
    """把 App 列表项转成站点需要的结构。"""
    bvid = raw.get("bvid") or ""
    aid = str(raw.get("param") or raw.get("aid") or "")
    return {
        "bvid": bvid,
        "aid": aid,
        "title": (raw.get("title") or "").strip(),
        "play": int(raw.get("play") or 0),
        "danmaku": int(raw.get("danmaku") or 0),
        "duration": int(raw.get("duration") or 0),
        "pubdate": int(raw.get("ctime") or 0),
        "tname": raw.get("tname") or "",
        "cover_remote": (raw.get("cover") or "").replace("http://", "https://"),
        "url": "https://www.bilibili.com/video/%s/" % bvid if bvid else "",
    }


def fetch_stats_and_cid(v: dict) -> None:
    """补 cid 与精确播放量（列表里的 play 可能有延迟）。失败不致命。"""
    try:
        j = web_api("/x/web-interface/view?aid=%s" % v["aid"])
    except Exception as e:                                       # noqa: BLE001
        log("  view 接口失败 %s: %r" % (v["bvid"], e))
        return
    d = j.get("data") or {}
    if j.get("code") != 0 or not d:
        return
    pages = d.get("pages") or []
    if pages:
        v["cid"] = str(pages[0].get("cid") or "")
    stat = d.get("stat") or {}
    if stat.get("view"):
        v["play"] = int(stat["view"])
    if stat.get("danmaku") is not None:
        v["danmaku"] = int(stat.get("danmaku") or 0)
    if d.get("duration"):
        v["duration"] = int(d["duration"])
    if d.get("pubdate"):
        v["pubdate"] = int(d["pubdate"])
    if d.get("pic"):
        v["cover_remote"] = d["pic"].replace("http://", "https://")


def fetch_sprite(v: dict) -> None:
    """抓 B 站官方缩略图雪碧图，用于卡片 hover 自动预览。"""
    if not v.get("cid"):
        return
    # 必须带 index=1：不带时 index 恒为空数组，我们就无从知道雪碧图里
    # 真正有效的格子数。接口偶发会返回空 index，所以这里重试几次。
    d = {}
    for attempt in range(3):
        try:
            j = web_api("/x/player/videoshot?aid=%s&cid=%s&index=1"
                        % (v["aid"], v["cid"]))
        except Exception as e:                                   # noqa: BLE001
            log("  videoshot 失败 %s: %r" % (v["bvid"], e))
            break
        if j.get("code") != 0:
            log("  videoshot code=%s %s" % (j.get("code"), j.get("message")))
            break
        d = j.get("data") or {}
        if d.get("index"):
            break
        time.sleep(1.0 * (attempt + 1))
    if not d:
        return
    images = d.get("image") or []
    if not images:
        return
    url = images[0]
    if url.startswith("//"):
        url = "https:" + url
    cols = int(d.get("img_x_len") or 10)
    rows = int(d.get("img_y_len") or 10)
    index = d.get("index") or []
    # index 是每帧的时间戳数组，但**开头有一个重复的占位 0**
    # （实测 [0,0,5,10,...]），真实帧数 = len(index) - 1。
    # 这个值只是备选：雪碧图里真正填了几格由 detect_frames 说了算。
    frames = max(1, len(index) - 1) if index else 0
    frames = max(0, min(int(frames), cols * rows))
    v["sprite_remote"] = url
    v["sprite_cols"] = cols
    v["sprite_rows"] = rows
    v["sprite_fw"] = int(d.get("img_x_size") or 480)
    v["sprite_fh"] = int(d.get("img_y_size") or 270)
    v["frames"] = frames


# ---------------------------------------------------------------- 素材


def download_asset(url: str, dest: str, shrink_cover: bool = False) -> bool:
    """下载到本地。已存在且非空则跳过（幂等）。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 2048:
        return True
    try:
        data = http_bytes(url, timeout=40)
    except Exception as e:                                       # noqa: BLE001
        log("  素材下载失败 %s: %r" % (url, e))
        return False
    if len(data) < 512:
        return False
    if shrink_cover:
        data = _maybe_shrink(data) or data
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    log("  写入 %s (%.1f KB)" % (os.path.relpath(dest, ROOT), len(data) / 1024.0))
    return True


def detect_frames(path: str, cols: int, rows: int):
    """
    数出雪碧图里真正填了内容的格子数 —— 这是图像本身给出的事实，
    比接口的 index 数组更可信，所以优先用它。

    做法：整图 BOX 降到 cols×rows，每个像素即该格平均亮度；B 站把没用到的
    格子留成**纯黑（实测精确为 0）**，于是从后往前找第一个 >2 的格子即可。
    阈值取 2 而不是更高，是为了保住视频结尾那种确实很暗的真实帧。

    返回 None 表示判断不了（没装 Pillow / 打不开图），由调用方决定退路。
    """
    try:
        from PIL import Image
    except Exception:                                            # noqa: BLE001
        return None
    try:
        with Image.open(path) as im:
            g = im.convert("L")
            if g.width < cols or g.height < rows:
                return None
            small = g.resize((cols, rows), Image.BOX)
            px = small.tobytes()        # mode L 下每像素 1 字节
            # 用 tobytes() 而不是已弃用的 getdata()（Pillow 14 会移除）
        total = cols * rows
        if len(px) < total:
            return None
        last = -1
        for i in range(total - 1, -1, -1):
            if px[i] > 2:
                last = i
                break
        return last + 1 if last >= 0 else None
    except Exception:                                            # noqa: BLE001
        return None


def prune_assets(keep: list[str]) -> None:
    """删掉 assets/videos 里已经没人引用的封面/雪碧图，避免仓库无限膨胀。"""
    if not os.path.isdir(ASSET_DIR):
        return
    keep_set = {os.path.basename(p) for p in keep if p}
    for name in sorted(os.listdir(ASSET_DIR)):
        if name in keep_set:
            continue
        path = os.path.join(ASSET_DIR, name)
        if os.path.isfile(path):
            os.remove(path)
            log("  清理不再引用的素材 %s" % name)


def _maybe_shrink(data: bytes):
    """有 Pillow 就把封面压到 COVER_MAX_W 宽（省流量）；没有就原样返回。"""
    try:
        import io

        from PIL import Image
    except Exception:                                            # noqa: BLE001
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width <= COVER_MAX_W:
            return None
        h = round(im.height * COVER_MAX_W / im.width)
        im = im.resize((COVER_MAX_W, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=COVER_QUALITY, optimize=True, progressive=True)
        out = buf.getvalue()
        return out if len(out) < len(data) else None
    except Exception:                                            # noqa: BLE001
        return None


# ---------------------------------------------------------------- 主流程


def build() -> int:
    log("目标 UP: %s，取播放量前 %d 名" % (MID, TOP_N))

    # 1) 拿「最多播放」排序的列表；失败则退回「最新发布」本地排序
    videos = list_videos("click", pages=2)
    if not videos:
        log("click 排序不可用，回退 pubdate + 本地排序")
        videos = list_videos("pubdate", pages=3)
    if not videos:
        log("!! 列表拉取失败，保留现有 videos.json 不动")
        return 1

    cand = [classify(x) for x in videos]
    cand = [c for c in cand if c["bvid"]]
    if not cand:
        log("!! 列表为空或解析失败，保留现有 videos.json 不动")
        return 1

    cand.sort(key=lambda x: x["play"], reverse=True)
    top = cand[:TOP_N]
    log("候选 %d 条，取前 %d 条" % (len(cand), len(top)))

    # 2) 逐个补详情 + 预览雪碧图
    ok: list[dict] = []
    for i, v in enumerate(top, 1):
        log("%d) %s  play=%s  %s" % (i, v["bvid"], v["play"], v["title"][:40]))
        fetch_stats_and_cid(v)
        fetch_sprite(v)
        time.sleep(0.5)

        cover_local = "assets/videos/%s.jpg" % v["bvid"]
        sprite_local = "assets/videos/%s_sprite.jpg" % v["bvid"]
        got_cover = bool(v["cover_remote"]) and download_asset(
            v["cover_remote"], os.path.join(ROOT, cover_local), shrink_cover=True)
        got_sprite = bool(v.get("sprite_remote")) and download_asset(
            v["sprite_remote"], os.path.join(ROOT, sprite_local))

        v["rank"] = i
        v["cover"] = cover_local if got_cover else v["cover_remote"]
        frames_api = int(v.pop("frames", 0) or 0)
        cols = int(v.get("sprite_cols") or 10)
        rows = int(v.get("sprite_rows") or 10)
        frames = frames_api
        if got_sprite:
            # 图像是事实来源：接口的 index 偶发为空，且其长度比真实帧数多 1
            detected = detect_frames(os.path.join(ROOT, sprite_local), cols, rows)
            if detected:
                if detected != frames_api:
                    log("  有效帧数取图像实测 %d（接口给的 %d）" % (detected, frames_api))
                frames = detected
        if got_sprite and frames:
            v["sprite"] = sprite_local
        else:
            v["sprite"] = ""            # 没帧就不做 hover 动画，只留封面
            frames = 0
        v["preview_frames"] = frames
        v.pop("cover_remote", None)
        v.pop("sprite_remote", None)
        ok.append(v)

    if not ok:
        log("!! 无有效结果，保留现有 videos.json 不动")
        return 1

    # 3) 清掉本轮没入选的旧素材
    prune_assets([x for v in ok for x in (v.get("cover"), v.get("sprite"))])

    payload = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "mid": MID,
        "space": "https://space.bilibili.com/%s" % MID,
        "sort": "play_desc",
        "total_scanned": len(cand),
        "videos": ok,
    }

    # 4) 内容没变就不写文件（避免每天一个无意义 commit）
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if _same(old.get("videos"), payload["videos"]):
                log("TOP%d 名单与统计数字都没变 —— 不重写 videos.json" % TOP_N)
                return 0
        except Exception:                                        # noqa: BLE001
            pass

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log("已写出 videos.json —— %s" % ", ".join(v["bvid"] for v in ok))
    return 0


def _same(old, new) -> bool:
    if not isinstance(old, list) or len(old) != len(new):
        return False
    keys = ("bvid", "play", "danmaku", "title", "duration",
            "preview_frames", "sprite", "cover")
    for a, b in zip(old, new):
        for k in keys:
            if a.get(k) != b.get(k):
                return False
    return True


if __name__ == "__main__":
    try:
        sys.exit(build())
    except KeyboardInterrupt:
        sys.exit(130)
