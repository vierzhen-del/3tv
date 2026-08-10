# 텔레그램 다중 발송 가이드 — 다른 단체방에도 같이 보내기

3tv 리포트/알림은 `notify_telegram.send_telegram()` 하나로 나갑니다. 이 함수는
`TELEGRAM_CHAT_ID`(콤마 구분)에 적힌 모든 chat id에 **같은 내용을 순서대로** 보냅니다
— 기존 대상은 그대로 두고 새 단체방 id만 뒤에 추가하면 됩니다. 리포트 발송
(kr/noon/us/night-digest), ETF 리뷰, vault-check 경고, `notify.yml` 공지 전송이
전부 이 값을 공유합니다.

## 1. 봇을 새 단체방에 초대

1. 텔레그램에서 대상 단체방 열기 → 멤버 추가
2. 3tv가 쓰는 봇(기존 3protv 알림봇, `TELEGRAM_BOT_TOKEN` 발급 시 만든 그 봇)을 검색해 초대
3. 그룹이 "개인정보 보호 모드"(Privacy Mode)가 켜진 봇이면 방 안의 일반 메시지는 못 읽지만,
   **메시지 보내기 자체는 초대만 되면 가능**합니다. chat id를 얻는 절차(2번)에서만 잠깐
   신경 쓰면 됩니다.

## 2. 새 단체방의 chat id 얻기

단체방 chat id는 음수입니다(`-100...`으로 시작하는 경우가 많음). 얻는 방법 두 가지:

**A. getUpdates로 직접 확인 (권장, 추가 봇 불필요)**

1. 봇을 초대한 단체방에 아무 메시지나 하나 보냅니다(예: "테스트").
2. 브라우저나 curl로 아래 URL을 엽니다 (`<TOKEN>`은 `TELEGRAM_BOT_TOKEN` 값):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. 응답 JSON에서 방금 보낸 메시지의 `"chat":{"id": -1009876543210, "type":"group", ...}`를 찾습니다.
   `id` 값이 그 방의 chat id입니다.
4. 응답이 비어 있으면 Privacy Mode 때문에 봇이 그 메시지를 못 받은 것입니다 — 방 관리자 →
   봇 권한에서 메시지 읽기를 허용하거나, [@BotFather](https://t.me/BotFather)에서
   `/setprivacy` → 해당 봇 선택 → **Disable**로 바꾼 뒤 다시 시도하세요.

**B. 확인용 봇 사용 (더 간단하지만 추가 봇 필요)**

[@RawDataBot](https://t.me/RawDataBot) 또는 [@userinfobot](https://t.me/userinfobot)을 같은
단체방에 잠깐 초대하면 입장 즉시 방 정보(그 안의 `chat.id`)를 메시지로 보여줍니다. 확인 후
이 봇은 방에서 빼도 됩니다.

## 3. TELEGRAM_CHAT_ID 값 갱신

기존 값이 예: `123456789` (개인 chat id) 였다면, 새 단체방 id를 콤마로 이어붙입니다:

```
123456789,-1009876543210
```

- **GitHub Actions에서 도는 자동 리포트**: repo → Settings → Secrets and variables →
  Actions → `TELEGRAM_CHAT_ID` 값을 위 형식으로 수정. 워크플로 YAML은 이미
  `secrets.TELEGRAM_CHAT_ID`를 그대로 전달하므로 **워크플로 파일 수정은 필요 없습니다.**
- **로컬 실행(`.env`)**: `.env`의 `TELEGRAM_CHAT_ID`도 동일하게 콤마로 이어붙이면 됩니다.

## 4. 확인

로컬에서 빠르게 검증하려면:

```bash
python scripts/send_notice.py docs/notices/latest.md
```

(없으면 짧은 텍스트 파일을 하나 만들어 경로로 넘기면 됩니다.) 두 방 모두에 같은 메시지가
도착하면 성공입니다. 한쪽만 실패하면 로그에 `텔레그램 전송 실패(<chat_id>) ...`로 어느 chat
id가 실패했는지 남습니다 — token은 맞는데 그 방에 봇이 없거나(kicked) Privacy Mode로 막힌
경우가 대부분입니다.

## 참고: 방을 늘리는 게 아니라 완전히 바꾸고 싶다면

동시 발송이 아니라 **기존 대상을 새 단체방으로 교체**하고 싶다면, 2번으로 얻은 새 chat id로
`TELEGRAM_CHAT_ID` 값을 통째로 덮어쓰면 됩니다(콤마로 이어붙이지 않음). 코드 변경은 필요
없습니다.
