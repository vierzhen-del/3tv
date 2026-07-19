"""노션 아카이브 (선택 기능 — 기존 v28 파이프라인 계승).

settings.yaml의 notion.enabled=true + NOTION_API_KEY/NOTION_PARENT_ID 등록 시
리포트를 노션 페이지로 저장한다.
"""
from __future__ import annotations

import requests

from .common import env_token, log, now_kst

NOTION_VERSION = "2022-06-28"


def _md_to_blocks(markdown: str) -> list[dict]:
    """마크다운을 노션 블록으로 단순 변환 (헤더/불릿/문단)."""
    blocks: list[dict] = []
    for raw in markdown.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        text = line.lstrip("#-* ").strip()
        if not text:
            continue
        rich = [{"type": "text", "text": {"content": text[:2000]}}]
        if line.startswith("### "):
            blocks.append({"heading_3": {"rich_text": rich}})
        elif line.startswith("## "):
            blocks.append({"heading_2": {"rich_text": rich}})
        elif line.startswith("# "):
            blocks.append({"heading_1": {"rich_text": rich}})
        elif line.lstrip().startswith(("- ", "* ")):
            blocks.append({"bulleted_list_item": {"rich_text": rich}})
        else:
            blocks.append({"paragraph": {"rich_text": rich}})
    # 노션 API 1회 요청 블록 한도(100)
    return [{"object": "block", "type": next(iter(b)), **b} for b in blocks[:95]]


def archive_to_notion(title_keyword: str, markdown_report: str) -> bool:
    api_key = env_token("NOTION_API_KEY")
    parent_id = env_token("NOTION_PARENT_ID")
    if not api_key or not parent_id:
        log.info("NOTION_API_KEY/NOTION_PARENT_ID 미설정 — 노션 저장 생략")
        return False

    title = f"3protv오늘_{now_kst():%Y%m%d}_{title_keyword}"
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            },
            "children": _md_to_blocks(markdown_report),
        },
        timeout=60,
    )
    if resp.status_code != 200:
        log.error("노션 저장 실패 %d: %s", resp.status_code, resp.text[:300])
        return False
    log.info("노션 저장 완료: %s", title)
    return True
