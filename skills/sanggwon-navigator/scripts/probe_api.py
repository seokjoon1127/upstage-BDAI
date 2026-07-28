"""서울 열린데이터광장 Open API 탐침.

서비스명이 실제로 존재하는지 확인하고 전체 건수와 컬럼명을 덤프한다.
서비스명·컬럼명을 기억으로 쓰지 않기 위한 스크립트. 전체 수집 전에 한 번만 돌린다.

사용법:
    python scripts/probe_api.py                    # 후보 전체 탐침
    python scripts/probe_api.py VwsmTrdarSelngQq   # 특정 서비스만
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = "http://openapi.seoul.go.kr:8088"

# 후보 서비스명. 살아있는 것만 골라내면 된다.
CANDIDATES = {
    "영역-상권": ["TbgisTrdarRelm", "VwsmTrdarRelm"],
    "추정매출-상권": ["VwsmTrdarSelngQq"],
    "점포-상권": ["VwsmTrdarStorQq"],
    "길단위인구-상권": ["VwsmTrdarFlpopQq"],
    "상권변화지표-상권": ["VwsmTrdarIxQq"],
    "상주인구-상권": ["VwsmTrdarRepopQq"],
    "직장인구-상권": ["VwsmTrdarWrcPopltnQq"],
    "소득소비-상권": ["VwsmTrdarIncmQq"],
}


def load_key(name: str = "SEOUL_API_KEY") -> str:
    """인증키를 읽는다. 환경변수 → .env 파일 순.

    Timely 등 배포 환경은 환경변수로 주입하므로 그쪽을 먼저 본다.
    키를 사용자에게 묻거나 화면에 출력하지 않는다.
    """
    val = os.environ.get(name)
    if val:
        return val.strip()
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(name + "="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    if key:
                        return key
    sys.exit(
        name + " 가 없습니다. "
        "Timely 는 [설정 > 환경변수] 에, 로컬은 프로젝트 루트 .env 에 등록하세요. "
        "키를 채팅창에 붙여넣지 마세요."
    )


def probe(key: str, service: str) -> dict:
    """1건만 요청해서 서비스 생존 여부·총건수·컬럼명을 확인한다."""
    url = f"{BASE}/{key}/json/{service}/1/1/"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "reason": f"요청 실패: {e}"}

    # 에러 응답은 최상위에 RESULT 만 들어온다
    if "RESULT" in body:
        r = body["RESULT"]
        return {"ok": False, "reason": f"{r.get('CODE')} {r.get('MESSAGE')}"}

    payload = next(iter(body.values()))
    result = payload.get("RESULT", {})
    if result.get("CODE") not in ("INFO-000", None):
        return {"ok": False, "reason": f"{result.get('CODE')} {result.get('MESSAGE')}"}

    rows = payload.get("row") or []
    return {
        "ok": True,
        "total": payload.get("list_total_count"),
        "columns": list(rows[0].keys()) if rows else [],
        "sample": rows[0] if rows else {},
    }


def main() -> None:
    key = load_key()
    targets = (
        {"(직접 지정)": sys.argv[1:]} if len(sys.argv) > 1 else CANDIDATES
    )

    alive = {}
    for label, services in targets.items():
        for service in services:
            res = probe(key, service)
            if res["ok"]:
                alive[label] = service
                print(f"\n{'=' * 70}")
                print(f"✅ {label}  →  {service}")
                print(f"   총 {res['total']:,} 건")
                print(f"   컬럼 {len(res['columns'])}개:")
                for k, v in res["sample"].items():
                    print(f"     {k:<28} = {v}")
                break
            print(f"❌ {label}  →  {service}  ({res['reason']})")

    print(f"\n{'=' * 70}")
    print(f"살아있는 서비스 {len(alive)}개")
    for label, service in alive.items():
        print(f"  {label}: {service}")


if __name__ == "__main__":
    main()
