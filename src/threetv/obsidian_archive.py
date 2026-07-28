"""옵시디안 second brain 볼트(GitHub repo)에 리포트 마크다운 저장.

- 볼트 repo를 얕게 clone → 3protv/YYYY/MM/3protv오늘_YYYYMMDD_키워드.md 작성 → 커밋·push
- us 세션이 먼저 파일을 만들고, kr 세션이 같은 날짜 파일에 한국 섹션을 병합
- 탭S9의 n8n 스케줄이 이 repo에서 fetch → 볼트 3protv/ → Syncthing이 S26으로 전파
  (Obsidian Git 플러그인 방식은 2026-07-18 이중 동기화 충돌로 폐기. docs/n8n_s9_sync.md)

여기서 나는 실패는 **조용히 넘어가면 안 된다** — GH_PAT 만료로 push가 8일간 끊겼는데
파이프라인이 계속 초록불이라 아무도 몰랐던 사고가 있었다(2026-07-27). 그래서
`archive_report`는 bool이 아니라 실패 사유가 담긴 `ArchiveResult`를 돌려주고,
호출부(main.py)가 이를 텔레그램 경고 + 종료코드로 승격시킨다.
"""
from __future__ import annotations

import glob as globmod
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from .common import REPO_ROOT, env_token, log, now_kst

VAULT_TMP = REPO_ROOT / ".vault_tmp"

# 볼트가 꺼져 있는 정상 상태 — 실패로 취급해 경고를 띄우면 안 된다
DISABLED = "disabled"


class ArchiveResult(NamedTuple):
    """볼트 저장 결과.

    ok=True                    저장·push 성공
    ok=False, skipped=True     비활성(--skip-archive / obsidian.enabled: false) — 정상
    ok=False, skipped=False    실제 실패 — reason을 경고로 띄우고 종료코드를 1로 올린다
    """
    ok: bool
    skipped: bool
    reason: str = ""
    rel: str = ""

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


def _clone_vault(obsidian_cfg: dict) -> tuple[Path | None, str]:
    """(볼트경로, 실패사유). 비활성이면 (None, DISABLED) — 이건 실패가 아니다."""
    vault_repo = (obsidian_cfg.get("vault_repo") or "").strip()
    if not obsidian_cfg.get("enabled") or not vault_repo:
        log.info("옵시디안 아카이브 비활성 (vault_repo 미설정)")
        return None, DISABLED
    branch = obsidian_cfg.get("vault_branch", "main")
    if VAULT_TMP.exists():
        shutil.rmtree(VAULT_TMP)
    url = _vault_url(vault_repo)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(VAULT_TMP)],
            check=True, capture_output=True, timeout=300,
        )
        return VAULT_TMP, ""
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
            return VAULT_TMP, ""
        reason = _clone_reason(stderr)
        log.error("볼트 clone 실패: %s", stderr[-300:])
        return None, reason
    except subprocess.TimeoutExpired:
        log.error("볼트 clone 타임아웃 (300초)")
        return None, "clone 타임아웃 (300초) — 네트워크 문제"


def _clone_reason(stderr: str) -> str:
    """git stderr에서 사람이 바로 조치할 수 있는 한 줄로 요약."""
    if "Invalid username or token" in stderr or "Authentication failed" in stderr:
        if not env_token("GH_PAT"):
            return "GH_PAT 시크릿이 비어 있음"
        return "GH_PAT 인증 실패 — 토큰 만료/권한 부족으로 보임"
    if "Repository not found" in stderr:
        return "볼트 repo를 찾을 수 없음 — 이름 또는 PAT 접근 범위 확인"
    return f"clone 실패: {stderr.strip()[-200:]}"


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


