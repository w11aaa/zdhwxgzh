from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import time
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

warnings.filterwarnings("ignore", message=r".*urllib3.*chardet.*charset_normalizer.*")

import requests
from requests import Response
from requests import Session
from requests.exceptions import HTTPError

from .config import CONFIG
from .store import ContentStore


LIST_URL = "https://market-api.fenbi.com/toolkit/api/v1/pc/exam/queryByCondition"
DETAIL_URL = "https://market-api.fenbi.com/toolkit/api/v1/pc/exam/detail"
ARTICLE_HTML_URL = "https://hera-webapp.fenbi.com/api/website/article/detail"
DEVICE_URL = "https://login.fenbi.com/api/users/device/sid/create"
DETAIL_PAGE_URL = "https://www.fenbi.com/page/exam-information-detail/{article_id}"

EXAM_TYPE_ALIASES = {
    "省考": "1",
    "公务员": "1",
    "国考": "0",
    "军队文职": "2",
    "选调": "3",
    "选调生": "3",
    "事业单位": "4",
    "大学生村官": "5",
    "三支一扶": "6",
    "遴选": "7",
    "招警": "8",
    "国企": "9",
    "教师": "10",
    "医疗": "11",
    "银行": "12",
    "其他": "13",
    "农信社": "14",
    "派遣": "15",
    "联考": "16",
    "社区工作者": "17",
    "高校": "18",
}
FENBI_EXAM_TYPES = [
    ("1", "省考"),
    ("0", "国考"),
    ("2", "军队文职"),
    ("3", "选调"),
    ("4", "事业单位"),
    ("5", "大学生村官"),
    ("6", "三支一扶"),
    ("7", "遴选"),
    ("8", "招警"),
    ("9", "国企"),
    ("10", "教师"),
    ("11", "医疗"),
    ("12", "银行"),
    ("13", "其他"),
    ("14", "农信社"),
    ("15", "派遣/临时/购买服务等"),
    ("16", "联考/统考"),
    ("17", "社区工作者"),
    ("18", "高校"),
    ("19", "公务员单招"),
]
FENBI_EXAM_TYPE_NAMES = {type_id: name for type_id, name in FENBI_EXAM_TYPES}
FENBI_EXAM_TYPE_IDS = {name: type_id for type_id, name in FENBI_EXAM_TYPES}
ENROLL_STATUS = {
    1: "即将开始",
    2: "正在报名",
    3: "报名结束",
    4: "正在报名",
}
STATUS_ALIASES = {
    "upcoming": "即将开始",
    "即将报名": "即将开始",
    "即将开始": "即将开始",
    "open": "正在报名",
    "ending_soon": "正在报名",
    "正在报名": "正在报名",
    "报名进行中": "正在报名",
    "closed": "报名结束",
    "已结束": "报名结束",
    "结束报名": "报名结束",
    "报名结束": "报名结束",
}


@dataclass
class FenbiEvent:
    source_platform: str
    source_id: str
    title: str
    region: str
    category: str
    fenbi_exam_type_id: str
    fenbi_exam_type_name: str
    org_name: str
    job_count: int | None
    qualification: str
    major_requirements: str
    registration_start: str
    registration_deadline: str
    registration_deadline_time: str
    exam_date: str
    status: str
    publish_time: str
    source_url: str
    article_url: str
    source_origin_url: str
    source_origin_text: str
    source_origin_html: str
    origin_search_status: str
    origin_search_attempts: int
    origin_last_checked_at: str
    summary: str
    raw_text: str
    raw_json: str
    hash_id: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ms_to_iso(value: object) -> str:
    try:
        ms = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def _ms_to_date(value: object) -> str:
    iso = _ms_to_iso(value)
    return iso[:10] if iso else ""


def _ms_to_display_time(value: object) -> str:
    iso = _ms_to_iso(value)
    return iso[:16].replace("T", " ") if iso else ""


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value)
    m = re.search(r"\d+", text.replace(",", ""))
    if not m:
        return None
    return int(m.group(0))


def _html_to_text(html_text: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_text or "")
    return parser.text()


def _tag_name(article: dict[str, Any], tag_type: int) -> str:
    tags = article.get("tagsList") or []
    if not isinstance(tags, list):
        return ""
    for tag in tags:
        if isinstance(tag, dict) and tag.get("type") == tag_type:
            return _clean_text(tag.get("name"))
    return ""


