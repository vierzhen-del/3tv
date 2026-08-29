"""라이브 n8n 워크플로의 노드 하나를 저장소 docs/*.json 원본으로 원자적으로 갱신.

n8n 웹 UI에서 노드를 열어 코드를 수동으로 복사·붙여넣기 하면 부분 수정
실수가 나기 쉽다(2026-08-14 사용자 지적 — "부분수정은 오류발생 될 수 있어서").
이 스크립트는 REST API로 라이브 워크플로 전체를 받아 지정한 노드의
parameters만 저장소 원본으로 교체한 뒤 통째로 되돌려써 부분 수정을 없앤다.

사전 준비 — n8n 웹 UI: Settings → n8n API → Create an API key (최초 1회).
발급된 키는 화면에 붙여넣지 말고 로컬 환경변수로만 쓴다.

사용법 (S9 proot Ubuntu, 3tv 리포지토리 루트에서):
    N8N_API_KEY=<발급받은 키> python3 scripts/n8n_update_node.py \\
        --workflow-name "3protv 오늘 종합 다이제스트 → 텔레그램" \\
        --node-id buildprompt \\
        --source docs/n8n_daily_digest_workflow.json \\
        --dry-run          # 먼저 변경 내용만 확인

--dry-run 없이 다시 실행하면 실제로 PUT까지 반영된다.

환경변수:
    N8N_API_KEY   필수. n8n API 키.
    N8N_BASE_URL  기본값 http://localhost:5678
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow-name", required=True,
                     help="갱신할 n8n 워크플로 이름 (정확히 일치해야 함)")
    ap.add_argument("--node-id", required=True,
                     help="갱신할 노드의 id (저장소 JSON의 노드 id 기준)")
    ap.add_argument("--source", required=True,
                     help="저장소 내 워크플로 JSON 경로 (source of truth)")
    ap.add_argument("--dry-run", action="store_true",
                     help="실제로 PUT하지 않고 변경 여부·글자수 차이만 출력")
    args = ap.parse_args()

    api_key = os.environ.get("N8N_API_KEY")
    if not api_key:
        print("N8N_API_KEY 환경변수가 없습니다 — n8n 웹 UI Settings → n8n API 에서 "
              "발급 후 `N8N_API_KEY=... python3 scripts/n8n_update_node.py ...`로 실행하세요.",
              file=sys.stderr)
        return 1
    base = os.environ.get("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
    headers = {"X-N8N-API-KEY": api_key}

    with open(args.source, encoding="utf-8") as f:
        source_doc = json.load(f)
    source_node = next((n for n in source_doc["nodes"] if n["id"] == args.node_id), None)
    if source_node is None:
        ids = [n["id"] for n in source_doc["nodes"]]
        print(f"{args.source}에 id={args.node_id} 노드가 없습니다. 실제 id 목록: {ids}",
              file=sys.stderr)
        return 1

    resp = requests.get(f"{base}/api/v1/workflows", headers=headers, timeout=30)
    resp.raise_for_status()
    workflows = resp.json().get("data", [])
    match = next((w for w in workflows if w.get("name") == args.workflow_name), None)
    if match is None:
        names = [w.get("name") for w in workflows]
        print(f"워크플로 '{args.workflow_name}'를 못 찾았습니다. n8n에 있는 이름: {names}",
              file=sys.stderr)
        return 1

    resp = requests.get(f"{base}/api/v1/workflows/{match['id']}", headers=headers, timeout=30)
    resp.raise_for_status()
    live = resp.json()

    live_node = next(
        (n for n in live["nodes"]
         if n.get("id") == args.node_id or n.get("name") == source_node.get("name")),
        None,
    )
    if live_node is None:
        print(f"라이브 워크플로에 id={args.node_id}(name={source_node.get('name')}) "
              f"노드가 없습니다 — docs와 라이브가 구조적으로 어긋나 있을 수 있습니다.",
              file=sys.stderr)
        return 1

    old_params = live_node.get("parameters", {})
    new_params = source_node.get("parameters", {})
    if old_params == new_params:
        print("이미 최신 상태입니다 — 변경 없음.")
        return 0

    old_code = old_params.get("jsCode", "")
    new_code = new_params.get("jsCode", "")
    print(f"노드 '{live_node.get('name')}' (id={live_node.get('id')}) 갱신 예정: "
          f"jsCode {len(old_code)}자 → {len(new_code)}자")

    if args.dry_run:
        print("--dry-run: 실제 PUT은 생략했습니다. 이 결과가 맞으면 --dry-run 빼고 다시 실행하세요.")
        return 0

    live_node["parameters"] = new_params
    # PUT은 보통 nodes/connections/name/settings만 받고 id·타임스탬프·active는
    # 거부하거나 무시하는 n8n 버전이 많다 — 안전하게 핵심 필드만 보낸다.
    payload = {
        "name": live["name"],
        "nodes": live["nodes"],
        "connections": live["connections"],
        "settings": live.get("settings", {}),
    }
    resp = requests.put(f"{base}/api/v1/workflows/{match['id']}", headers=headers,
                         json=payload, timeout=30)
    if not resp.ok:
        print(f"PUT 실패 {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1
    print("갱신 완료 — n8n 웹 UI에서 노드를 열어 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
