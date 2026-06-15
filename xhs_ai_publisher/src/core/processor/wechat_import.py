from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal

from src.core.importers.wechat_article import fetch_wechat_article, is_wechat_mp_article_url
from src.core.importers.webpage_article import fetch_webpage_article


class WechatArticleImportThread(QThread):
    """Fetch an article from a URL and convert to XHS draft fields.

    Supports:
    - WeChat official account articles (mp.weixin.qq.com/s/...)
    - Generic web pages (best effort)
    """

    finished = pyqtSignal(dict)  # {title, content, image_urls, ...}
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, url: str, *, max_images: int = 9):
        super().__init__()
        self.url = str(url or "").strip()
        try:
            self.max_images = int(max_images or 9)
        except Exception:
            self.max_images = 9
        self.max_images = max(0, min(50, self.max_images))

    def run(self) -> None:
        try:
            if is_wechat_mp_article_url(self.url):
                self.progress.emit("正在抓取公众号文章...")
                article = fetch_wechat_article(self.url)
            else:
                self.progress.emit("正在抓取网页内容...")
                article = fetch_webpage_article(self.url)
            self.progress.emit("正在解析正文与图片...")

            images = []
            cover = str(article.cover_image_url or "").strip()
            if cover:
                images.append(cover)
            images.extend(list(article.image_urls or []))

            # De-dupe while keeping order
            seen = set()
            deduped = []
            for u in images:
                u = str(u or "").strip()
                if not u:
                    continue
                if u in seen:
                    continue
                seen.add(u)
                deduped.append(u)
            images = deduped

            if self.max_images > 0:
                images = images[: self.max_images]

            self.finished.emit(
                {
                    "url": article.url,
                    "title": article.title,
                    "content": article.content_text,
                    "author": article.author,
                    "publish_time": article.publish_time,
                    "image_urls": images,
                    "cover_image_url": cover,
                }
            )
        except Exception as e:
            self.error.emit(str(e))