def obsidian_deeplink(
    obsidian_cfg: dict, date: datetime | None = None, file_prefix: str = "3protv오늘",
) -> str:
    """탭S9/S26에서 탭하면 옵시디안이 열리는 딥링크.

    파일명에는 그날 키워드가 붙어 전송 시점엔 확정되지 않으므로(us 세션이 만든
    파일을 kr이 재사용) **검색 딥링크**를 쓴다 — 날짜만으로 항상 맞는다.
    `file_prefix`로 noon(`3protv정오`)·night(`3protv야간`) 노트도 같은 방식으로 연결한다.
    """
    vault = (obsidian_cfg or {}).get("vault_name", "").strip()
    if not vault:
        return ""
    ymd = (date or now_kst()).strftime("%Y%m%d")
    return (f"obsidian://search?vault={quote(vault)}"
            f"&query={quote(f'{file_prefix}_{ymd}')}")


def _frontmatter(
    date: datetime, session_tag: str, keyword: str = "",
    indices: list[dict] | None = None, holdings_mentioned: list[dict] | None = None,
) -> str:
    """모든 노트 공통 frontmatter — 연도/월/세션 태그를 심어 옵시디안 검색·
    Dataview에서 연관검색(같은 달/세션끼리 묶어보기)이 되게 한다."""
    tags = ["3protv", session_tag, f"3protv/{date:%Y}", f"3protv/{date:%Y-%m}"]
    lines = [
        "---",
        f'date: {date:%Y-%m-%d}',
        f'year: {date:%Y}',
        f'month: "{date:%Y-%m}"',
        f'session: {session_tag}',
        f'tags: [{", ".join(tags)}]',
    ]
    if keyword:
        lines.append(f'keyword: "{keyword}"')
    if holdings_mentioned is not None:
        mentioned = [h["name"] for h in holdings_mentioned if h.get("mentioned")]
        lines.append(f'holdings_mentioned: {mentioned if mentioned else "[]"}')
    if indices is not None:
        idx_lines = "\n".join(
            f'  - "{q["name"]}: {q["close"]} ({q["direction"]}{abs(q["change_pct"])}%)"'
            for q in indices[:6]
        )
        lines.append("indices:")
        lines.append(idx_lines if idx_lines else "  []")
    lines.append("---\n")
    return "\n".join(lines)


_DAY_LABELS = {
    "3protv오늘": "일일시황",
    "3protv기사": "종목기사",
    "3protv정오": "정오시황",
    "3protv야간": "야간미장",
}
_WEEKDAY_KO = "월화수목금토일"
RELATED_MARKER = "<!-- 3tv:related -->"


def _relink_day(month_dir: Path, ymd: str) -> list[Path]:
    """같은 날짜(ymd)의 노트끼리 하단에 '관련 노트' 위키링크를 채워 넣는다.

    옵시디안 백링크/그래프뷰로 그날 시황·기사·정오·야간 노트가 서로 연결돼
    보이게 하는 것이 목적 — 파일명이 매번 키워드로 달라져 자동 백링크만으론
    안 잡히므로 명시적으로 링크한다. 새 노트가 추가될 때마다 그날 전체 노트를
    다시 훑어 재작성하므로 순서와 무관하게 항상 최신 상태를 유지한다.
    """
    files = sorted(f for f in month_dir.glob(f"*{ymd}*.md") if f.name != "_index.md")
    changed = []
    for f in files:
        siblings = [g for g in files if g != f]
        content = f.read_text(encoding="utf-8")
        body = content.split(RELATED_MARKER, 1)[0].rstrip()
        if siblings:
            links = " · ".join(f"[[{g.stem}]]" for g in siblings)
            block = f"\n\n{RELATED_MARKER}\n---\n🔗 같은 날 다른 3tv 리포트: {links}\n"
        else:
            block = f"\n\n{RELATED_MARKER}\n"
        new_content = body + block
        if new_content != content:
            f.write_text(new_content, encoding="utf-8")
            changed.append(f)
    return changed


