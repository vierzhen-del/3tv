"""카카오 '나에게 보내기' 최초 토큰 발급 헬퍼 (로컬 PC에서 1회 실행).

사전 준비 (https://developers.kakao.com):
1. 애플리케이션 생성 → REST API 키 확인
2. 앱 설정 > 플랫폼 > Web 사이트 도메인: http://localhost 등록
3. 제품 설정 > 카카오 로그인 활성화, Redirect URI: http://localhost:8899 등록
4. 동의항목 > "카카오톡 메시지 전송(talk_message)" 활성화

실행: python scripts/kakao_get_token.py
→ 브라우저에서 카카오 로그인/동의 → 출력된 KAKAO_REFRESH_TOKEN을
  GitHub Secrets와 로컬 .env에 등록
"""
import http.server
import threading
import urllib.parse
import webbrowser

import requests

REDIRECT_PORT = 8899
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"

auth_code_holder: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [None])[0]
        auth_code_holder["code"] = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>인증 완료. 이 창을 닫고 터미널을 확인하세요.</h2>".encode())

    def log_message(self, *args):
        pass


def main():
    rest_key = input("카카오 REST API 키: ").strip()

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    auth_url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={rest_key}&redirect_uri={REDIRECT_URI}"
        "&response_type=code&scope=talk_message"
    )
    print(f"\n브라우저에서 카카오 로그인을 진행하세요:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("인증 대기 중...")
    while "code" not in auth_code_holder:
        pass
    code = auth_code_holder["code"]
    if not code:
        print("❌ 인증 코드를 받지 못함")
        return

    resp = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": rest_key,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=30,
    )
    data = resp.json()
    if "refresh_token" not in data:
        print(f"❌ 토큰 발급 실패: {data}")
        return

    print("\n✅ 발급 완료. 아래 값을 GitHub Secrets와 .env에 등록하세요:\n")
    print(f"KAKAO_REST_API_KEY={rest_key}")
    print(f"KAKAO_REFRESH_TOKEN={data['refresh_token']}")
    print("\n(refresh token 유효기간 약 2개월 — 파이프라인이 자동 갱신을 시도하며,")
    print(" 갱신 실패 시 텔레그램으로 재발급 안내가 전송됩니다)")


if __name__ == "__main__":
    main()
