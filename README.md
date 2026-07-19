# 3tv — 삼프로TV 라이브 분석 데일리 시황 리포트

삼프로TV(@3protv) 아침 라이브 방송을 자동 녹화·분석해 **미국 전일 시황 + 한국 당일 전망** 리포트를 매일 아침 텔레그램/카카오톡으로 보내고, 옵시디안 second brain 볼트에 저장하는 파이프라인입니다.

라이브 방송은 자막이 없으므로 자막 대신:
- **자료화면 분석**: 흰 배경 자료화면 프레임만 선별 → Gemini 비전으로 텍스트·표·그래프 추출 (광고/스튜디오 화면 자동 제외)
- **음성 분석**: 녹화 구간 오디오를 Whisper로 전사 → 진행자·패널 발언의 종목 언급/호재·악재 맥락 확보

## 하루 흐름 (평일, KST)

| 시각 | 세션 | 동작 |
|------|------|------|
| 05:55 | `us` 시작 | 라이브 폴링 |
| 05:55~06:40 | `us` 녹화 | 비정기방송(월가 인사이트, 영문 자료도 분석) + 본방1 미국 시황 40분. 진행자가 보드형 광고 소개를 시작하면 방송 종료로 취급(이후 분석 제외) |
| ~07:00 | `us` 전송 | 🇺🇸 미국 전일 시황 리포트 → 텔레그램/카카오 + 옵시디안 |
| 07:55 | `kr` 시작 | 라이브 폴링 |
| 07:55~08:25 | `kr` 녹화 | 방송2 한국 시황 — 흰색배경 자료화면 중심 요약 |
| ~08:40 | `kr` 전송 | 🇰🇷 한국 당일 전망 (아침 us 리포트 연동) → 같은 옵시디안 파일에 병합 |

자료화면 판별: `us` 세션은 흰배경 슬라이드 외에 **어두운 배경의 전체화면 데이터 화면(Finviz, 차트 사이트)과 영문 기사 자료**도 분석 대상입니다. 화면 속 배경/하단 배너 광고(ETF·상품)는 무시하고 본 자료만 추출합니다. 예시 화면은 `docs/reference/examples/` 참고.

## 파이프라인

```
capture (yt-dlp+ffmpeg 라이브 녹화, 480p)
  → frames (10초/1프레임, 흰배경 휴리스틱 + phash 중복제거)
  → vision (Gemini: 자료화면/광고 분류 + 텍스트·그래프 추출)
  → transcribe (faster-whisper 한국어 전사)
  → market (언급 종목을 yfinance/pykrx 실시세로 검증)
  → report (Claude: 시황 요약·종목 예측·보유종목 언급 체크)
  → notify (텔레그램 + 카카오 나에게보내기)
  → archive (옵시디안 볼트 repo 커밋 → S26/탭S9 자동 pull, 노션 선택)
```

## 셋업

### 1. GitHub Secrets 등록 (Settings → Secrets and variables → Actions)

| Secret | 필수 | 설명 |
|--------|------|------|
| `GEMINI_API_KEY` | ✅ | https://aistudio.google.com 에서 발급 (프레임 분석) |
| `ANTHROPIC_API_KEY` | ✅ | https://console.anthropic.com 에서 발급 (리포트 생성) |
| `TELEGRAM_BOT_TOKEN` | ✅ | 기존 3protv 알림봇 토큰 재사용 가능 |
| `TELEGRAM_CHAT_ID` | ✅ | 수신자/채널 chat id |
| `YOUTUBE_COOKIES` | 권장 | 유튜브 봇차단 대응 (아래 참고) |
| `KAKAO_REST_API_KEY` | 선택 | 카카오 나에게 보내기 |
| `KAKAO_REFRESH_TOKEN` | 선택 | `scripts/kakao_get_token.py`로 발급 |
| `GH_PAT` | 선택 | 옵시디안 볼트 push + 카카오 토큰 자동갱신 — **최소 스코프 발급 필수 (아래 참고)** |
| `NOTION_API_KEY` / `NOTION_PARENT_ID` | 선택 | 노션 아카이브 (settings.yaml에서 활성화) |

