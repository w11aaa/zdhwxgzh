from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import requests


@dataclass
class WechatArticle:
    url: str
    title: str
    content_text: str
    image_urls: List[str] = field(default_factory=list)
    cover_image_url: str = ""
    author: str = ""
    publish_time: str = ""


def is_wechat_mp_article_url(url: str) -> bool:
    u = str(url or "").strip()
    if not u:
        return False
    return "mp.weixin.qq.com/" in u and ("/s?" in u or "/s/" in u or u.rstrip("/").endswith("/s"))


def _default_headers(*, referer: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _normalize_image_url(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("data:"):
        return ""
    if s.startswith("//"):
        return "https:" + s
    return s


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in items:
        it = str(it or "").strip()
        if not it:
            continue
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def _cleanup_text(text: str) -> str:
    s = str(text or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Remove non-breaking spaces
    s = s.replace("\xa0", " ")
    # Collapse whitespace but keep newlines.
    s = re.sub(r"[ \t\f\v]+", " ", s)
    # Trim spaces around newlines
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    # Collapse excessive blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _extract_og_meta(html: str) -> Dict[str, str]:
    """Extract a few og:* meta tags without additional dependencies."""
    s = str(html or "")
    out: Dict[str, str] = {}

    def _pick(prop: str) -> str:
        m = re.search(
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            s,
            re.IGNORECASE,
        )
        return (m.group(1) if m else "").strip()

    for k in ("og:title", "og:image", "og:description", "og:site_name", "og:article:author"):
        v = _pick(k)
        if v:
            out[k] = v
    return out


class _WechatTitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_activity_name = False
        self._in_title_tag = False
        self.title_parts: List[str] = []
        self.fallback_title_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_map = {k: (v or "") for k, v in attrs}
        if tag.lower() == "h1" and attrs_map.get("id") == "activity-name":
            self._in_activity_name = True
        if tag.lower() == "title":
            self._in_title_tag = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1" and self._in_activity_name:
            self._in_activity_name = False
        if tag.lower() == "title" and self._in_title_tag:
            self._in_title_tag = False

    def handle_data(self, data: str) -> None:
        if self._in_activity_name:
            self.title_parts.append(data)
        elif self._in_title_tag:
            self.fallback_title_parts.append(data)


class _WechatContentParser(HTMLParser):
    _BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "blockquote",
    }
    _SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_content = False
        self._depth = 0
        self._skip_depth = 0
        self.text_parts: List[str] = []
        self.image_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs_map = {k: (v or "") for k, v in attrs}

        if not self._in_content:
            if t == "div" and attrs_map.get("id") == "js_content":
                self._in_content = True
                self._depth = 1
            return

        # Nested tag inside js_content
        self._depth += 1

        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth > 0:
            return

        if t == "img":
            src = (attrs_map.get("data-src") or attrs_map.get("src") or "").strip()
            if src:
                self.image_urls.append(src)

        if t == "br":
            self.text_parts.append("\n")
        elif t in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_content:
            return

        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if self._skip_depth == 0 and t in self._BLOCK_TAGS:
            self.text_parts.append("\n")

        self._depth -= 1
        if self._depth <= 0:
            self._in_content = False

    def handle_data(self, data: str) -> None:
        if not self._in_content:
            return
        if self._skip_depth > 0:
            return
        if data:
            self.text_parts.append(data)


def fetch_wechat_article(url: str, *, timeout_s: int = 25) -> WechatArticle:
    target_url = str(url or "").strip()
    if not target_url:
        raise ValueError("公众号链接为空")
    if not is_wechat_mp_article_url(target_url):
        raise ValueError("仅支持 mp.weixin.qq.com 的公众号文章链接（/s 或 /s?）")

    resp = requests.get(
        target_url,
        headers=_default_headers(),
        timeout=max(5, int(timeout_s or 0)),
        allow_redirects=True,
    )
    resp.raise_for_status()

    # WeChat pages are typically UTF-8; ensure we have usable text.
    if not resp.encoding:
        resp.encoding = "utf-8"
    html = resp.text or ""

    meta = _extract_og_meta(html)
    cover = _normalize_image_url(meta.get("og:image", ""))

    title = (meta.get("og:title") or "").strip()
    if not title:
        tp = _WechatTitleParser()
        tp.feed(html)
        title = _cleanup_text("".join(tp.title_parts))
        if not title:
            title = _cleanup_text("".join(tp.fallback_title_parts))

    cp = _WechatContentParser()
    cp.feed(html)

    content_text = _cleanup_text("".join(cp.text_parts))
    image_urls = [_normalize_image_url(u) for u in cp.image_urls]
    image_urls = [u for u in image_urls if u and u.startswith(("http://", "https://"))]
    image_urls = _dedupe_keep_order(image_urls)

    # Some articles don't embed cover in js_content; keep it as separate cover.
    return WechatArticle(
        url=target_url,
        title=title,
        content_text=content_text,
        image_urls=image_urls,
        cover_image_url=cover,
        author=(meta.get("og:article:author") or "").strip(),
    )

