"""옵시디안 second brain 볼트(GitHub repo)에 리포트 마크다운 저장.

- 볼트 repo를 얕게 clone → 3protv/YYYY/MM/3protv오늘_YYYYMMDD_키워드.md 작성 → 커밋·push
- us 세션이 먼저 파일을 만들고, kr 세션이 같은 날짜 파일에 한국 섹션을 병합
- S26/탭S9의 Obsidian Git 플러그인이 pull 하여 기기에 반영됨
"""
from __future__ import annotations

import glob as globmod
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .common import REPO_ROOT, env_token, log, now_kst

VAULT_TMP = REPO_ROOT / ".vault_tmp"

US_MARKER = "<!-- 3tv:us -->"
KR_MARKER = "<!-- 3tv:kr -->"

# 기사검색 별도 노트(3protv기사_YYYYMMDD.md)의 세션별 섹션 헤더
US_NEWS_HEAD = "## 🇺🇸 미장 종목 기사"
KR_NEWS_HEAD = "## 🇰🇷 한국 종목 기사"


def _vault_url(vault_repo: str) -> str:
    pat = env_token("GH_PAT")
    if pat:
        return f"https://x-access-token:{pat}@github.com/{vault_repo}.git"
    return f"https://github.com/{vault_repo}.git"


def _clone_vault(obsidian_cfg: dict) -> Path | None:
    vault_repo = (obsidian_cfg.get("vault_repo") or "").strip()
    if not obsidian_cfg.get("enabled") or not vault_repo:
        log.info("옵시디안 아카이브 비활성 (vault_repo 미설정)")
        return None
    branch = obsidian_cfg.get("vault_branch", "main")
    if VAULT_TMP.exists():
        shutil.rmtree(VAULT_TMP)
    url = _vault_url(vault_repo)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(VAULT_TMP)],
            check=True, capture_output=True, timeout=300,
        )
        return VAULT_TMP
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode()
        if "Remote branch" in stderr and "not found" in stderr:
            # 커밋이 하나도 없는 완전히 빈 저장소 — 로컬에서 새로 초기화해
            # 최초 커밋·push 시 branch가 원격에 생성되도록 한다
            log.info("볼트 repo가 비어있음 — 최초 커밋으로 %s 브랜치를 새로 만듦", branch)
            VAULT_TMP.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-b", branch, str(VAULT_TMP)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(VAULT_TMP), "remote", "add", "origin", url],
                           check=True, capture_output=True)
            return VAULT_TMP
        log.error("볼트 clone 실패: %s", stderr[-300:])
        return None


