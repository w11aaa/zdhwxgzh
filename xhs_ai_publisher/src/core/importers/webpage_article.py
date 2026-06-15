from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests


@dataclass
class WebpageArticle:
    url: str
    title: str
    content_text: str
    image_urls: List[str] = field(default_factory=list)
    cover_image_url: str = ""
    author: str = ""
    publish_time: str = ""


def is_http_url(url: str) -> bool:
    u = str(url or "").strip()
    if not u:
        return False
    try:
        parsed = urlparse(u)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


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


def _normalize_url(raw: str, *, base_url: str = "") -> str:
    s = str(raw or "").strip()
    if not s:
        return ""

    low = s.lower()
    if low.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return ""

    if s.startswith("//"):
        s = "https:" + s

    # If it's relative, join with base_url
    if base_url:
        try:
            s = urljoin(base_url, s)
        except Exception:
            pass

    s = s.strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        return ""
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
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class _MetaAndTitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.base_href: str = ""
        self.canonical: str = ""

        self._in_title = False
        self.title_parts: List[str] = []

        self._in_h1 = False
        self.h1_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs_map = {str(k).lower(): (v or "") for k, v in attrs}

        if t == "base":
            href = (attrs_map.get("href") or "").strip()
            if href and not self.base_href:
                self.base_href = href
            return

        if t == "link":
            rel = (attrs_map.get("rel") or "").lower()
            href = (attrs_map.get("href") or "").strip()
            if href and "canonical" in rel and not self.canonical:
                self.canonical = href
            return

        if t == "meta":
            key = (attrs_map.get("property") or attrs_map.get("name") or "").strip()
            content = (attrs_map.get("content") or "").strip()
            if key and content:
                self.meta[key.lower()] = content
            return

        if t == "title":
            self._in_title = True
            return

        if t == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title" and self._in_title:
            self._in_title = False
        if t == "h1" and self._in_h1:
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)


@dataclass
class _Container:
    tag: str
    start_depth: int
    weight: int = 0
    text_parts: List[str] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)

    def finalize_text(self) -> str:
        return _cleanup_text("".join(self.text_parts))


