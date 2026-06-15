#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备用内容生成器 - 当主API不可用时的备选方案
"""

import json
import random
import re
import time
import os
import uuid
from PyQt5.QtCore import QThread, pyqtSignal
from src.core.services.system_image_template_service import system_image_template_service


class BackupContentGenerator(QThread):
    """备用内容生成器"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, input_text, header_title, author, generate_btn):
        super().__init__()
        self.input_text = input_text
        self.header_title = header_title
        self.author = author
        self.generate_btn = generate_btn
        self.info_reason = ""

    def run(self):
        """生成备用内容"""
        try:
            print("🔄 主API不可用，使用备用内容生成器...")
            
            # 更新按钮状态
            self.generate_btn.setText("⏳ 本地生成中...")
            self.generate_btn.setEnabled(False)

            # 基于输入内容生成标题和内容（尽量偏小红书风格：短句分段、可直接发布）
            title = self._generate_title()
            content, content_pages = self._generate_content_and_pages()
            
            # 优先使用系统模板图片生成（如 x-auto-publisher）；失败则回退到本地占位图
            cover_image = ""
            content_images = []
            try:
                generated = system_image_template_service.generate_post_images(
                    title=title,
                    content=content,
                    content_pages=content_pages,
                    page_count=max(1, len(content_pages)),
                )
                if generated:
                    cover_image, content_images = generated
            except Exception:
                cover_image = ""
                content_images = []

            if not cover_image or not content_images:
                # 生成本地占位图片（离线可用，避免外部图片服务不稳定）
                cover_image, content_images = self._generate_local_placeholder_images(title, count=random.randint(2, 4))

            result = {
                'title': title,
                'content': content,
                'cover_image': cover_image,
                'content_images': content_images,
                'content_pages': content_pages,
                'input_text': self.input_text,
                'generator': 'backup',
                'info_reason': self.info_reason or ''
            }

            print(f"✅ 备用内容生成成功: {title}")
            self.finished.emit(result)

        except Exception as e:
            error_msg = f"备用内容生成失败: {str(e)}"
            print(f"❌ {error_msg}")
            self.error.emit(error_msg)
        finally:
            # 恢复按钮状态
            self.generate_btn.setText("✨ 生成内容")
            self.generate_btn.setEnabled(True)

    def _generate_title(self):
        """生成标题"""
        if not self.header_title:
            self.header_title = "精彩分享"
        
        # 基于输入内容的关键词生成标题
        keywords = self.input_text.split()[:3]  # 取前3个词作为关键词
        
        base = "".join(keywords) if keywords else str(self.input_text or "").strip()
        base = re.sub(r"\s+", "", base)
        base = base[:10] if base else "这个话题"

        title_templates = [
            f"{base}真的有用吗 先看这3点",
            f"{base}别再踩坑 这份清单够用",
            f"{base}新手必看 3步就能上手",
            f"{base}想提升 先把这件事做对",
            f"{base}怎么做更稳 关键在这里",
        ]

        title = random.choice(title_templates)
        # 尽量控制在 15-20 字（中文按字符计）
        title = title[:20]
        if len(title) < 15:
            title = (title + "实用版").strip()[:20]
        return title

    def _generate_content_and_pages(self):
        """生成更适合小红书的分段内容 + 图片分页。"""
        topic = str(self.input_text or "").strip() or "这个话题"

        tips = [
            f"先把目标说清楚：你想从{topic}得到什么结果",
            "不要一上来就堆信息，先抓住最关键的 1-2 个点",
            "把能坚持的动作做成日常，比一次性爆发更有效",
        ]
        actions = [
            "今天就开始：写下你的现状和一个可执行的小目标",
            "用 7 天做一次复盘：哪里有效，哪里需要调整",
            "只保留最有效的 2 个习惯，其它先放一放",
        ]

        tags = [topic, "干货", "实用", "方法"]
        tags = [t for t in tags if t]
        # 去重保序
        seen = set()
        uniq = []
        for t in tags:
            t = re.sub(r"\s+", "", str(t))
            if not t or t in seen:
                continue
            seen.add(t)
            uniq.append(t)
        uniq = uniq[:10]

        tags_line = " ".join([f"#{t}" for t in uniq]).strip()

        content = "\n\n".join(
            [
                f"关于{topic}，我整理了一个更好上手的思路：",
                "先看重点：\n" + "\n".join([f"{i+1}. {x}" for i, x in enumerate(tips)]),
                "你可以这样做：\n" + "\n".join([f"{i+1}. {x}" for i, x in enumerate(actions)]),
                tags_line,
            ]
        ).strip()

        pages = [
            f"# 先看重点\n\n" + "\n\n".join(tips),
            f"# 你可以这样做\n\n" + "\n\n".join(actions),
            f"# 话题标签\n\n" + tags_line,
        ]
        pages = [p for p in pages if str(p).strip()]
        return content, pages

    def _generate_placeholder_image(self, title):
        """生成占位图片URL"""
        # 使用占位图服务
        width = random.randint(400, 800)
        height = random.randint(400, 600)
        
        # 使用更可靠的占位图服务（移除有SSL问题的via.placeholder.com）
        placeholder_services = [
            f"https://picsum.photos/{width}/{height}?random={random.randint(1, 1000)}",
            f"https://dummyimage.com/{width}x{height}/4ECDC4/FFFFFF&text={title}",
            f"https://placehold.co/{width}x{height}/png?text={title}"
        ]
        
        return random.choice(placeholder_services) 

    def _generate_local_placeholder_images(self, title: str, count: int = 3):
        """生成本地占位图片，避免依赖外部图片服务。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as e:
            raise Exception(f"Pillow 未安装或不可用: {e}")

        base_dir = os.path.join(os.path.expanduser('~'), '.xhs_system', 'generated_imgs')
        os.makedirs(base_dir, exist_ok=True)

        def _make(path: str, label: str):
            width, height = 1080, 1440
            img = Image.new('RGB', (width, height), (245, 245, 245))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            text = f"{label}\n{(title or '').strip()[:40]}"
            draw.multiline_text((60, 80), text, fill=(30, 30, 30), font=font, spacing=10)
            img.save(path, format='JPEG', quality=90)

        unique = uuid.uuid4().hex[:8]
        ts = int(time.time())
        cover_path = os.path.join(base_dir, f'cover_{ts}_{unique}.jpg')
        _make(cover_path, "封面")

        content_paths = []
        for i in range(max(1, int(count))):
            p = os.path.join(base_dir, f'content_{i+1}_{ts}_{unique}.jpg')
            _make(p, f"内容图{i+1}")
            content_paths.append(p)

        return cover_path, content_paths
