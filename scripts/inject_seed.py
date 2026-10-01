#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 videos.json 的内容内联进 index.html 的 VIDEO_SEED 标记之间。

为什么需要这一步：
  站点靠 fetch('videos.json') 拿数据，意味着封面图必须等这一次网络往返回来、
  才知道自己的 URL。实测线上这条链是 TTFB 785ms → JSON 到 1110ms → 封面才开始下载，
  也就是 LCP 平白多等 325ms；而且「01 板块稍后才出现」还会引发整块重排（手机端 CLS 0.80）。
  把数据直接写进 HTML，首帧就能渲染出真卡片，这两件事一起解决。

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
JSON_PATH = os.path.join(ROOT, "videos.json")

START = "<!-- VIDEO_SEED_START -->"
END = "<!-- VIDEO_SEED_END -->"


def log(msg: str) -> None:
    print("[seed] %s" % msg, flush=True)


def build_block(payload: dict) -> str:
    """把数据序列化成一段可直接放进 <script type=application/json> 的文本。"""
    # separators 去掉多余空格；ensure_ascii=False 保留中文（HTML 本身是 UTF-8）
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    safe = raw.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return '%s\n<script type="application/json" id="videoSeed">%s</script>\n%s' % (START, safe, END)


def main() -> int:
    check_only = "--check" in sys.argv[1:]

    for p in (HTML_PATH, JSON_PATH):
        if not os.path.exists(p):
            log("!! 找不到 %s，跳过" % os.path.relpath(p, ROOT))
            return 0

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)

    videos = payload.get("videos") or []
    if not videos:
        log("!! videos.json 里没有 videos，跳过（保持页面上现有的种子不动）")
        return 0

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    i, j = html.find(START), html.find(END)
    if i < 0 or j < 0 or j < i:
        # 标记被删掉是很严重的事故：静默跳过会让站点悄悄退回「慢版本」，所以直接报错
        log("!! index.html 里找不到 VIDEO_SEED 标记，无法注入")
        return 2

    block = build_block(payload)
    new_html = html[:i] + block + html[j + len(END):]

    if new_html == html:
        log("种子内容没变，index.html 不动")
        return 0

    if check_only:
        log("需要更新（--check 模式，不写文件）")
        return 1

    with open(HTML_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(new_html)

    log("已注入 %d 条视频数据，index.html %d → %d 字节（+%d）" % (
        len(videos), len(html.encode("utf-8")), len(new_html.encode("utf-8")),
        len(new_html.encode("utf-8")) - len(html.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
