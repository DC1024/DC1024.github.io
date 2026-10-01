#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 videos.json / photos.json 的内容内联进 index.html 的对应标记之间。

为什么需要这一步：
  站点靠 fetch('videos.json') 拿数据，意味着封面图必须等这一次网络往返回来、
  才知道自己的 URL。实测线上这条链是 TTFB 785ms → JSON 到 1110ms → 封面才开始下载，
  也就是 LCP 平白多等 325ms；而且「01 板块稍后才出现」还会引发整块重排（手机端 CLS 0.80）。
  把数据直接写进 HTML，首帧就能渲染出真卡片，这两件事一起解决。

  摄影区的照片同理：缩略图地址来自 photos.json，不内联就得多等一次往返。

安全：
  内联在 <script> 里，只要出现 "</script" 就会被解析器当成结束标签、把后面的 HTML
  吐成纯文本。所以把所有 "<" 转义成 \\u003c（JSON 合法转义，前端 JSON.parse 拿到的是
  原字符）。视频标题里出现 "<" 完全可能，不转义就是个定时炸弹。

幂等：
  内容没变就不重写 index.html，避免每天一个无意义 commit。

用法：
  python scripts/inject_seed.py          # 就地更新 index.html
  python scripts/inject_seed.py --check   # 只检查是否需要更新，不写文件
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML_PATH = os.path.join(ROOT, "index.html")

# (JSON 文件名, 起始标记, 结束标记, <script> 的 id, 必须存在的键)
# 必须存在的键用来判断「这份数据是不是空的」：空的话保持页面上现有种子不动。
DATASETS = [
    ("videos.json", "<!-- VIDEO_SEED_START -->", "<!-- VIDEO_SEED_END -->", "videoSeed", "videos"),
    ("photos.json", "<!-- PHOTO_SEED_START -->", "<!-- PHOTO_SEED_END -->", "photoSeed", "photos"),
]


def log(msg: str) -> None:
    print("[seed] %s" % msg, flush=True)


def build_block(payload: dict, script_id: str) -> str:
    """把数据序列化成一段可直接放进 <script type=application/json> 的文本。"""
    # separators 去掉多余空格；ensure_ascii=False 保留中文（HTML 本身是 UTF-8）
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    safe = raw.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return '<script type="application/json" id="%s">%s</script>' % (script_id, safe)


def inject_one(html: str, json_name: str, start: str, end: str, script_id: str, key: str):
    """就地把一份数据注入 html，返回 (新 html, 是否有改动, 返回码)。"""
    json_path = os.path.join(ROOT, json_name)
    rel_json = json_name

    if not os.path.exists(json_path):
        # 摄影区还没铺数据时属于正常情况（photos.json 还没生成），静默跳过
        log("%s 不存在，跳过" % rel_json)
        return html, False, 0

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:                                       # noqa: BLE001
        log("!! %s 解析失败：%r" % (rel_json, e))
        return html, False, 2

    if not (payload.get(key) or []):
        log("!! %s 里没有 %s，跳过（保持页面上现有的种子不动）" % (rel_json, key))
        return html, False, 0

    i, j = html.find(start), html.find(end)
    if i < 0 or j < 0 or j < i:
        # 标记被删掉是很严重的事故：静默跳过会让站点悄悄退回「慢版本」，所以直接报错
        log("!! index.html 里找不到 %s / %s 标记，无法注入 %s" % (start, end, rel_json))
        return html, False, 2

    block = "%s\n%s\n%s" % (start, build_block(payload, script_id), end)
    new_html = html[:i] + block + html[j + len(end):]

    if new_html == html:
        log("%s 种子内容没变" % rel_json)
        return html, False, 0

    log("%s：已注入 %d 条（%d → %d 字节）" % (
        rel_json, len(payload.get(key) or []),
        len(html.encode("utf-8")), len(new_html.encode("utf-8"))))
    return new_html, True, 0


def main() -> int:
    check_only = "--check" in sys.argv[1:]

    if not os.path.exists(HTML_PATH):
        log("!! 找不到 index.html")
        return 2

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    original = html
    changed = False
    for ds in DATASETS:
        html, ch, rc = inject_one(html, *ds)
        if rc == 2:
            return 2
        changed = changed or ch

    if not changed:
        log("所有种子都是最新的，index.html 不动")
        return 0

    if check_only:
        log("需要更新（--check 模式，不写文件）")
        return 1

    with open(HTML_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(html)

    log("已写回 index.html：%d → %d 字节（+%d）" % (
        len(original.encode("utf-8")), len(html.encode("utf-8")),
        len(html.encode("utf-8")) - len(original.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