> ⚠️ **`GH_PAT`는 반드시 Fine-grained PAT로 최소 스코프 발급하세요.** classic PAT의 `repo` 스코프는 계정의 **모든** 저장소에 대한 읽기/쓰기 권한을 부여합니다. 이 토큰이 유출되면(로그 노출, 워크플로 변조 등) 공격자가 워크플로 파일에 시크릿 유출 스텝을 몰래 추가해 `ANTHROPIC_API_KEY`·`YOUTUBE_COOKIES` 등 이 저장소의 다른 모든 시크릿까지 훔쳐갈 수 있습니다. 발급 절차:
> 1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
> 2. Resource owner: 본인 계정, **Repository access: Only select repositories** → `3tv`(카카오 토큰 자동갱신용) + `3tv-reports`(리포트 중계 repo) 두 개만 선택
> 3. Permissions: `3tv`에는 **Secrets: Read and write**만, `3tv-reports`에는 **Contents: Read and write**만 부여 (그 외 전부 No access)
> 4. 위 두 저장소 외에는 접근 권한이 전혀 없으므로, 유출되어도 피해 범위가 이 두 repo로 한정됩니다

> 💡 **Gemini 무료 티어 할당량 주의**: 무료 키는 `gemini-2.5-flash` 기준 **하루 20요청** 제한이 있습니다.
> 이 파이프라인은 그 안에서 돌도록 예산 설계되어 있습니다 — 세션당 최대 4요청(64프레임 ÷ 16장/배치),
> us+kr 하루 운영 최대 8요청, 나머지는 재시도·수동 테스트 여유분. 실패한 요청도 할당량을 소모하므로
> 같은 날 테스트를 반복하면 한도에 걸릴 수 있고, 이때는 남은 배치가 `gemini-2.5-flash-lite`(별도 할당량
> 버킷)로 자동 폴백된 뒤 그마저 소진되면 부분 결과로 진행됩니다. 유료 결제를 활성화하면 이 제약이
> 사실상 사라지며, `config/settings.yaml`의 `frames.vision_*` 값을 상향해도 됩니다.

### 2. 유튜브 쿠키 (권장)

GitHub Actions 같은 데이터센터 IP는 유튜브가 yt-dlp 접근을 차단할 수 있습니다.
1. 크롬 확장 "Get cookies.txt LOCALLY" 등으로 youtube.com 로그인 상태의 cookies.txt 내보내기
2. 파일 내용 전체를 `YOUTUBE_COOKIES` secret에 붙여넣기
3. 차단으로 캡처가 실패하면 텔레그램으로 경고가 오고, 로컬 실행 또는 VOD 재실행으로 복구

### 3. 보유종목 등록

`config/holdings.yaml`에 보유/관심 종목을 채우면 방송 언급 시 리포트에 "💼 보유종목 언급 체크" 섹션으로 강조됩니다.

### 4. 옵시디안 second brain 연동 (Syncthing 구조, S9 마스터 + S26)

볼트(`RaeVault`)는 **Syncthing**(S9 마스터 ⇄ S26 P2P)으로 동기화되므로 볼트 자체를 GitHub에 올리지 않습니다. 대신 중계 repo를 거칩니다:

```
GitHub Actions ──push──▶ 3tv-reports repo (private, 중계용)
                              │ fetch (n8n 스케줄, 07:10/08:50 KST)
                              ▼
        S9: /storage/emulated/0/obsidian/RaeVault/3protv/YYYY/MM/*.md
                              │ Syncthing 자동 전파
                              ▼
                         S26 옵시디안
```

1. GitHub에 **`3tv-reports` private repo** 생성 (빈 repo, README만 있어도 됨)
2. `config/settings.yaml`의 `obsidian.vault_repo`에 `계정명/3tv-reports` 입력
3. `GH_PAT` secret이 이 repo에 Contents:write 권한을 갖는지 확인
4. S9의 n8n에 동기화 워크플로 등록 — **`docs/n8n_s9_sync.md` 가이드 참고** (`docs/n8n_3tv_sync_workflow.json` import 가능)
5. 리포트는 `3protv/YYYY/MM/3protv오늘_YYYYMMDD_키워드.md`로 저장되며 frontmatter(날짜/태그/언급종목)가 있어 Dataview 검색이 가능합니다
6. write 충돌 규칙: n8n의 3tv 워크플로는 볼트 내 `3protv/` 폴더에만 쓰기 (기존 n8n `00_Inbox/` 규칙과 병행 — 새 파일 생성만이라 Syncthing 충돌 없음)

