#!/usr/bin/env python3
"""
通用网页链接导入解析测试
仅测试 HTML 解析逻辑（不发起网络请求）
"""

import os
import sys

import pytest

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.core.importers.webpage_article import parse_webpage_html


class TestWebpageImport:
    def test_parse_prefers_article_container(self):
        html = """
        <html>
          <head>
            <meta property="og:title" content="OG Title"/>
            <meta property="og:image" content="/cover.jpg"/>
            <title>Fallback Title</title>
          </head>
          <body>
            <div class="sidebar">
              这里是侧边栏内容，不应被当成正文。这里是侧边栏内容，不应被当成正文。
            </div>
            <main id="main-content">
              <article class="post-content">
                <h1>H1 Title</h1>
                <p>第一段正文。</p>
                <img src="/cover.jpg"/>
                <img data-src="/a.png"/>
                <p>第二段正文。</p>
              </article>
            </main>
          </body>
        </html>
        """

        parsed = parse_webpage_html(html, base_url="https://example.com/page")

        assert parsed["title"] == "OG Title"
        assert parsed["cover_image_url"] == "https://example.com/cover.jpg"
        assert parsed["image_urls"] == ["https://example.com/a.png"]
        assert "第一段正文" in parsed["content_text"]
        assert "侧边栏内容" not in parsed["content_text"]

    def test_parse_resolves_base_href_for_images(self):
        html = """
        <html>
          <head>
            <base href="https://cdn.example.com/assets/"/>
            <title>Only Title</title>
          </head>
          <body>
            <article class="content">
              <p>正文</p>
              <img src="img/a.png"/>
            </article>
          </body>
        </html>
        """

        parsed = parse_webpage_html(html, base_url="https://example.com/post")

        assert parsed["title"] == "Only Title"
        assert parsed["cover_image_url"] == "https://cdn.example.com/assets/img/a.png"
        assert parsed["image_urls"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