def _rebuild_month_index(vault: Path, base_path: str, date: datetime) -> Path:
    """월별 인덱스(MOC) 노트 재생성 — 연도/월/일자 단위 검색·요약 참조용.

    호출될 때마다 그 달 폴더를 훑어 처음부터 다시 쓰므로(멱등) 순서·중복
    걱정 없이 항상 그 시점의 실제 파일 구성을 반영한다.
    """
    month_dir = vault / base_path / date.strftime("%Y") / date.strftime("%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[tuple[str, str]]] = {}
    for f in sorted(month_dir.glob("*.md")):
        if f.name == "_index.md":
            continue
        m = re.search(r"_(\d{8})", f.stem)
        if not m:
            continue
        ymd = m.group(1)
        prefix = f.stem.split("_")[0]
        label = _DAY_LABELS.get(prefix, prefix)
        by_day.setdefault(ymd, []).append((label, f.stem))
    lines = [
        "---",
        f'tags: [3protv, index, "3protv/{date:%Y}", "3protv/{date:%Y-%m}"]',
        "---",
        f"# {date:%Y}년 {date:%m}월 3protv 인덱스",
        "",
        "일자별 시황 리포트 모음 — 검색/Dataview 참조용.",
        "",
    ]
    for ymd in sorted(by_day):
        d = datetime.strptime(ymd, "%Y%m%d")
        items = " · ".join(
            f"[[{stem}|{label}]]" for label, stem in sorted(by_day[ymd])
        )
        lines.append(f"- **{d:%Y-%m-%d} ({_WEEKDAY_KO[d.weekday()]})**: {items}")
    path = month_dir / "_index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _index_and_relink(vault: Path, base_path: str, month_dir: Path, date: datetime,
                      ymd: str, already: set[Path]) -> list[Path]:
    """`_relink_day` + `_rebuild_month_index`를 함께 실행하고, git add에 추가할
    새 변경 파일 목록(이미 add 예정인 파일 제외)을 돌려준다."""
    changed = _relink_day(month_dir, ymd)
    index_path = _rebuild_month_index(vault, base_path, date)
    extra = {p for p in changed} | {index_path}
    return [p for p in extra if p not in already]


def read_us_section_today(obsidian_cfg: dict) -> tuple[str, str]:
    """kr 세션이 us 세션 리포트를 컨텍스트로 읽는다 → (본문, 실패사유).

    실패사유가 비어있지 않으면 kr 리포트가 **미장 맥락 없이** 만들어진다는 뜻이므로
    호출부가 경고에 실어 보낸다. 볼트가 꺼져 있으면 사유는 DISABLED(정상).
    """
    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return "", reason
    try:
        date = now_kst()
        ymd = date.strftime("%Y%m%d")
        pattern = str(
            vault / obsidian_cfg["base_path"] / date.strftime("%Y") / date.strftime("%m")
            / f"3protv오늘_{ymd}*.md"
        )
        files = sorted(globmod.glob(pattern))
        if not files:
            # us 세션이 아직 안 돌았거나 us 아카이브가 실패한 상태 — 둘 다 알 가치가 있다
            return "", "볼트에 오늘 us 리포트가 없음 (us 세션 미실행 또는 저장 실패)"
        content = Path(files[0]).read_text(encoding="utf-8")
        if US_MARKER in content:
            return content.split(US_MARKER, 1)[1].split(KR_MARKER, 1)[0], ""
        return content, ""
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