def _official_url(detail: dict[str, Any]) -> str:
    for tool in detail.get("selectionTools") or []:
        if not isinstance(tool, dict):
            continue
        name = _clean_text(tool.get("name"))
        url = _clean_text(tool.get("url"))
        if url and ("官方公告" in name or url.startswith("http")):
            return url
    return ""


def _detail_summary(detail: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in detail.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = _clean_text(section.get("title"))
        data = section.get("data")
        if not title:
            continue
        if isinstance(data, dict):
            bits = []
            for key in ("examName", "recruitNum", "positionNum"):
                if data.get(key) not in (None, ""):
                    bits.append(f"{key}={data.get(key)}")
            if bits:
                lines.append(f"{title}: " + ", ".join(bits))
        elif isinstance(data, list) and data:
            lines.append(f"{title}: {len(data)}项")
    return "\n".join(lines)


def _build_raw_text(article: dict[str, Any], detail: dict[str, Any], article_html: str) -> str:
    pieces = [
        _clean_text(article.get("title")),
        _detail_summary(detail),
        _html_to_text(article_html),
    ]
    return "\n\n".join(piece for piece in pieces if piece).strip()


class FenbiClient:
    def __init__(self) -> None:
        self.session: Session = requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.fenbi.com",
            "Referer": "https://www.fenbi.com/page/exams-information-list?type=1",
        }
        self.public_params = {
            "app": "web",
            "av": "100",
            "hav": "100",
            "kav": "100",
            "gav": "2",
            "apcid": "0",
        }
        self._warmup()
        self.device_id = self._create_device_id()

    def _warmup(self) -> None:
        try:
            self.session.get(
                "https://www.fenbi.com/page/exams-information-list?type=1",
                headers={**self.headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                timeout=20,
            )
        except requests.RequestException:
            pass

    def _create_device_id(self) -> str:
        params = {
            **self.public_params,
            "client_context_id": str(int(time.time() * 1000)),
        }
        payload = {"pf": "web", "startupId": str(uuid.uuid4()), "extras": {}}
        response = self.session.post(DEVICE_URL, params=params, json=payload, headers=self.headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        device_id = (data.get("data") or {}).get("deviceId")
        if data.get("code") != 1 or not device_id:
            raise RuntimeError(f"粉笔 deviceId 创建失败: {data}")
        return str(device_id)

    def _params(self) -> dict[str, str]:
        return {
            **self.public_params,
            "client_context_id": str(int(time.time() * 1000)),
            "deviceId": self.device_id,
        }

    def _request_with_auth_retry(self, method: str, url: str, *, retries: int = 3, **kwargs: Any) -> Response:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            response = self.session.request(method, url, **kwargs)
            if response.status_code != 401:
                return response
            last_error = HTTPError(f"401 Unauthorized: {url}", response=response)
            if attempt < retries:
                print(f"[fenbi] 401 Unauthorized, refreshing deviceId and retrying ({attempt}/{retries - 1})...")
                self._warmup()
                self.device_id = self._create_device_id()
                params = dict(kwargs.get("params") or {})
                params.update(self._params())
                kwargs["params"] = params
                time.sleep(0.6 * attempt)
        if last_error:
            raise last_error
        raise RuntimeError(f"粉笔请求失败: {url}")

    def query(
        self,
        *,
        exam_type: str,
        year: str,
        start: int,
        length: int,
        enroll_status: str = "",
        recruit_num_code: str = "",
        district_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            "districtId": district_id or None,
            "examType": exam_type or None,
            "year": year or None,
            "enrollStatus": enroll_status or None,
            "recruitNumCode": recruit_num_code or None,
            "start": start,
            "len": length,
            "needTotal": start == 0,
        }
        response = self._request_with_auth_retry(
            "POST",
            LIST_URL,
            params=self._params(),
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 1:
            raise RuntimeError(f"粉笔列表接口返回异常: {data}")
        return data.get("data") or {}

    def detail(self, article_id: str) -> dict[str, Any]:
        response = self._request_with_auth_retry(
            "GET",
            DETAIL_URL,
            params={"articleId": article_id, **self._params()},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 1:
            raise RuntimeError(f"粉笔详情接口返回异常: {data}")
        return data.get("data") or {}

    def article_html(self, article_id: str) -> str:
        response = self._request_with_auth_retry(
            "GET",
            ARTICLE_HTML_URL,
            params={"deviceType": "3", "id": article_id, **self._params()},
            headers={**self.headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=30,
        )
        response.raise_for_status()
        return response.text


def _normalize_exam_type(value: str) -> str:
    value = _clean_text(value)
    if not value or value == "全部":
        return ""
    return EXAM_TYPE_ALIASES.get(value, FENBI_EXAM_TYPE_IDS.get(value, value))


def _article_to_event(article: dict[str, Any], detail: dict[str, Any], article_html: str) -> FenbiEvent:
    article_id = str(article.get("id") or "")
    info = article.get("announcementArticleInfoRet") or {}
    if not isinstance(info, dict):
        info = {}
    region = _tag_name(article, 1)
    category = _tag_name(article, 2)
    exam_type_id = str(article.get("examType") or "")
    exam_type_name = FENBI_EXAM_TYPE_NAMES.get(exam_type_id) or category
    title = _clean_text(article.get("title")) or f"粉笔公告 {article_id}"
    official_url = _official_url(detail)
    raw_text = _build_raw_text(article, detail, article_html)
    combined_raw = {
        "article": article,
        "detail": detail,
        "article_html_length": len(article_html or ""),
    }
    return FenbiEvent(
        source_platform="fenbi",
        source_id=article_id,
        title=title,
        region=region,
        category=category,
        fenbi_exam_type_id=exam_type_id,
        fenbi_exam_type_name=exam_type_name,
        org_name=title,
        job_count=_to_int(info.get("recruitNumRet") or (detail.get("sections") or [{}])[-1].get("recruitNum") if detail.get("sections") else None),
        qualification="",
        major_requirements="",
        registration_start=_ms_to_date(info.get("enrollStartTime")),
        registration_deadline=_ms_to_date(info.get("enrollEndTime")),
        registration_deadline_time=_ms_to_display_time(info.get("enrollEndTime")),
        exam_date="",
        status=ENROLL_STATUS.get(int(info.get("enrollStatus") or 0), "unknown"),
        publish_time=_ms_to_iso(article.get("issueTime")),
        source_url=DETAIL_PAGE_URL.format(article_id=article_id),
        article_url=official_url,
        source_origin_url=official_url,
        source_origin_text=_html_to_text(article_html),
        source_origin_html=article_html,
        origin_search_status="found" if official_url else "pending",
        origin_search_attempts=1 if official_url else 0,
        origin_last_checked_at=datetime.utcnow().isoformat() if official_url else "",
        summary=_detail_summary(detail),
        raw_text=raw_text,
        raw_json=json.dumps(combined_raw, ensure_ascii=False),
        hash_id=hashlib.md5(f"fenbi|{article_id}".encode("utf-8")).hexdigest(),
    )


def crawl_fenbi(
    *,
    max_items: int,
    page: int,
    exam_type: str,
    year: str,
    enroll_status: str = "",
    recruit_num_code: str = "",
    district_id: str = "",
    client: FenbiClient | None = None,
) -> list[FenbiEvent]:
    client = client or FenbiClient()
    events: list[FenbiEvent] = []
    page_size = min(max(max_items, 1), 50)
    start = max(page - 1, 0) * page_size
    while len(events) < max_items:
        batch_len = min(page_size, max_items - len(events))
        data = client.query(
            exam_type=exam_type,
            year=year,
            start=start,
            length=batch_len,
            enroll_status=enroll_status,
            recruit_num_code=recruit_num_code,
            district_id=district_id,
        )
        articles = data.get("articles") or []
        if not articles:
            break
        for article in articles:
            if not isinstance(article, dict):
                continue
            article_id = str(article.get("id") or "")
            if not article_id:
                continue
            try:
                detail = client.detail(article_id)
            except Exception as exc:
                print(f"[fenbi] detail failed article_id={article_id}: {exc}")
                detail = {}
            try:
                article_html = client.article_html(article_id)
            except Exception as exc:
                print(f"[fenbi] article html failed article_id={article_id}: {exc}")
                article_html = ""
            events.append(_article_to_event(article, detail, article_html))
            if len(events) >= max_items:
                break
        start += len(articles)
        if len(articles) < batch_len:
            break
    return events


def repair_fenbi_exam_types(db_path: str | None = None) -> int:
    path = db_path or str(CONFIG.database_path)
    store = ContentStore(CONFIG.database_path)
    del store
    updated = 0
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, source_platform, category, raw_json
            FROM gongkao_events
            WHERE coalesce(fenbi_exam_type_id, '') = '' OR coalesce(fenbi_exam_type_name, '') = ''
            """
        ).fetchall()
        for row in rows:
            exam_type_id = ""
            exam_type_name = ""
            if row["source_platform"] == "fenbi":
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                    article = raw.get("article") if isinstance(raw, dict) else {}
                    if isinstance(article, dict):
                        exam_type_id = str(article.get("examType") or "")
                except Exception:
                    exam_type_id = ""
            if not exam_type_id:
                category = _clean_text(row["category"])
                exam_type_id = _normalize_exam_type(category)
            exam_type_name = FENBI_EXAM_TYPE_NAMES.get(exam_type_id) or _clean_text(row["category"])
            if exam_type_id or exam_type_name:
                conn.execute(
                    """
                    UPDATE gongkao_events
                    SET fenbi_exam_type_id = ?, fenbi_exam_type_name = ?
                    WHERE id = ?
                    """,
                    (exam_type_id, exam_type_name, row["id"]),
                )
                updated += 1
        conn.commit()
    return updated


def repair_registration_statuses(db_path: str | None = None) -> int:
    path = db_path or str(CONFIG.database_path)
    ContentStore(CONFIG.database_path)
    updated = 0
    today = datetime.now().date()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, status, registration_start, registration_deadline
            FROM gongkao_events
            """
        ).fetchall()
        for row in rows:
            old_status = _clean_text(row["status"])
            new_status = ""
            start = _clean_text(row["registration_start"])
            deadline = _clean_text(row["registration_deadline"])
            try:
                if deadline and datetime.strptime(deadline, "%Y-%m-%d").date() < today:
                    new_status = "报名结束"
            except Exception:
                pass
            if not new_status:
                try:
                    if start and datetime.strptime(start, "%Y-%m-%d").date() > today:
                        new_status = "即将开始"
                except Exception:
                    pass
            if not new_status and deadline:
                new_status = "正在报名"
            if not new_status:
                new_status = STATUS_ALIASES.get(old_status, "")
            if new_status and new_status != old_status:
                conn.execute("UPDATE gongkao_events SET status = ? WHERE id = ?", (new_status, row["id"]))
                updated += 1
        conn.commit()
    return updated


def repair_deadline_times(db_path: str | None = None) -> int:
    path = db_path or str(CONFIG.database_path)
    ContentStore(CONFIG.database_path)
    updated = 0
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, source_platform, registration_deadline, registration_deadline_time, raw_json
            FROM gongkao_events
            WHERE coalesce(registration_deadline_time, '') = ''
            """
        ).fetchall()
        for row in rows:
            deadline_time = ""
            if row["source_platform"] == "fenbi":
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                    article = raw.get("article") if isinstance(raw, dict) else {}
                    info = article.get("announcementArticleInfoRet") if isinstance(article, dict) else {}
                    if isinstance(info, dict):
                        deadline_time = _ms_to_display_time(info.get("enrollEndTime"))
                except Exception:
                    deadline_time = ""
            if not deadline_time and row["registration_deadline"]:
                deadline_time = f"{row['registration_deadline']} 23:59"
            if deadline_time:
                conn.execute(
                    "UPDATE gongkao_events SET registration_deadline_time = ? WHERE id = ?",
                    (deadline_time, row["id"]),
                )
                updated += 1
        conn.commit()
    return updated


def crawl_active_by_type(*, year: str, max_items_per_status: int) -> int:
    client = FenbiClient()
    store = ContentStore(CONFIG.database_path)
    statuses = [("1", "即将开始"), ("2", "正在报名")]
    total_steps = len(FENBI_EXAM_TYPES) * len(statuses)
    step = 0
    imported_total = 0
    print(f"PROGRESS 0/{total_steps} 准备按粉笔考试类型补全有效公告", flush=True)
    for exam_type_id, exam_type_name in FENBI_EXAM_TYPES:
        for status_id, status_name in statuses:
            step += 1
            print(
                f"PROGRESS {step}/{total_steps} 正在采集：{exam_type_name} / {status_name} / 全国",
                flush=True,
            )
            try:
                events = crawl_fenbi(
                    max_items=max_items_per_status,
                    page=1,
                    exam_type=exam_type_id,
                    year=year,
                    enroll_status=status_id,
                    district_id="",
                    client=client,
                )
                for event in events:
                    if not event.fenbi_exam_type_id:
                        event.fenbi_exam_type_id = exam_type_id
                    if not event.fenbi_exam_type_name:
                        event.fenbi_exam_type_name = exam_type_name
                    store.upsert_gongkao_event(event.__dict__)
                imported_total += len(events)
                print(f"[fenbi] {exam_type_name}/{status_name}: imported {len(events)} items", flush=True)
            except Exception as exc:
                print(f"[fenbi] {exam_type_name}/{status_name}: failed: {exc}", flush=True)
    repaired = repair_fenbi_exam_types()
    repaired_statuses = repair_registration_statuses()
    repaired_deadline_times = repair_deadline_times()
    print(f"PROGRESS {total_steps}/{total_steps} 补全完成", flush=True)
    print(
        f"[fenbi] active backfill imported {imported_total} items, repaired {repaired} exam types, "
        f"normalized {repaired_statuses} statuses, repaired {repaired_deadline_times} deadline times",
        flush=True,
    )
    return imported_total


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl Fenbi exam announcements into gongkao_events.")
    parser.add_argument("--category", default="", help="Exam category label or Fenbi type id, e.g. 事业单位/公务员/国企/1/4.")
    parser.add_argument("--type", dest="exam_type", default="", help="Fenbi exam type id. Overrides --category.")
    parser.add_argument("--year", default=str(datetime.now().year), help="Exam year, e.g. 2026.")
    parser.add_argument("--enroll_status", default="", help="Fenbi enroll status id: 1=即将开始, 2=正在报名, 3=报名结束.")
    parser.add_argument("--recruit_num_code", default="", help="Fenbi recruit range id.")
    parser.add_argument("--district_id", default="", help="Fenbi district id. Empty means nationwide.")
    parser.add_argument("--max_items", type=int, default=20)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--backfill_active", action="store_true", help="Crawl every Fenbi exam type for upcoming/open nationwide announcements.")
    parser.add_argument("--repair_exam_types", action="store_true", help="Fill fenbi exam type columns for existing records.")
    parser.add_argument("--repair_statuses", action="store_true", help="Normalize registration statuses to 即将开始/正在报名/报名结束.")
    parser.add_argument("--repair_deadline_times", action="store_true", help="Fill registration_deadline_time for existing records.")
    args = parser.parse_args()

    if args.repair_exam_types:
        repaired = repair_fenbi_exam_types()
        repaired_statuses = repair_registration_statuses()
        repaired_deadline_times = repair_deadline_times()
        print(
            f"[fenbi] repaired {repaired} exam types, normalized {repaired_statuses} statuses, "
            f"repaired {repaired_deadline_times} deadline times"
        )
        return

    if args.repair_statuses:
        repaired_statuses = repair_registration_statuses()
        print(f"[fenbi] normalized {repaired_statuses} statuses")
        return

    if args.repair_deadline_times:
        repaired_deadline_times = repair_deadline_times()
        print(f"[fenbi] repaired {repaired_deadline_times} deadline times")
        return

    if args.backfill_active:
        crawl_active_by_type(year=args.year, max_items_per_status=max(1, args.max_items))
        return

    exam_type = _normalize_exam_type(args.exam_type or args.category)
    events = crawl_fenbi(
        max_items=args.max_items,
        page=args.page,
        exam_type=exam_type,
        year=args.year,
        enroll_status=args.enroll_status,
        recruit_num_code=args.recruit_num_code,
        district_id=args.district_id,
    )
    store = ContentStore(CONFIG.database_path)
    for event in events:
        store.upsert_gongkao_event(event.__dict__)
    print(f"[fenbi] imported {len(events)} items into {CONFIG.database_path}")


if __name__ == "__main__":
    main()