### 5. 카카오 나에게 보내기 (선택)

1. https://developers.kakao.com 에서 앱 생성, 카카오 로그인 + `talk_message` 동의항목 활성화, Redirect URI `http://localhost:8899` 등록
2. 로컬 PC에서 `python scripts/kakao_get_token.py` 실행 → 출력된 값을 Secrets에 등록
3. refresh token(유효 약 2개월)은 파이프라인이 자동 갱신을 시도하며(GH_PAT 필요), 실패 시 텔레그램으로 안내

## 실행

### 자동 (GitHub Actions)
`.github/workflows/us-session.yml`, `kr-session.yml`이 평일 아침 cron으로 자동 실행됩니다.
Actions 탭 → 워크플로 → **Run workflow**로 수동 실행도 가능하며, `vod_url` 입력 시 라이브 대신 과거 영상으로 테스트할 수 있습니다.

### 로컬 (Galaxy Book fallback)
```bat
:: 1회 준비: py -3.11, ffmpeg 설치 후
pip install -r requirements.txt
copy .env.example .env   :: 값 채우기
:: 기존 C:\3protv\.env 가 있으면 자동 인식됩니다 (동일 키 재사용, 복사 불필요)

:: 실행
scripts\run_local.bat us
scripts\run_local.bat kr
```
Windows 작업 스케줄러에 `run_local.bat us`(평일 05:55), `run_local.bat kr`(평일 07:55)를 등록하면 Actions 없이도 동일하게 동작합니다.

### 테스트 (과거 VOD로)
```bash
PYTHONPATH=src python -m threetv.main --session us \
  --vod-url "https://www.youtube.com/watch?v=..." --skip-notify --skip-archive
```
결과물은 `output/YYYYMMDD/us/`에 저장됩니다: 선별된 프레임(`frames/`), `vision_results.json`, `transcript.txt`, `report.md`, `report.json`

### 구간 트리밍 사전검토 (비용 절감)

전체 방송 대신 지정 구간만 다운로드·분석하고 싶을 때 `--trim-start`/`--trim-duration`을 함께 씁니다(분:초 단위, `MM:SS`). yt-dlp가 해당 구간만 정확히 받아오므로 다운로드·분석 비용이 구간 길이에 비례해 줄어듭니다. 결과는 `output/YYYYMMDD/<세션>_trim/`에 저장되어 전체구간 결과(`output/YYYYMMDD/<세션>/`)와 겹치지 않고 나란히 비교할 수 있습니다.

```bash
PYTHONPATH=src python -m threetv.main --session us \
  --vod-url "https://www.youtube.com/live/C4VUZP2h24Y" \
  --trim-start 6:00 --trim-duration 3:00 \
  --skip-notify --skip-archive
```

## 알려진 리스크

1. **유튜브 데이터센터 IP 차단** — 쿠키로 완화, 실패 시 텔레그램 경고 + 로컬/VOD 복구 경로. 첫 1~2주는 실운영 모니터링 권장
2. **GitHub cron 지연** — 스케줄 실행이 40~55분 늦게 시작되는 것이 실측으로 확인됨(2026-07 운영 로그). 이 때문에 cron을 방송 55분 전(05:00/07:00 KST)에 기동하고 스크립트가 방송 시작까지 대기하는 구조. 지연이 55분을 넘는 날은 캡처 실패 → 텔레그램 경고 후 VOD 재실행으로 복구
3. **라이브 시작 시각 변동** — 세션 시작 전부터 종료 시각까지 30초 간격 폴링으로 대응
4. **카카오 refresh token 만료** — 자동 갱신 실패 시 `scripts/kakao_get_token.py`로 재발급
5. **`GH_PAT` 스코프 관리** — classic PAT의 넓은 `repo` 스코프로 발급하면, 토큰 유출 시 이 저장소를 포함한 계정의 모든 저장소가 위험해집니다. 반드시 위 "GitHub Secrets 등록"의 Fine-grained PAT 가이드대로 `3tv` + 볼트 repo 2곳으로만 스코프를 한정하세요

## 면책

본 리포트는 방송 내용 기반 자동 요약이며 투자 참고용입니다. 투자 판단의 책임은 본인에게 있습니다.