def _today_file(vault: Path, base_path: str, keyword: str, date: datetime) -> Path:
    """오늘 날짜 파일 경로. 이미 있으면(us 세션이 만든) 기존 파일 재사용."""
    ymd = date.strftime("%Y%m%d")
    month_dir = vault / base_path / date.strftime("%Y") / date.strftime("%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(month_dir.glob(f"3protv오늘_{ymd}*.md"))
    if existing:
        return existing[0]
    safe_kw = "".join(c for c in keyword if c.isalnum() or c in "가-힣_-")[:20]
    return month_dir / f"3protv오늘_{ymd}_{safe_kw or '시황'}.md"


def _news_file(vault: Path, base_path: str, date: datetime) -> Path:
    """기사검색 리포트 파일 경로 — 시황 파일과 분리해 본문이 잠식되지 않게 한다."""
    month_dir = vault / base_path / date.strftime("%Y") / date.strftime("%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    return month_dir / f"{news_note_name(date)}.md"


def news_note_name(date: datetime) -> str:
    return f"3protv기사_{date.strftime('%Y%m%d')}"


def obsidian_deeplink(obsidian_cfg: dict, date: datetime | None = None) -> str:
    """탭S9/S26에서 탭하면 옵시디안이 열리는 딥링크.

    파일명에는 그날 키워드가 붙어 전송 시점엔 확정되지 않으므로(us 세션이 만든
    파일을 kr이 재사용) **검색 딥링크**를 쓴다 — 날짜만으로 항상 맞는다.
    """
    vault = (obsidian_cfg or {}).get("vault_name", "").strip()
    if not vault:
        return ""
    ymd = (date or now_kst()).strftime("%Y%m%d")
    return (f"obsidian://search?vault={quote(vault)}"
            f"&query={quote(f'3protv오늘_{ymd}')}")


def _frontmatter(date: datetime, keyword: str, indices: list[dict],
                 holdings_mentioned: list[dict]) -> str:
    mentioned = [h["name"] for h in holdings_mentioned if h.get("mentioned")]
    idx_lines = "\n".join(
        f'  - "{q["name"]}: {q["close"]} ({q["direction"]}{abs(q["change_pct"])}%)"'
        for q in indices[:6]
    )
    return f"""---
date: {date.strftime("%Y-%m-%d")}
tags: [3protv, 시황]
keyword: "{keyword}"
holdings_mentioned: {mentioned if mentioned else "[]"}
indices:
{idx_lines if idx_lines else "  []"}
---
"""


def read_us_section_today(obsidian_cfg: dict) -> str:
    """kr 세션이 us 세션 리포트를 컨텍스트로 읽는다."""
    vault = _clone_vault(obsidian_cfg)
    if not vault:
        return ""
    date = now_kst()
    ymd = date.strftime("%Y%m%d")
    pattern = str(
        vault / obsidian_cfg["base_path"] / date.strftime("%Y") / date.strftime("%m")
        / f"3protv오늘_{ymd}*.md"
    )
    files = sorted(globmod.glob(pattern))
    if not files:
        return ""
    content = Path(files[0]).read_text(encoding="utf-8")
    if US_MARKER in content:
        return content.split(US_MARKER, 1)[1].split(KR_MARKER, 1)[0]
    return content


def archive_report(
    obsidian_cfg: dict,
    session: str,
    keyword: str,
    markdown_report: str,
    indices: list[dict],
    holdings_mentioned: list[dict],
    news_markdown: str = "",
) -> bool:
    """리포트를 볼트 repo에 커밋·push. 실패해도 파이프라인은 계속(best-effort).

    news_markdown(종목 기사검색)은 같은 날짜 폴더의 **별도 파일**
    `3protv기사_YYYYMMDD.md`로 저장하고, 시황 파일에서 `[[위키링크]]`로 연결한다
    (기사 목록이 시황 본문을 잠식하지 않게 — 2026-07-27 사용자 요청).
    """
    vault = _clone_vault(obsidian_cfg)
    if not vault:
        return False
    try:
        date = now_kst()
        path = _today_file(vault, obsidian_cfg["base_path"], keyword, date)
        marker = US_MARKER if session == "us" else KR_MARKER
        section_title = "## 🇺🇸 미국 시황 (06시 방송)" if session == "us" \
            else "## 🇰🇷 한국 시황 (08시 방송)"
        section = f"\n{marker}\n{section_title}\n\n{markdown_report}\n"

        news_path: Path | None = None
        if news_markdown.strip():
            news_path = _news_file(vault, obsidian_cfg["base_path"], date)
            head = US_NEWS_HEAD if session == "us" else KR_NEWS_HEAD
            other = KR_NEWS_HEAD if session == "us" else US_NEWS_HEAD
            prev = news_path.read_text(encoding="utf-8") if news_path.exists() \
                else f"# {news_note_name(date)}\n"
            if head in prev:
                # 같은 세션 재실행 → 내 섹션만 걷어내고 다른 세션 섹션은 보존
                before, rest = prev.split(head, 1)
                prev = before + (other + rest.split(other, 1)[1] if other in rest else "")
            news_path.write_text(
                f"{prev.rstrip()}\n\n{head}\n\n{news_markdown}\n", encoding="utf-8")
            section += f"\n📰 종목 기사검색 → [[{news_note_name(date)}]]\n"

        if path.exists():
            content = path.read_text(encoding="utf-8")
            if marker in content:
                # 같은 세션 재실행 → 해당 섹션 교체
                before = content.split(marker, 1)[0]
                rest = content.split(marker, 1)[1]
                other_marker = KR_MARKER if marker == US_MARKER else US_MARKER
                after = ""
                if other_marker in rest:
                    after = other_marker + rest.split(other_marker, 1)[1]
                content = before + section + after
            else:
                content += section
        else:
            title = f"# {path.stem}\n"
            content = _frontmatter(date, keyword, indices, holdings_mentioned) + title + section

        path.write_text(content, encoding="utf-8")

        rel = path.relative_to(vault)
        to_add = [str(rel)]
        if news_path is not None:
            to_add.append(str(news_path.relative_to(vault)))
        subprocess.run(["git", "-C", str(vault), "add", *to_add],
                       check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "-C", str(vault),
             "-c", "user.name=3tv-bot", "-c", "user.email=3tv-bot@users.noreply.github.com",
             "commit", "-m", f"3protv {session} 리포트 {date:%Y-%m-%d}"],
            capture_output=True, text=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            log.error("볼트 커밋 실패: %s", commit.stderr[-300:])
            return False
        branch = obsidian_cfg.get("vault_branch", "main")
        # -u: 빈 저장소에서 git init으로 새로 만든 경우 원격에 branch가 없어
        # upstream 추적이 없으므로 매번 명시적으로 지정 (기존 브랜치엔 무해)
        push = subprocess.run(["git", "-C", str(vault), "push", "-u", "origin", branch],
                              capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            log.error("볼트 push 실패: %s", push.stderr[-300:])
            return False
        log.info("옵시디안 볼트 저장 완료: %s", rel)
        return True
    except Exception as e:
        log.error("옵시디안 아카이브 오류(무시하고 계속): %s", e)
        return False
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)
