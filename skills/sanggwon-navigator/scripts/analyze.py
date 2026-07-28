"""파생지표 계산 + 상권력 등급 판정.

축 1(상권력)만 담당한다. 축 2(진입비용)는 rent.py 에서 따로 낸다.
두 축을 합치지 않는 이유는 reference/scoring_rules.md 에 적혀 있다.

가중치·컷오프는 config/defaults.yaml 에서 읽는다. 이 파일에 숫자를 박지 않는다.

사용법:
    python scripts/analyze.py --biz 카페                 # 서울 전체 랭킹 상위 20
    python scripts/analyze.py --biz 카페 --region 성수동   # 특정 지역 진단
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

METRICS = [
    "sales_growth",
    "footfall_conversion",
    "competition_density",
    "survival_signal",
    "lifecycle",
]


# ── 로딩 ───────────────────────────────────────────────────────

def load_config() -> dict:
    with open(SKILL_ROOT / "config" / "defaults.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_service_codes() -> dict:
    with open(SKILL_ROOT / "reference" / "service_codes.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_cache(cfg: dict) -> dict:
    cache = SKILL_ROOT / cfg["paths"]["cache_dir"]
    meta_path = cache / "_meta.json"
    if not meta_path.exists():
        sys.exit("캐시가 없습니다. 먼저 python scripts/fetch_data.py 를 실행하세요.")
    data = {"_meta": json.loads(meta_path.read_text(encoding="utf-8"))}
    for name in ("area", "sales", "store", "footfall", "change"):
        data[name] = pd.read_parquet(cache / f"{name}.parquet")
    return data


# ── 입력 정규화 ────────────────────────────────────────────────

def resolve_biz(name: str, sales: pd.DataFrame) -> dict:
    """업종명 → 업종코드. 판단 근거를 함께 돌려준다."""
    codes = load_service_codes()
    catalog = (
        sales[["SVC_INDUTY_CD", "SVC_INDUTY_CD_NM"]]
        .drop_duplicates()
        .set_index("SVC_INDUTY_CD")["SVC_INDUTY_CD_NM"]
        .to_dict()
    )
    key = name.strip()

    # 1) 애매한 업종 — 기본값으로 진행하되 대안을 함께 표기
    amb = codes.get("ambiguous", {}).get(key)
    if amb:
        return {
            "code": amb["default"],
            "name": catalog.get(amb["default"], "?"),
            "note": amb["reason"].strip(),
            "alternative": (
                {"code": amb["alternative"], "name": catalog.get(amb["alternative"], "?")}
                if amb.get("alternative")
                else None
            ),
        }

    # 2) 별칭 사전
    code = codes.get("aliases", {}).get(key)
    if code:
        return {"code": code, "name": catalog.get(code, "?"), "note": None, "alternative": None}

    # 3) 데이터의 업종명과 직접 일치 / 부분 일치
    for c, n in catalog.items():
        if n == key:
            return {"code": c, "name": n, "note": None, "alternative": None}
    partial = [(c, n) for c, n in catalog.items() if key in n or n in key]
    if len(partial) == 1:
        c, n = partial[0]
        return {"code": c, "name": n, "note": f"'{key}' → '{n}' 로 해석", "alternative": None}

    return {
        "code": None,
        "name": None,
        "note": f"'{key}' 에 해당하는 업종을 찾지 못했습니다.",
        "candidates": [n for _, n in partial[:5]] or sorted(catalog.values())[:10],
    }


REGION_COLS = ["TRDAR_CD", "TRDAR_CD_NM", "TRDAR_SE_CD_NM", "SIGNGU_CD_NM", "ADSTRD_CD_NM"]


def resolve_region(name: str, area: pd.DataFrame) -> pd.DataFrame:
    """지역명 → 후보 상권.

    세 가지 문제를 함께 푼다.

    1) 서울 행정동 398개 중 256개(64%)가 '성수1가1동' 처럼 숫자로 쪼개져 있다.
       사람들은 '성수동' 이라 부르므로, 못 찾으면 어간('성수')으로 다시 찾는다.
    2) 상권명에 우연히 글자가 겹쳐 엉뚱한 게 딸려온다.
       ('대학동' → 전국 대학교 이름이 든 상권 14개 구) → 행정동 매칭을 우선한다.
    3) 같은 이름의 동이 여러 자치구에 있다. ('신사동' → 강남구·은평구)
       → 띄어쓴 단어를 각각 찾아 교집합을 취한다. "강남 신사동" 이면 강남구 것만.
    """
    tokens = [t for t in name.strip().split() if t]
    if not tokens:
        return area.iloc[0:0][REGION_COLS].copy()

    def token_mask(k: str) -> pd.Series:
        """정확한 것부터 순서대로 시도한다. 하나 걸리면 거기서 멈춘다.

        느슨한 부분일치를 먼저 하면 엉뚱한 게 딸려온다.
        ('창동' 은 '평창동'·'북창동' 의 부분 문자열이기도 하다)
        """
        k = k.replace(" ", "")

        # 1) 자치구 ('송파구' 가 '송파1동' 으로 좁혀지지 않게 먼저)
        if k.endswith("구"):
            gu = area["SIGNGU_CD_NM"] == k
            if gu.any():
                return gu

        # 2) 행정동 완전 일치 — '명동'(중구), '신사동'(강남·관악)
        adm = area["ADSTRD_CD_NM"] == k
        if adm.any():
            return adm

        # 3) 쪼개진 행정동 — '어간 + 숫자 + (가N) + 동' 형태만
        #    '창동' → 창1~5동 ○ / 창신동·평창동 ✕
        #    '성수동' → 성수1가1동 ○ / '금호동' → 금호1가동 ○
        if k.endswith("동") and len(k) >= 2:
            split = area["ADSTRD_CD_NM"].str.match(
                rf"^{re.escape(k[:-1])}\d+(가\d*)?동$", na=False
            )
            if split.any():
                return split

        # 4) 행정동 부분 일치
        adm = area["ADSTRD_CD_NM"].str.contains(k, na=False, regex=False)
        if adm.any():
            return adm

        # 5) 자치구·상권명 부분 일치 — '홍대', '가로수길' 처럼 행정동이 아닌 통칭
        gu = area["SIGNGU_CD_NM"].str.contains(k, na=False, regex=False)
        nm = area["TRDAR_CD_NM"].str.replace(" ", "", regex=False).str.contains(
            k, na=False, regex=False
        )
        return gu | nm

    masks = [token_mask(t) for t in tokens]
    combined = masks[0]
    for m in masks[1:]:
        combined = combined & m
    if not combined.any():
        combined = masks[-1]      # 교집합이 비면 가장 구체적인 말 하나로 되돌린다
    return area[combined][REGION_COLS].copy()


# ── 지표 계산 ──────────────────────────────────────────────────

def build_metrics(cache: dict, biz_code: str, cfg: dict) -> pd.DataFrame:
    """해당 업종에 대해 서울 전체 상권의 지표 테이블을 만든다."""
    latest = cache["_meta"]["latest_quarter"]
    year_ago = f"{int(latest[:4]) - 1}{latest[4:]}"

    sales = cache["sales"][cache["sales"]["SVC_INDUTY_CD"] == biz_code]
    cur = sales[sales["STDR_YYQU_CD"] == latest]
    prev = sales[sales["STDR_YYQU_CD"] == year_ago]

    df = cur[["TRDAR_CD", "THSMON_SELNG_AMT", "THSMON_SELNG_CO"]].rename(
        columns={"THSMON_SELNG_AMT": "sales_amt", "THSMON_SELNG_CO": "sales_cnt"}
    )
    df = df.merge(
        prev[["TRDAR_CD", "THSMON_SELNG_AMT"]].rename(columns={"THSMON_SELNG_AMT": "sales_prev"}),
        on="TRDAR_CD",
        how="left",
    )

    store = cache["store"][cache["store"]["SVC_INDUTY_CD"] == biz_code]
    df = df.merge(
        store[["TRDAR_CD", "SIMILR_INDUTY_STOR_CO", "STOR_CO", "FRC_STOR_CO", "OPBIZ_RT", "CLSBIZ_RT"]],
        on="TRDAR_CD",
        how="left",
    )
    df = df.merge(
        cache["footfall"][["TRDAR_CD", "TOT_FLPOP_CO"]].rename(columns={"TOT_FLPOP_CO": "footfall"}),
        on="TRDAR_CD",
        how="left",
    )
    df = df.merge(
        cache["change"][["TRDAR_CD", "TRDAR_CHNGE_IX_NM"]], on="TRDAR_CD", how="left"
    )
    df = df.merge(
        cache["area"][["TRDAR_CD", "TRDAR_CD_NM", "TRDAR_SE_CD_NM", "SIGNGU_CD_NM", "ADSTRD_CD_NM"]],
        on="TRDAR_CD",
        how="left",
    )

    # ── 표본 게이트 ──
    # 기저가 작으면 성장률이 폭발한다(전년 300만원 → 올해 4,600만원 = 1601%).
    # 그런 값은 "성장"이 아니라 표본 부족이므로 지표를 내지 않는다.
    gate = cfg["commercial_power"]["sample_gate"]
    df["sample_ok"] = df["STOR_CO"].fillna(0) >= gate["min_stores"]
    base_floor = df["sales_prev"].median() * gate["growth_base_min_ratio"]

    # ── 파생지표 5종 ──
    df["sales_growth"] = (df["sales_amt"] / df["sales_prev"]) - 1
    df.loc[
        df["sales_prev"].isna() | (df["sales_prev"] < base_floor), "sales_growth"
    ] = pd.NA

    df["footfall_conversion"] = df["sales_amt"] / df["footfall"].replace(0, pd.NA)
    df["competition_density"] = df["SIMILR_INDUTY_STOR_CO"] / df["footfall"].replace(0, pd.NA)
    df["survival_signal"] = df["OPBIZ_RT"] - df["CLSBIZ_RT"]
    df["lifecycle"] = df["TRDAR_CHNGE_IX_NM"].map(cfg["commercial_power"]["lifecycle_scores"])

    # 참고용 — 점포당 월평균 매출 (수익 추정에서 씀)
    df["monthly_sales_per_store"] = (df["sales_amt"] / df["STOR_CO"].replace(0, pd.NA)) / 3

    # pd.NA 를 넣은 컬럼이 object 로 바뀌어 정렬·집계가 깨진다. 숫자로 되돌린다.
    for c in ("sales_growth", "footfall_conversion", "competition_density",
              "survival_signal", "lifecycle", "monthly_sales_per_store"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df


def add_grades(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """백분위 → 가중합 → A~E. 빈 지표는 제외하고 가중치를 재정규화한다."""
    cp = cfg["commercial_power"]
    weights = cp["weights"]
    reverse = set(cp["reverse_metrics"])

    w_cut = cp.get("winsorize", 0)
    for m in METRICS:
        series = pd.to_numeric(df[m], errors="coerce")
        # 표본부족 상권은 분포 자체에서 뺀다. 그래야 백분위가 왜곡되지 않는다.
        series = series.where(df["sample_ok"])
        if w_cut:
            lo, hi = series.quantile([w_cut, 1 - w_cut])
            series = series.clip(lo, hi)
        pct = series.rank(pct=True, na_option="keep")
        df[f"pct_{m}"] = (1 - pct) if m in reverse else pct

    pct_cols = [f"pct_{m}" for m in METRICS]
    w = pd.Series({f"pct_{m}": weights[m] for m in METRICS})

    present = df[pct_cols].notna()
    covered = present.mul(w, axis=1).sum(axis=1)          # 살아있는 가중치 합
    weighted = df[pct_cols].mul(w, axis=1).sum(axis=1, min_count=1)

    df["coverage"] = covered
    score = (weighted / covered).where(covered >= cp["min_weight_coverage"])
    # 표본 부족 상권은 아예 순위 모집단에서 뺀다. 남겨두면 백분위가 왜곡된다.
    df["score"] = score.where(df["sample_ok"])

    cuts = cp["grade_cutoffs"]
    score_pct = df["score"].rank(pct=True) * 100

    def to_grade(p):
        if pd.isna(p):
            return "표본부족"
        if p >= cuts["A"]:
            return "A"
        if p >= cuts["B"]:
            return "B"
        if p >= cuts["C"]:
            return "C"
        if p >= cuts["D"]:
            return "D"
        return "E"

    df["score_pct"] = score_pct
    df["grade"] = score_pct.map(to_grade)

    # ── 강등 규칙 ──
    for rule in cp.get("demotion_rules", []):
        if rule["metric"] == "closure_rate":
            avg = pd.to_numeric(df["CLSBIZ_RT"], errors="coerce").mean()
            hit = pd.to_numeric(df["CLSBIZ_RT"], errors="coerce") > avg * 1.5
            order = ["A", "B", "C", "D", "E"]
            cap = rule["max_grade"]
            df.loc[hit & df["grade"].isin(order[: order.index(cap)]), "grade"] = cap
            df["demoted"] = hit & df["grade"].notna()

    return df


# ── CLI ────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="상권력 지표·등급 계산")
    ap.add_argument("--biz", required=True, help="업종명 (예: 카페)")
    ap.add_argument("--region", help="지역명 (예: 성수동). 없으면 서울 전체 랭킹")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    cfg = load_config()
    cache = load_cache(cfg)

    biz = resolve_biz(args.biz, cache["sales"])
    if not biz["code"]:
        print(biz["note"])
        print("후보:", ", ".join(biz.get("candidates", [])))
        return
    print(f"업종: {args.biz} → {biz['name']} ({biz['code']})")
    if biz["note"]:
        print(f"  ※ {biz['note']}")
    if biz.get("alternative"):
        print(f"  ※ 다르게 보면: {biz['alternative']['name']}")

    df = add_grades(build_metrics(cache, biz["code"], cfg), cfg)
    print(f"기준 분기: {cache['_meta']['latest_quarter']} / 대상 상권 {len(df):,}개\n")

    show = ["TRDAR_CD_NM", "SIGNGU_CD_NM", "TRDAR_SE_CD_NM", "grade", "score_pct",
            "sales_growth", "monthly_sales_per_store", "CLSBIZ_RT"]

    if args.region:
        hit = resolve_region(args.region, cache["area"])
        if hit.empty:
            print(f"'{args.region}' 에 해당하는 상권을 찾지 못했습니다.")
            return
        sub = df[df["TRDAR_CD"].isin(hit["TRDAR_CD"])].sort_values("sales_amt", ascending=False)
        print(f"'{args.region}' 관련 상권 {len(sub)}개 (매출순)\n")
        out = sub[show]
    else:
        out = df.sort_values("score", ascending=False).head(args.top)[show]

    fmt = out.copy()
    fmt["score_pct"] = fmt["score_pct"].round(1)
    fmt["sales_growth"] = (fmt["sales_growth"] * 100).round(1)
    fmt["monthly_sales_per_store"] = (fmt["monthly_sales_per_store"] / 10000).round(0)
    fmt = fmt.rename(columns={
        "TRDAR_CD_NM": "상권", "SIGNGU_CD_NM": "자치구", "TRDAR_SE_CD_NM": "구분",
        "grade": "등급", "score_pct": "상위%", "sales_growth": "성장률%",
        "monthly_sales_per_store": "점포당월매출(만원)", "CLSBIZ_RT": "폐업률",
    })
    print(fmt.to_string(index=False))


if __name__ == "__main__":
    main()
