"""축 2 — 진입비용. 한국부동산원 임대료·공실률·권리금을 붙인다.

상권력(축 1)과 합산하지 않는다. 합치면 "매출이 좋아서 B" 인지
"월세가 싸서 B" 인지 구분이 사라진다. 이유는 reference/scoring_rules.md.

서울시 상권(1,650개)과 부동산원 상권(59개)은 쪼개는 방식이 다르다.
그 대응표는 reference/rent_districts.yaml 에 있다.

사용법:
    python scripts/rent.py --status          # 캐시 상태
    python scripts/rent.py --refresh         # 강제 재수집
    python scripts/rent.py --district 성동구  # 해당 자치구 임대료 확인
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"
SOURCE = "한국부동산원 상업용부동산 임대동향조사 (R-ONE)"
LICENSE = "공공누리 제1유형 (출처표시)"


# ── 로딩 ───────────────────────────────────────────────────────

def load_config() -> dict:
    with open(SKILL_ROOT / "config" / "defaults.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_mapping() -> dict:
    with open(SKILL_ROOT / "reference" / "rent_districts.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_key(name: str = "REB_API_KEY") -> str:
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


# ── 수집 ───────────────────────────────────────────────────────

def fetch_table(key: str, statbl: str, cycle: str, page_size: int = 1000) -> list[dict]:
    """통계표 하나를 페이지네이션으로 전부 받는다."""
    rows: list[dict] = []
    page = 1
    while True:
        url = (f"{BASE}?KEY={key}&Type=json&STATBL_ID={statbl}"
               f"&DTACYCLE_CD={cycle}&pIndex={page}&pSize={page_size}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as e:                      # noqa: BLE001
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"{statbl} 호출 실패: {last_err}")

        if "RESULT" in body:
            raise RuntimeError(f"{statbl}: {body['RESULT'].get('MESSAGE')}")
        chunk = next((x["row"] for x in body.get("SttsApiTblData", []) if "row" in x), [])
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return rows


def collect(cfg: dict) -> dict:
    key = load_key()
    ids = load_mapping()["statbl"]
    out = SKILL_ROOT / cfg["paths"]["cache_dir"]
    out.mkdir(parents=True, exist_ok=True)
    summary = {}

    for name, (statbl, cycle) in {
        "rent": (ids["rent"], "QY"),
        "vacancy": (ids["vacancy"], "QY"),
        "premium": (ids["premium"], "YY"),
    }.items():
        print(f"  [{name}] {statbl}")
        rows = fetch_table(key, statbl, cycle)
        df = pd.DataFrame(rows)
        df["DTA_VAL"] = pd.to_numeric(df["DTA_VAL"], errors="coerce")
        df.to_parquet(out / f"reb_{name}.parquet", index=False)
        latest = sorted(df["WRTTIME_IDTFR_ID"].unique())[-1]
        summary[name] = {"statbl": statbl, "rows": len(df), "latest": latest}
        print(f"    {len(df):,} 행 · 최신 {latest}")

    meta = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "license": LICENSE,
        "tables": summary,
    }
    (out / "_reb_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def load_reb(cfg: dict) -> dict | None:
    out = SKILL_ROOT / cfg["paths"]["cache_dir"]
    mp = out / "_reb_meta.json"
    if not mp.exists():
        return None
    data = {"_meta": json.loads(mp.read_text(encoding="utf-8"))}
    for name in ("rent", "vacancy", "premium"):
        p = out / f"reb_{name}.parquet"
        if not p.exists():
            return None
        data[name] = pd.read_parquet(p)
    return data


def ensure(cfg: dict, refresh: bool = False) -> dict:
    """캐시가 없거나 낡았으면 받는다. 서울 상권 데이터와 같은 냉장고 규칙."""
    reb = None if refresh else load_reb(cfg)
    if reb is None:
        print("부동산원 데이터 수집 중...")
        collect(cfg)
        reb = load_reb(cfg)
    return reb


# ── 상권 매칭 ──────────────────────────────────────────────────

def load_seoul_lease() -> dict[str, float]:
    """서울시 상가임대차 실태조사 — 상권별 실측 통상임대료 (만원/㎡·월).

    부동산원은 서울 주요상권 59곳만 조사하지만, 이 자료는 140개 상권
    1층 점포 12,531개를 실측했다. 골목 안쪽까지 훨씬 촘촘하다.

    원본이 스캔 이미지 PDF라 Document Parse + Solar 로 뽑았다.
    (scripts/parse_lease_pdf.py) 평균값만 쓴다 — 중위수는 OCR 정렬 오류가 섞여 있다.

    ※ 통상임대료 = 보증금×12%/12 + 월세 + 공용관리비.
       순수 월세인 부동산원 값보다 크며, 실제 부담에 더 가깝다.
    """
    import csv

    path = SKILL_ROOT / "reference" / "seoul_lease_districts.csv"
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = re.sub(r"\([0-9]층\)", "", r["상권명"] or "").strip().replace(" ", "")
            floor = r["상권명"] or ""
            if not name:
                continue
            # 1층 우선. 층 표기가 없으면 그대로 채택.
            if "1층" in floor or "층" not in floor:
                try:
                    out[name] = float(r["평균"])
                except (TypeError, ValueError):
                    pass
    return out


def match_seoul_lease(trdar_nm: str, table: dict[str, float]) -> tuple[float | None, str | None]:
    """서울시 실측 임대료를 상권명으로 붙인다. 정확 일치 → 부분 일치 순."""
    if not table or not trdar_nm:
        return None, None
    key = trdar_nm.replace(" ", "")
    if key in table:
        return table[key], trdar_nm
    for nm, val in table.items():
        if nm and (nm in key or key in nm):
            return val, nm
    return None, None


def match_reb_district(trdar_nm: str, sigungu: str, mapping: dict, available: set) -> tuple[str | None, str]:
    """서울시 상권 → 부동산원 상권. (상권명, 매칭근거) 를 돌려준다."""
    aliases = mapping.get("name_aliases", {})
    name = (trdar_nm or "").replace(" ", "")

    # 1) 상권명 직접 매칭 — '뚝섬역' → '뚝섬', '홍대입구' → '홍대/합정'
    for reb_nm in available:
        keys = [reb_nm] + aliases.get(reb_nm, [])
        if any(k.replace(" ", "") in name for k in keys):
            return reb_nm, "상권명 일치"

    # 2) 자치구 대표 상권 — '성수역'(성동구) → '뚝섬'
    cands = mapping["districts"].get(sigungu, [])
    for c in cands:
        if c in available:
            return c, f"{sigungu} 대표 상권"

    # 3) 부동산원 상권이 없는 자치구 → 서울 평균
    return None, "서울 평균"


def load_cost_ratio(cfg: dict, biz_code: str) -> dict | None:
    """업종의 임대료제외 원가율. 외식업 10종만 있고 그 밖은 None.

    KREI 「외식업체 경영실태 조사」는 임차료를 별도 항목으로 조사한다.
    그래서 임차료 칸만 우리 실측값으로 갈아끼우면 보정 없이 영업이익이 나온다.
    """
    with open(SKILL_ROOT / "reference" / "service_codes.yaml", encoding="utf-8") as f:
        krei_map = yaml.safe_load(f).get("krei_map", {})
    name = krei_map.get(biz_code)
    if not name:
        return None
    table = pd.read_csv(SKILL_ROOT / cfg["profit"]["cost_table"], encoding="utf-8-sig")
    row = table[table["구분"].str.strip() == name]
    if row.empty:
        return None
    r = row.iloc[0]
    ex_rent = float(r["임대료제외_원가율"]) / 100
    return {
        "krei_name": name,
        "ex_rent_cost_ratio": ex_rent,      # 임차료를 뺀 원가율
        "breakeven_burden": 1 - ex_rent,    # 이 임대료율을 넘으면 적자
        "food_cost": float(r["식재료비"]) / 100,
        "labor_cost": float(r["인건비계"]) / 100,
        "krei_rent": float(r["임차료"]) / 100,
    }


def attach(df: pd.DataFrame, cfg: dict, reb: dict, biz_code: str | None = None) -> pd.DataFrame:
    """상권력 테이블에 임대료·공실률·부담률·진입비용 등급·영업이익을 붙인다."""
    mapping = load_mapping()
    ec = cfg["entry_cost"]
    area_sqm = ec["assumed_area_sqm"]
    cost = load_cost_ratio(cfg, biz_code) if biz_code else None

    # 서울 상권만, 최신 분기만
    rent = reb["rent"]
    vac = reb["vacancy"]
    latest_q = sorted(rent["WRTTIME_IDTFR_ID"].unique())[-1]

    seoul_ids = set(mapping_ids(mapping))
    r_now = rent[rent["WRTTIME_IDTFR_ID"] == latest_q]
    v_now = vac[vac["WRTTIME_IDTFR_ID"] == latest_q]

    rent_by = dict(zip(r_now["CLS_NM"], r_now["DTA_VAL"]))
    vac_by = dict(zip(v_now["CLS_NM"], v_now["DTA_VAL"]))
    available = {n for n in rent_by if n in seoul_ids}

    # 서울 평균 (부동산원 상권이 없는 자치구 대체값)
    seoul_rent_avg = pd.Series([rent_by[n] for n in available]).mean()
    seoul_vac_avg = pd.Series([vac_by.get(n) for n in available]).dropna().mean()

    seoul_tbl = load_seoul_lease()
    matched, why, rent_v, vac_v = [], [], [], []
    for _, row in df.iterrows():
        trdar = row.get("TRDAR_CD_NM")
        # 공실률은 부동산원만 있으므로 상권 매칭은 항상 해둔다
        nm, reason = match_reb_district(trdar, row.get("SIGNGU_CD_NM"), mapping, available)
        vac_v.append(vac_by.get(nm, seoul_vac_avg) if nm else seoul_vac_avg)

        # 임대료는 서울시 실측(140개 상권)을 우선한다. 없으면 부동산원(59개)으로 폴백.
        s_rent, s_nm = match_seoul_lease(trdar, seoul_tbl)
        if s_rent is not None:
            matched.append(s_nm)
            why.append("서울시 실측")
            rent_v.append(s_rent * 10)          # 만원/㎡ → 천원/㎡ (부동산원 단위에 맞춤)
        else:
            matched.append(nm or "-")
            why.append(reason)
            rent_v.append(rent_by.get(nm, seoul_rent_avg) if nm else seoul_rent_avg)

    df = df.copy()
    df["reb_district"] = matched
    df["reb_match_reason"] = why
    df["rent_per_sqm"] = rent_v                       # 천원/㎡ · 월
    df["vacancy_rate"] = vac_v                        # %
    df["monthly_rent"] = df["rent_per_sqm"] * 1000 * area_sqm
    df["rent_burden"] = df["monthly_rent"] / df["monthly_sales_per_store"]

    # 중/고 경계는 업종별 손익분기 임대료율. 원가율을 모르는 업종은 서울 평균만 쓴다.
    lo = ec["low_max"]
    hi = cost["breakeven_burden"] if cost else lo
    lo = min(lo, hi)          # 치킨·양식처럼 손익분기가 서울 평균보다 낮으면 '중'을 없앤다
    cap = ec["max_valid_burden"]

    def band(x):
        if pd.isna(x):
            return "판정불가"
        if x > cap:
            # 자치구 평균 임대료를 저매출 골목 점포에 붙인 결과다. 등급을 내지 않는다.
            return "판정보류"
        return "저" if x < lo else ("고" if x > hi else "중")

    df["entry_cost"] = df["rent_burden"].map(band)
    df["breakeven_burden"] = hi
    df["rent_quarter"] = latest_q

    # ── 영업이익 (외식업만) ──
    if cost:
        margin = 1 - cost["ex_rent_cost_ratio"] - df["rent_burden"]
        df["profit_margin"] = margin
        df["monthly_profit"] = df["monthly_sales_per_store"] * margin
        df["ex_rent_cost_ratio"] = cost["ex_rent_cost_ratio"]
        df["food_cost"] = cost["food_cost"]
        df["labor_cost"] = cost["labor_cost"]
        # 세금·공과 + 기타경비 = 임대료제외 원가에서 식재료·인건비를 뺀 나머지
        df["etc_cost"] = cost["ex_rent_cost_ratio"] - cost["food_cost"] - cost["labor_cost"]
        df["krei_name"] = cost["krei_name"]
    else:
        for c in ("profit_margin", "monthly_profit", "ex_rent_cost_ratio",
                  "food_cost", "labor_cost", "etc_cost"):
            df[c] = pd.NA
        df["krei_name"] = None
    return df


def mapping_ids(mapping: dict) -> list[str]:
    """매핑 파일에 등록된 서울 상권 이름 전체."""
    out = []
    for v in mapping["districts"].values():
        out.extend(v)
    return out


# ── 4처방 ──────────────────────────────────────────────────────

PRESCRIPTION = {
    ("높음", "낮음"): ("🟢 노려라", "상권력이 좋은데 진입비용까지 낮다. 드문 조합이다."),
    ("높음", "높음"): ("🟡 자금력 싸움", "장사는 되지만 임대료가 무겁다. 협상이 승부처다."),
    ("낮음", "낮음"): ("🔵 저리스크 실험", "기대 매출은 낮지만 실패 비용이 작다."),
    ("낮음", "높음"): ("🔴 피하라", "매출은 낮은데 고정비가 무겁다."),
}


def prescribe(grade: str, cost_band: str) -> tuple[str, str]:
    """두 축을 조합해 처방을 낸다.

    진입비용을 못 재도 상권력은 살아 있다. 있는 정보까지 버리지 않는다.
    """
    if grade in ("표본부족", "판정불가"):
        return "판정 보류", "점포 수가 적어 평균이 대표성을 갖지 못한다."
    if cost_band in ("판정불가", "판정보류"):
        power = "좋은 편" if grade in ("A", "B") else "낮은 편"
        return (
            f"⚪ 상권력만 판정 ({grade})",
            f"상권력은 {power}이나, 이 상권은 임대료 조사 대상이 아니라 진입비용을 판정하지 않았다.",
        )
    power = "높음" if grade in ("A", "B") else "낮음"
    cost = "높음" if cost_band == "고" else "낮음"
    return PRESCRIPTION[(power, cost)]


def premium_for(reb: dict, biz_code: str, mapping: dict) -> dict | None:
    """서울 + 해당 업종 대분류의 평균 권리금."""
    pm = mapping["premium_industry"]
    industry = next(
        (v for k, v in pm["prefix_map"].items() if (biz_code or "").startswith(k)),
        pm["default"],
    )
    p = reb["premium"]
    latest = sorted(p["WRTTIME_IDTFR_ID"].unique())[-1]
    sel = p[
        (p["WRTTIME_IDTFR_ID"] == latest)
        & (p["GRP_NM"] == "서울")
        & (p["CLS_NM"].str.strip() == industry.strip())
    ]
    if sel.empty:
        return None
    out = {"year": latest, "industry": industry}
    for _, r in sel.iterrows():
        out[r["ITM_NM"]] = r["DTA_VAL"]
    return out


# ── CLI ────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="부동산원 임대료·공실률·권리금")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--district", help="자치구 (예: 성동구)")
    args = ap.parse_args()

    cfg = load_config()
    if args.status:
        reb = load_reb(cfg)
        if not reb:
            print("부동산원 캐시 없음")
            return
        m = reb["_meta"]
        print(f"수집 {m['fetched_at']} · 출처 {m['source']}")
        for k, v in m["tables"].items():
            print(f"  {k:<9} {v['rows']:>6,}행  최신 {v['latest']}  [{v['statbl']}]")
        return

    reb = ensure(cfg, args.refresh)
    mapping = load_mapping()
    rent = reb["rent"]
    latest = sorted(rent["WRTTIME_IDTFR_ID"].unique())[-1]
    now = rent[rent["WRTTIME_IDTFR_ID"] == latest]
    rent_by = dict(zip(now["CLS_NM"], now["DTA_VAL"]))

    names = mapping["districts"].get(args.district) if args.district else mapping_ids(mapping)
    print(f"기준 분기 {latest} · 33㎡ 환산\n")
    print(f"{'부동산원 상권':<14}{'㎡당(천원)':>11}{'월 임대료':>13}")
    for n in names or []:
        if n in rent_by:
            print(f"{n:<14}{rent_by[n]:>11.1f}{rent_by[n]*1000*33/10000:>11,.0f}만원")


if __name__ == "__main__":
    main()