class _GenericContentParser(HTMLParser):
    _BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "main",
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
        "pre",
    }
    _SKIP_TAGS = {"script", "style", "noscript"}
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _CONTAINER_TAGS = {"article", "main", "section", "div"}

    _POSITIVE_HINTS = {
        "content",
        "article",
        "post",
        "entry",
        "main",
        "detail",
        "body",
        "text",
        "rich",
        "markdown",
        "story",
        "news",
    }
    _NEGATIVE_HINTS = {
        "nav",
        "footer",
        "header",
        "comment",
        "sidebar",
        "aside",
        "advert",
        "ads",
        "recommend",
        "related",
        "breadcrumb",
        "share",
        "social",
        "toolbar",
        "menu",
        "pagination",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self._skip_depth = 0

        self.root = _Container(tag="__root__", start_depth=0, weight=0)
        self._open: List[_Container] = [self.root]
        self.closed: List[_Container] = []

    def _calc_weight(self, tag: str, attrs_map: Dict[str, str]) -> int:
        weight = 0
        if tag in {"article", "main"}:
            weight += 6

        role = (attrs_map.get("role") or "").lower()
        if role == "main":
            weight += 6

        hint = " ".join(
            [
                str(attrs_map.get("id") or ""),
                str(attrs_map.get("class") or ""),
                str(attrs_map.get("itemprop") or ""),
            ]
        ).lower()

        pos_hits = sum(1 for k in self._POSITIVE_HINTS if k in hint)
        neg_hits = sum(1 for k in self._NEGATIVE_HINTS if k in hint)

        weight += pos_hits * 3
        weight -= neg_hits * 5
        return weight

    def _append_to_open(self, s: str) -> None:
        for c in self._open:
            c.text_parts.append(s)

    def _append_img_to_open(self, u: str) -> None:
        for c in self._open:
            c.image_urls.append(u)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t not in self._VOID_TAGS:
            self.depth += 1
        attrs_map = {str(k).lower(): (v or "") for k, v in attrs}

        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth > 0:
            return

        if t in self._BLOCK_TAGS or t == "br":
            self._append_to_open("\n")

        if t == "img":
            raw = (
                (attrs_map.get("data-src") or "")
                or (attrs_map.get("data-original") or "")
                or (attrs_map.get("data-url") or "")
                or (attrs_map.get("data-lazy-src") or "")
                or (attrs_map.get("data-actualsrc") or "")
                or (attrs_map.get("src") or "")
            ).strip()
            if raw:
                self._append_img_to_open(raw)

        if t in self._CONTAINER_TAGS:
            weight = self._calc_weight(t, attrs_map)
            self._open.append(_Container(tag=t, start_depth=self.depth, weight=weight))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()

        # Void elements don't have a real end tag in HTML. HTMLParser may call
        # handle_endtag for XHTML-style <img/>; keep depth unchanged.
        if t in self._VOID_TAGS:
            return

        if self._skip_depth > 0:
            if t in self._SKIP_TAGS:
                self._skip_depth -= 1
            self.depth = max(0, self.depth - 1)
            return

        if t in self._BLOCK_TAGS:
            self._append_to_open("\n")

        if (
            len(self._open) > 1
            and self._open[-1].tag == t
            and self._open[-1].start_depth == self.depth
        ):
            self.closed.append(self._open.pop())

        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data:
            self._append_to_open(data)


def parse_webpage_html(html: str, *, base_url: str = "") -> Dict[str, object]:
    s = str(html or "")

    meta_parser = _MetaAndTitleParser()
    try:
        meta_parser.feed(s)
    except Exception:
        pass

    resolved_base = str(base_url or "").strip()
    if meta_parser.base_href:
        resolved_base = urljoin(resolved_base or base_url or "", meta_parser.base_href)
    elif meta_parser.canonical and resolved_base:
        # Canonical can help for relative image URLs
        resolved_base = urljoin(resolved_base, meta_parser.canonical)

    meta = meta_parser.meta

    title = (
        (meta.get("og:title") or "").strip()
        or (meta.get("twitter:title") or "").strip()
        or (meta.get("title") or "").strip()
    )
    if not title:
        h1 = _cleanup_text("".join(meta_parser.h1_parts))
        if h1:
            title = h1
        else:
            title = _cleanup_text("".join(meta_parser.title_parts))

    cover = (
        (meta.get("og:image") or "").strip()
        or (meta.get("twitter:image") or "").strip()
        or (meta.get("twitter:image:src") or "").strip()
    )

    author = (
        (meta.get("author") or "").strip()
        or (meta.get("article:author") or "").strip()
        or (meta.get("og:article:author") or "").strip()
    )
    publish_time = (
        (meta.get("article:published_time") or "").strip()
        or (meta.get("og:article:published_time") or "").strip()
        or (meta.get("pubdate") or "").strip()
        or (meta.get("publishdate") or "").strip()
        or (meta.get("date") or "").strip()
    )

    content_parser = _GenericContentParser()
    try:
        content_parser.feed(s)
    except Exception:
        pass

    containers = list(content_parser.closed) + [content_parser.root]

    best = content_parser.root
    best_score = -1
    for c in containers:
        text = c.finalize_text()
        if not text:
            continue
        # Score: text length dominates, but hint-weight can flip close calls.
        score = len(text) + (c.weight * 200)
        if score > best_score:
            best_score = score
            best = c

    content_text = best.finalize_text()
    raw_images = list(best.image_urls or [])

    image_urls = [_normalize_url(u, base_url=resolved_base or base_url or "") for u in raw_images]
    image_urls = [u for u in image_urls if u]
    image_urls = _dedupe_keep_order(image_urls)

    cover_url = _normalize_url(cover, base_url=resolved_base or base_url or "")
    if not cover_url and image_urls:
        cover_url = image_urls[0]

    # Avoid duplicating cover in content images
    if cover_url:
        image_urls = [u for u in image_urls if u != cover_url]

    return {
        "title": title,
        "content_text": content_text,
        "image_urls": image_urls,
        "cover_image_url": cover_url,
        "author": author,
        "publish_time": publish_time,
        "base_url": resolved_base or base_url,
    }


def fetch_webpage_article(url: str, *, timeout_s: int = 25) -> WebpageArticle:
    target_url = str(url or "").strip()
    if not target_url:
        raise ValueError("链接为空")
    if not is_http_url(target_url):
        raise ValueError("仅支持 http/https 网页链接")

    resp = requests.get(
        target_url,
        headers=_default_headers(),
        timeout=max(5, int(timeout_s or 0)),
        allow_redirects=True,
    )
    resp.raise_for_status()

    if not resp.encoding:
        try:
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception:
            resp.encoding = "utf-8"

    html = resp.text or ""
    parsed = parse_webpage_html(html, base_url=resp.url or target_url)

    return WebpageArticle(
        url=str(resp.url or target_url),
        title=str(parsed.get("title") or "").strip(),
        content_text=str(parsed.get("content_text") or "").strip(),
        image_urls=list(parsed.get("image_urls") or []),
        cover_image_url=str(parsed.get("cover_image_url") or "").strip(),
        author=str(parsed.get("author") or "").strip(),
        publish_time=str(parsed.get("publish_time") or "").strip(),
    )
