"""지원제도 매칭 — 필터가 아니라 분류.

사용자에게 나이·성별·창업경력을 묻지 않는다. 조건별로 갈라서 전부 보여준다.
그래서 답이 없어도 리포트는 완결되고, 자동 실행에서도 그대로 돈다.

정책은 캐시하지 않는다. "마감 D-12"가 셀링포인트인데 캐시하면 날짜가 화석이 된다.

사용법:
    python scripts/match_policy.py --district 성동구
    python scripts/match_policy.py --district 성동구 --zone 전통시장
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
SOURCE = "기업마당 (bizinfo.go.kr) 지원사업정보"


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_key(name: str = "BIZINFO_API_KEY") -> str:
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


def fetch_policies(count: int = 1500) -> list[dict]:
    url = f"{API}?crtfcKey={urllib.parse.quote(load_key())}&dataType=json&searchCnt={count}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if "jsonArray" not in body:
                raise RuntimeError(body.get("reqErr", "예상치 못한 응답"))
            return body["jsonArray"]
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"기업마당 호출 실패: {e}") from e
    return []


# ── 필터 ───────────────────────────────────────────────────────

def _haystack(p: dict) -> str:
    """키워드를 찾을 텍스트를 한 덩어리로 만든다. HTML 태그는 제거."""
    raw = " ".join(
        str(p.get(f, "")) for f in ("pblancNm", "hashtags", "bsnsSumryCn", "trgetNm")
    )
    return re.sub(r"<[^>]+>", " ", raw)


SEOUL_DISTRICTS = [
    "종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구",
    "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구",
    "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구",
]
_TITLE_TAG = re.compile(r"^\s*\[([^\]]+)\]")


def _other_region_tagged(p: dict, kw: dict) -> bool:
    """공고명이 [경기] 처럼 타 지역 태그로 시작하는가."""
    m = _TITLE_TAG.match(p.get("pblancNm") or "")
    if not m:
        return False
    tag = m.group(1)
    return any(tag.startswith(x) for x in kw["region"]["exclude_title_tags"])


def _other_district_only(p: dict, district: str | None) -> bool:
    """다른 자치구만 지목하고 우리 자치구는 언급하지 않는 공고인가."""
    if not district:
        return False
    text = _haystack(p)
    mentioned = {d for d in SEOUL_DISTRICTS if d in text}
    return bool(mentioned) and district not in mentioned


def filter_region(policies: list[dict], kw: dict, district: str | None) -> list[dict]:
    r = kw["region"]
    allowed = set(r["nationwide_institutions"]) | set(r["seoul_institutions"])
    check_district = r.get("district_exclusive_check", False)
    out = []
    for p in policies:
        if _other_region_tagged(p, kw):
            continue
        if check_district and _other_district_only(p, district):
            continue
        inst = (p.get("jrsdInsttNm") or "").strip()
        if inst in allowed:
            p["_scope"] = "서울" if inst in r["seoul_institutions"] else "전국"
            out.append(p)
        elif district and district in _haystack(p):
            # 소관기관이 목록 밖이어도 해당 자치구를 명시하면 포함
            p["_scope"] = district
            out.append(p)
    return out


def filter_relevance(policies: list[dict], kw: dict) -> list[dict]:
    rel = kw["relevance"]
    out = []
    for p in policies:
        text = _haystack(p)
        if any(x in text for x in rel["exclude_any"]):
            continue
        if not any(x in text for x in rel["include_any"]):
            continue
        out.append(p)
    return out


def parse_deadline(p: dict, kw: dict) -> dict:
    """신청기간 문자열을 파싱해 D-day 를 계산한다."""
    raw = (p.get(kw["deadline"]["field"]) or "").strip()
    today = date.today()
    m = re.findall(r"(\d{4})[-.](\d{2})[-.](\d{2})", raw)
    if len(m) < 2:
        return {"raw": raw or "-", "days_left": None, "open": True, "label": kw["deadline"]["always_open_label"]}
    end = date(*map(int, m[-1]))
    start = date(*map(int, m[0]))
    days = (end - today).days
    is_open = start <= today <= end
    if days < 0:
        label = "마감"
    elif days <= kw["deadline"]["urgent_days"]:
        label = f"마감 D-{days} ⚠️"
    else:
        label = f"마감 D-{days}"
    return {"raw": raw, "days_left": days, "open": is_open, "label": label}


def classify(policies: list[dict], kw: dict, zone_type: str | None) -> dict:
    """조건별 분기로 나눈다. 어디에도 안 걸리면 '조건 없이 신청 가능'."""
    branches = kw["branches"]
    result: dict[str, list] = {name: [] for name in branches}

    for p in policies:
        text = _haystack(p)
        matched = []
        for name, spec in branches.items():
            hit = any(k in text for k in spec.get("keywords") or [])
            # 연령처럼 숫자가 공고마다 바뀌는 조건은 정규식으로 잡는다
            if not hit:
                hit = any(re.search(pat, text) for pat in spec.get("regex") or [])
            if hit:
                matched.append(name)

        # 지정구역 분기는 해당 상권이 실제로 지정구역일 때만 살린다
        if "designated_zone" in matched and zone_type not in ("전통시장",):
            matched.remove("designated_zone")

        for name in (matched or ["unconditional"]):
            result[name].append(p)

    return result


def sort_key(p: dict, kw: dict) -> tuple:
    """소상공인 직결 → 서울 사업 → 지원분야 → 마감 임박 순."""
    rel = kw["relevance"]
    text = _haystack(p)
    core = 0 if any(k in text for k in rel.get("core_any", [])) else 1
    scope = 0 if p.get("_scope") == "서울" else 1
    field = p.get("pldirSportRealmLclasCodeNm", "기타")
    d = p["_deadline"]["days_left"]
    return (core, scope, rel["field_priority"].get(field, 99), d if d is not None else 999)


# ── 메인 ───────────────────────────────────────────────────────

def match(district: str, zone_type: str | None = None, top_n: int = 10) -> dict:
    kw = load_yaml(SKILL_ROOT / "reference" / "policy_keywords.yaml")
    cfg = load_yaml(SKILL_ROOT / "config" / "defaults.yaml")

    raw = fetch_policies()
    regional = filter_region(raw, kw, district)
    relevant = filter_relevance(regional, kw)

    for p in relevant:
        p["_deadline"] = parse_deadline(p, kw)
    if cfg["policy"]["only_open"]:
        relevant = [p for p in relevant if p["_deadline"]["open"]]

    branches = classify(relevant, kw, zone_type)
    for name in branches:
        branches[name] = sorted(branches[name], key=lambda p: sort_key(p, kw))[:top_n]

    return {
        "district": district,
        "zone_type": zone_type,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "source": SOURCE,
        "total_fetched": len(raw),
        "after_region": len(regional),
        "after_relevance": len(relevant),
        "branches": branches,
        "labels": {n: s["label"] for n, s in kw["branches"].items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="지원제도 조건별 분기 매칭")
    ap.add_argument("--district", required=True, help="자치구 (예: 성동구)")
    ap.add_argument("--zone", help="상권 구분 (예: 전통시장)")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    r = match(args.district, args.zone, args.top)
    print(f"기준일 {r['as_of']} · 출처 {r['source']}")
    print(f"전체 {r['total_fetched']}건 → 지역 {r['after_region']}건 → 관련 {r['after_relevance']}건 (접수중)\n")

    for name, items in r["branches"].items():
        if not items:
            continue
        print(f"【{r['labels'][name]}】 {len(items)}건")
        for p in items:
            print(f"  · [{p.get('pldirSportRealmLclasCodeNm','')}] {p.get('pblancNm','')[:58]}")
            print(f"      {p.get('jrsdInsttNm','')} | {p['_deadline']['label']}")
        print()


if __name__ == "__main__":
    main()