def archive_simple_report(
    obsidian_cfg: dict, file_prefix: str, keyword: str, markdown_report: str,
) -> ArchiveResult:
    """noon/night처럼 **하루 1회만** 발행되는 단일 세션 리포트를 저장한다.

    us/kr의 `archive_report()`는 같은 날짜 파일에 US_MARKER/KR_MARKER로 두 세션을
    병합하는 구조라 세션이 늘어나면 안 맞는다. noon/night은 병합 상대가 없으므로
    그날 새 파일 하나를 통째로 쓰는 훨씬 단순한 경로를 쓴다.
    """
    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return ArchiveResult(ok=False, skipped=reason == DISABLED, reason=reason)
    try:
        date = now_kst()
        ymd = date.strftime("%Y%m%d")
        month_dir = vault / obsidian_cfg["base_path"] / date.strftime("%Y") / date.strftime("%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        safe_kw = "".join(c for c in keyword if c.isalnum() or c in "가-힣_-")[:20]
        path = month_dir / f"{file_prefix}_{ymd}_{safe_kw or '리포트'}.md"
        session_tag = _DAY_LABELS.get(file_prefix, file_prefix)
        front = _frontmatter(date, session_tag, keyword=keyword)
        path.write_text(f"{front}# {path.stem}\n\n{markdown_report}\n", encoding="utf-8")

        rel = path.relative_to(vault)
        extra = _index_and_relink(vault, obsidian_cfg["base_path"], month_dir, date, ymd, {path})
        to_add = [str(rel)] + [str(p.relative_to(vault)) for p in extra]
        subprocess.run(["git", "-C", str(vault), "add", *to_add],
                       check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "-C", str(vault),
             "-c", "user.name=3tv-bot", "-c", "user.email=3tv-bot@users.noreply.github.com",
             "commit", "-m", f"{file_prefix} 리포트 {date:%Y-%m-%d}"],
            capture_output=True, text=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            return ArchiveResult(False, False,
                                 f"커밋 실패: {commit.stderr.strip()[-200:]}", str(rel))
        branch = obsidian_cfg.get("vault_branch", "main")
        push = subprocess.run(["git", "-C", str(vault), "push", "-u", "origin", branch],
                              capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            return ArchiveResult(False, False,
                                 f"push 실패: {push.stderr.strip()[-200:]}", str(rel))
        log.info("옵시디안 볼트 저장 완료: %s", rel)
        return ArchiveResult(True, False, "", str(rel))
    except Exception as e:
        log.error("옵시디안 아카이브 오류(무시하고 계속): %s", e)
        return ArchiveResult(False, False, f"아카이브 오류: {e}")
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


def archive_report(
    obsidian_cfg: dict,
    session: str,
    keyword: str,
    markdown_report: str,
    indices: list[dict],
    holdings_mentioned: list[dict],
    news_markdown: str = "",
) -> ArchiveResult:
    """리포트를 볼트 repo에 커밋·push. 실패해도 **파이프라인은 계속** 진행하되,
    사유를 담은 ArchiveResult를 돌려줘 호출부가 경고·종료코드로 승격시킨다.

    news_markdown(종목 기사검색)은 같은 날짜 폴더의 **별도 파일**
    `3protv기사_YYYYMMDD.md`로 저장하고, 시황 파일에서 `[[위키링크]]`로 연결한다
    (기사 목록이 시황 본문을 잠식하지 않게 — 2026-07-27 사용자 요청).
    """
    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return ArchiveResult(ok=False, skipped=reason == DISABLED, reason=reason)
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
                else _frontmatter(date, "종목기사") + f"# {news_note_name(date)}\n"
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
            content = _frontmatter(date, "일일시황", keyword=keyword, indices=indices,
                                   holdings_mentioned=holdings_mentioned) + title + section

        path.write_text(content, encoding="utf-8")

        rel = path.relative_to(vault)
        ymd = date.strftime("%Y%m%d")
        already = {path} | ({news_path} if news_path is not None else set())
        extra = _index_and_relink(vault, obsidian_cfg["base_path"], path.parent, date, ymd, already)
        to_add = [str(rel)]
        if news_path is not None:
            to_add.append(str(news_path.relative_to(vault)))
        to_add += [str(p.relative_to(vault)) for p in extra]
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
            return ArchiveResult(False, False,
                                 f"커밋 실패: {commit.stderr.strip()[-200:]}", str(rel))
        branch = obsidian_cfg.get("vault_branch", "main")
        # -u: 빈 저장소에서 git init으로 새로 만든 경우 원격에 branch가 없어
        # upstream 추적이 없으므로 매번 명시적으로 지정 (기존 브랜치엔 무해)
        push = subprocess.run(["git", "-C", str(vault), "push", "-u", "origin", branch],
                              capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            log.error("볼트 push 실패: %s", push.stderr[-300:])
            return ArchiveResult(False, False,
                                 f"push 실패: {push.stderr.strip()[-200:]}", str(rel))
        log.info("옵시디안 볼트 저장 완료: %s", rel)
        return ArchiveResult(True, False, "", str(rel))
    except Exception as e:
        log.error("옵시디안 아카이브 오류(무시하고 계속): %s", e)
        return ArchiveResult(False, False, f"아카이브 오류: {e}")
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


# ─────────────────── night 세션: 슬롯 캡처 → 06시 종합 ───────────────────
#
# GitHub Actions 단일 job은 최대 6시간이라 22:00~06:00(8h) 연속 녹화가 불가능하다.
# 매시 정각 5분만 캡처하는 슬롯 job 8개가 각자 독립 실행되므로, 로컬 파일로는
# 서로의 결과를 못 본다 — us→kr이 쓰는 것과 같은 볼트 repo를 슬롯 간 전달 통로로
# 재사용한다(3protv/ 폴더와 안 겹치게 _night_slots/ 아래에 저장).

NIGHT_SLOT_DIR = "_night_slots"


def save_night_slot(
    obsidian_cfg: dict, date_ymd: str, hour_label: str, payload: dict
) -> ArchiveResult:
    """슬롯 1회분(vision_results 등)을 볼트에 저장 — 06시 종합이 나중에 모아 읽는다."""
    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return ArchiveResult(ok=False, skipped=reason == DISABLED, reason=reason)
    try:
        slot_dir = vault / NIGHT_SLOT_DIR / date_ymd
        slot_dir.mkdir(parents=True, exist_ok=True)
        path = slot_dir / f"{hour_label}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        rel = path.relative_to(vault)
        subprocess.run(["git", "-C", str(vault), "add", str(rel)],
                       check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "-C", str(vault),
             "-c", "user.name=3tv-bot", "-c", "user.email=3tv-bot@users.noreply.github.com",
             "commit", "-m", f"night slot {hour_label} {date_ymd}"],
            capture_output=True, text=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            return ArchiveResult(False, False,
                                 f"슬롯 커밋 실패: {commit.stderr.strip()[-200:]}", str(rel))
        branch = obsidian_cfg.get("vault_branch", "main")
        push = subprocess.run(["git", "-C", str(vault), "push", "-u", "origin", branch],
                              capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            return ArchiveResult(False, False,
                                 f"슬롯 push 실패: {push.stderr.strip()[-200:]}", str(rel))
        log.info("야간 슬롯 저장 완료: %s", rel)
        return ArchiveResult(True, False, "", str(rel))
    except Exception as e:
        log.error("야간 슬롯 저장 오류(무시하고 계속): %s", e)
        return ArchiveResult(False, False, f"슬롯 저장 오류: {e}")
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)


def read_night_slots(obsidian_cfg: dict, date_ymd: str) -> tuple[list[dict], str]:
    """오늘 저장된 슬롯 전체를 시각순으로 읽는다 → (슬롯 목록, 실패사유).

    슬롯 job이 죽거나 늦어 일부만 있어도 있는 만큼으로 종합을 진행한다 —
    개별 슬롯 실패로 06시 종합 전체가 무산되면 안 된다.
    """
    vault, reason = _clone_vault(obsidian_cfg)
    if not vault:
        return [], reason
    try:
        slot_dir = vault / NIGHT_SLOT_DIR / date_ymd
        files = sorted(slot_dir.glob("*.json")) if slot_dir.exists() else []
        if not files:
            return [], f"{date_ymd} 야간 슬롯이 볼트에 하나도 없음 (전체 슬롯 job 실패 추정)"
        slots = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["hour_label"] = f.stem
                slots.append(data)
            except Exception as e:
                log.warning("슬롯 파일 파싱 실패(건너뜀) %s: %s", f, e)
        return slots, ""
    finally:
        shutil.rmtree(VAULT_TMP, ignore_errors=True)
