"""텔레그램 전송 (기존 3protv 알림봇 토큰 재사용)."""
from __future__ import annotations

import re

import requests

from .common import env_token, log


def send_telegram(text: str, max_len: int = 4000, html: bool = False) -> bool:
    """텔레그램 전송 — 개인 채팅 + (설정 시) 다른 사람이 있는 그룹방에 동시 발송.

    html=True면 parse_mode=HTML로 보낸다 — 링크가 제목만 보이고(긴 URL 숨김),
    `<blockquote expandable>` 접기 블록이 눌러서 펼쳐진다(Bot API 7.3+).
    HTML 파싱이 실패하면(태그 깨짐 등) 그 조각만 평문으로 1회 재전송해
    메시지를 통째로 잃지 않는다.

    ⚠️ TELEGRAM_GROUP_CHAT_ID(선택)를 넣으면 같은 내용을 그 chat_id에도 보낸다.
    봇을 그 그룹/채널에 초대한 뒤 chat_id를 알아내(예: @userinfobot, 또는
    getUpdates 응답의 message.chat.id — 그룹은 음수) secret으로 등록하면 된다.
    그룹 발송은 best-effort다 — 실패해도 개인 채팅 발송 결과(반환값)에는 영향 없음
    (카카오와 달리 텔레그램 봇은 API로 어느 대화방이든 초대만 되면 보낼 수 있다).
    """
    token = env_token("TELEGRAM_BOT_TOKEN")
    # 기존 v28 .env 호환: TELEGRAM_CHAT_ID 없으면 TELEGRAM_NOTIFY_CHANNEL 사용
    chat_id = env_token("TELEGRAM_CHAT_ID") or env_token("TELEGRAM_NOTIFY_CHANNEL")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 — 텔레그램 전송 생략")
        return False

    ok = _send_to_chat(token, chat_id, text, max_len, html)

    group_chat_id = env_token("TELEGRAM_GROUP_CHAT_ID")
    if group_chat_id:
        if not _send_to_chat(token, group_chat_id, text, max_len, html):
            log.warning("텔레그램 그룹방(TELEGRAM_GROUP_CHAT_ID) 전송 실패 — 개인 채팅 결과는 별개")

    return ok


def _send_to_chat(token: str, chat_id: str, text: str, max_len: int, html: bool) -> bool:
    """한 chat_id에 분할·HTML 폴백까지 처리해 전송."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _split(text, max_len, html=html):
        payload = {"chat_id": chat_id, "text": chunk}
        if html:
            payload["parse_mode"] = "HTML"
            payload["link_preview_options"] = {"is_disabled": True}
        resp = requests.post(url, json=payload, timeout=30)

        if resp.status_code != 200 and html:
            # 파싱 실패 추정 → 태그를 벗겨 평문으로 한 번 더 (내용 유실 방지)
            log.warning("텔레그램 HTML 파싱 실패(%d, chat_id=%s) → 평문 재전송: %s",
                        resp.status_code, chat_id, resp.text[:200])
            resp = requests.post(
                url, json={"chat_id": chat_id, "text": _strip_tags(chunk)}, timeout=30
            )
        if resp.status_code != 200:
            log.error("텔레그램 전송 실패 %d (chat_id=%s): %s",
                       resp.status_code, chat_id, resp.text[:300])
            ok = False
    if ok:
        log.info("텔레그램 전송 완료%s (chat_id=%s)", " (HTML)" if html else "", chat_id)
    return ok


def _strip_tags(s: str) -> str:
    """HTML 폴백용 — 태그 제거 + 엔티티 복원."""
    s = re.sub(r"<a href=\"([^\"]+)\">(.*?)</a>", r"\2 (\1)", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&amp;", "&"))


def send_alert(text: str) -> None:
    """파이프라인 실패 알림 등 짧은 경고 메시지."""
    try:
        send_telegram(text)
    except Exception as e:
        log.error("알림 전송 실패: %s", e)


# 여러 줄에 걸치는 HTML 블록 — 이 안에서 잘리면 파싱이 깨진다
_BLOCK_RE = re.compile(r"<(blockquote|pre)\b[^>]*>.*?</\1>", re.S)


def _split(text: str, max_len: int, html: bool = False) -> list[str]:
    """4000자 단위 분할.

    html=True면 `<blockquote expandable>`·`<pre>`처럼 **여러 줄에 걸치는 블록을
    통째로** 한 조각에 담는다. 줄 단위로만 자르면 접기 블록 한가운데서 끊겨
    텔레그램이 "can't parse entities"로 조각 전체를 거부한다.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    def add(piece: str) -> None:
        nonlocal current
        if not current:
            current = piece
        elif len(current) + len(piece) + 1 <= max_len:
            current = f"{current}\n{piece}"
        else:
            flush()
            current = piece

    for atom in _atoms(text, html):
        if len(atom) <= max_len:
            add(atom)
            continue
        # 조각 하나가 이미 한도를 넘는다 — 안전한 지점에서 쪼갠다
        for piece in _break_long(atom, max_len, html):
            add(piece)
    flush()
    return chunks


def _atoms(text: str, html: bool) -> list[str]:
    """분할 단위 목록. HTML이면 여러 줄 블록은 쪼개지 않고 하나로 취급."""
    if not html:
        return text.split("\n")
    atoms: list[str] = []
    pos = 0
    for m in _BLOCK_RE.finditer(text):
        atoms += text[pos : m.start()].split("\n")
        atoms.append(m.group(0))
        pos = m.end()
    atoms += text[pos:].split("\n")
    return atoms


def _break_long(atom: str, max_len: int, html: bool) -> list[str]:
    """한도를 넘는 단일 조각을 쪼갠다.

    블록(`<blockquote>`/`<pre>`)이면 여는·닫는 태그를 조각마다 다시 붙여
    각 조각이 그 자체로 유효한 HTML이 되게 한다.
    """
    m = _BLOCK_RE.fullmatch(atom) if html else None
    if m:
        inner = atom[atom.index(">") + 1 : atom.rindex("</")]
        open_tag = atom[: atom.index(">") + 1]
        close_tag = atom[atom.rindex("</") :]
        room = max_len - len(open_tag) - len(close_tag)
        return [
            f"{open_tag}{p}{close_tag}"
            for p in _break_long(inner, max(room, 1), html)
        ]

    out: list[str] = []
    buf = ""
    for line in atom.split("\n"):
        while len(line) > max_len:
            cut = _safe_cut(line, max_len) if html else max_len
            out.append(line[:cut])
            line = line[cut:]
        if not buf:
            buf = line
        elif len(buf) + len(line) + 1 <= max_len:
            buf = f"{buf}\n{line}"
        else:
            out.append(buf)
            buf = line
    if buf:
        out.append(buf)
    return out


def _safe_cut(s: str, limit: int) -> int:
    """s[:i]가 태그 중간이나 <a>…</a> 사이를 가르지 않는 최대 i."""
    cut = limit
    # 태그 한가운데(<a href="… )면 그 '<' 앞으로 물린다
    lt, gt = s.rfind("<", 0, cut), s.rfind(">", 0, cut)
    if lt > gt:
        cut = lt
    # <a>는 열었는데 </a>를 아직 못 닫았으면 그 <a> 앞으로 물린다
    head = s[:cut]
    if head.count("<a ") > head.count("</a>"):
        cut = head.rfind("<a ")
    return max(1, cut)
