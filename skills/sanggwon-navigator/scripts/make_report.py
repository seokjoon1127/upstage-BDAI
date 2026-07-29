"""리포트 생성 — 파이프라인 전체를 관통한다.

    python scripts/make_report.py --region 성수동 --biz 카페

Step 0 입력 정규화 → 1 데이터 로드 → 2 지표 → 3 등급 → 4 정책 → 5 리포트
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from analyze import (  # noqa: E402
    METRICS, add_grades, build_metrics,
    load_cache, load_config, resolve_biz, resolve_region,
)
import rent as rent_mod  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent

METRIC_LABELS = {
    "sales_growth": "매출 성장률",
    "footfall_conversion": "유동인구 대비 매출",
    "competition_density": "경쟁 밀도",
    "survival_signal": "생존 신호",
    "lifecycle": "상권 라이프사이클",
}


def fmt_metric(name: str, v) -> str:
    if pd.isna(v):
        return "지표 없음"
    if name == "sales_growth":
        return f"{v * 100:+.1f}%"
    if name == "footfall_conversion":
        return f"{v:,.0f} 원/명"
    if name == "competition_density":
        return f"{v * 10000:.2f} 개/만명"
    if name == "survival_signal":
        return f"{v:+.1f}%p"
    return f"{v:.2f}"


METRIC_READING = {
    "sales_growth": ("전년보다 {v} 늘었다", "전년보다 {v} 줄었다"),
    "footfall_conversion": ("지나는 사람이 지갑을 잘 연다", "사람은 지나가는데 돈은 안 쓴다"),
    "competition_density": ("경쟁 점포가 적은 편이다", "같은 업종이 이미 빽빽하다"),
    "survival_signal": ("들어오는 가게가 나가는 가게보다 많다", "나가는 가게가 더 많다"),
    "lifecycle": ("상권이 커지는 국면이다", "상권이 정체·축소 국면이다"),
}


def build_metric_table(row: pd.Series, weights: dict) -> str:
    """지표 기여도 표.

    ⚠️ 백분위 표기 주의. 내부 pct 는 '좋을수록 1' 이지만,
    한국어 "상위 N%" 는 **N이 작을수록 좋다**. 그대로 쓰면 정반대로 읽힌다.
    (pct 0.89 → "상위 89%" ✕ / "상위 11%" ○)

    해석 문구를 직접 넣어 읽는 쪽이 지어내지 않게 한다.
    """
    lines = []
    for m in METRICS:
        pct = row[f"pct_{m}"]
        if pd.isna(pct):
            lines.append(f"| {METRIC_LABELS[m]} | 지표 없음 | — | {weights[m]:.2f} | — |")
            continue
        rank = (1 - pct) * 100                       # 상위 N% (작을수록 좋음)
        good = pct >= 0.5
        tmpl = METRIC_READING[m][0 if good else 1]
        reading = tmpl.format(v=fmt_metric(m, row[m]).lstrip("+-"))
        mark = "🟢" if pct >= 0.7 else ("🔴" if pct < 0.3 else "🟡")
        lines.append(
            f"| {METRIC_LABELS[m]} | {fmt_metric(m, row[m])} | "
            f"{mark} 상위 {rank:.0f}% | {reading} | {pct * weights[m]:.3f} |"
        )
    lines.append(f"| **총점** | | | | **{row['score']:.3f}** |")
    return "\n".join(lines)


def build_candidate_table(sub: pd.DataFrame, target: str, min_sales: float = 3_000_000) -> str:
    """후보 상권 비교표.

    점포당 월매출이 300만원도 안 되는 상권은 임대료도 못 내는 수준이라
    창업 후보가 되지 못한다. 표에서 빼고 몇 개를 뺐는지만 알린다.
    """
    keep = sub[sub["monthly_sales_per_store"].fillna(0) >= min_sales]
    dropped = len(sub) - len(keep)

    head = ("| 상권 | 구분 | 상권력 | 점포당 월매출 | 임대료 부담률 | 폐업률 |\n"
            "|---|---|---|---|---|---|")
    rows = []
    for _, r in keep.iterrows():
        mps = "—" if pd.isna(r["monthly_sales_per_store"]) else f"{r['monthly_sales_per_store'] / 10000:,.0f}만원"
        rb = "측정불가" if r["entry_cost"] == "판정보류" else (
            "—" if pd.isna(r["rent_burden"]) else f"{r['rent_burden'] * 100:.0f}%")
        star = " ←기준" if r["TRDAR_CD_NM"] == target else ""
        rows.append(
            f"| {r['TRDAR_CD_NM']}{star} | {r['TRDAR_SE_CD_NM']} | "
            f"**{r['grade']}** | {mps} | {rb} | {r['CLSBIZ_RT']:.1f}% |"
        )
    out = head + "\n" + "\n".join(rows)
    if dropped:
        out += (f"\n\n> 점포당 월매출 300만원 미만 **{dropped}곳은 제외**했다. "
                "임대료도 감당하기 어려운 수준이라 창업 후보가 되지 못한다.")
    return out


def build_cost_section(row: pd.Series, seoul_vac: float, prem: dict | None, cfg: dict) -> str:
    """진입비용 상세. 공실률은 등급이 아니라 협상 여지로 읽는다."""
    area = cfg["entry_cost"]["assumed_area_sqm"]
    if pd.isna(row.get("rent_burden")):
        return "_점포당 매출을 산출할 수 없어 임대료 부담률을 내지 않았습니다._"

    if row.get("entry_cost") == "판정보류":
        return "\n".join([
            "> ⚠️ **이 상권의 임대료는 알 수 없습니다.**",
            ">",
            "> 한국부동산원 임대동향조사는 서울의 **주요 상권 59곳만** 조사합니다.",
            f"> `{row['TRDAR_CD_NM']}` 은 조사 대상이 아니어서 인근 **{row['reb_district']}** 상권",
            f"> 시세(㎡당 {row['rent_per_sqm']:.1f}천원)를 적용했더니 "
            f"부담률이 **{row['rent_burden']*100:.0f}%** 로 나왔습니다.",
            ">",
            "> 골목 안쪽 점포에 큰길가 시세를 붙인 결과이므로 **진입비용을 판정하지 않습니다.**",
            "> 위의 상권력 판정은 그대로 유효합니다.",
        ])

    lines = [
        "| 항목 | 값 |",
        "|---|---|",
        f"| 부동산원 상권 | **{row['reb_district']}** ({row['reb_match_reason']}) |",
        f"| ㎡당 월 임대료 | {row['rent_per_sqm']:.1f} 천원 |",
        f"| {area}㎡(약 {area/3.3:.0f}평) 환산 월 임대료 | **{row['monthly_rent']/10000:,.0f}만원** |",
        f"| 점포당 월 예상매출 | {row['monthly_sales_per_store']/10000:,.0f}만원 |",
        f"| **임대료 부담률** | **{row['rent_burden']*100:.1f}%** → 진입비용 **{row['entry_cost']}** |",
        f"| 공실률 | {row['vacancy_rate']:.1f}% (서울 주요상권 평균 {seoul_vac:.1f}%) |",
    ]

    # 공실률은 임차인에게 나쁜 소식이 아니다 — 협상 여지다
    if row["vacancy_rate"] > seoul_vac:
        lines.append(
            f"\n> 💬 공실률이 서울 평균보다 **{row['vacancy_rate'] - seoul_vac:.1f}%p 높습니다.**\n"
            "> 임대인이 아쉬운 상황이라는 뜻이므로 임대료 협상 여지가 있습니다."
        )
    else:
        lines.append(
            f"\n> 💬 공실률이 서울 평균보다 낮습니다. 매물이 귀하므로 협상 여지는 크지 않습니다."
        )

    # ── 수익 추정 (외식업만) ──
    if pd.notna(row.get("monthly_profit")):
        m = row["monthly_sales_per_store"]
        lines += [
            "",
            "### 월 수익 추정",
            "",
            f"업종 원가 구조는 KREI 「외식업체 경영실태 조사」 **{row['krei_name']}** 기준이다. "
            "임차료만 위 실측값으로 갈아끼웠다.",
            "",
            "| 항목 | 금액 | 비율 | 출처 |",
            "|---|---|---|---|",
            f"| 월 평균매출 | **{m/10000:,.0f}만원** | — | 서울시 상권 데이터 (카드 실측) |",
            f"| − 임대료 | −{row['monthly_rent']/10000:,.0f}만원 | {row['rent_burden']*100:.1f}% | "
            f"{row['reb_match_reason']} · {row['reb_district']} |",
            f"| − 식재료비 | −{m*row['food_cost']/10000:,.0f}만원 | {row['food_cost']*100:.1f}% | KREI 조사 |",
            f"| − 인건비 | −{m*row['labor_cost']/10000:,.0f}만원 | {row['labor_cost']*100:.1f}% | KREI 조사 |",
            f"| − 세금·공과·기타 | −{m*row['etc_cost']/10000:,.0f}만원 | {row['etc_cost']*100:.1f}% | KREI 조사 |",
            f"| **= 월 예상 영업이익** | **{row['monthly_profit']/10000:,.0f}만원** | "
            f"**{row['profit_margin']*100:.1f}%** | |",
            "",
            f"> 손익분기 임대료율은 **{row['breakeven_burden']*100:.1f}%** 다. "
            f"이 상권은 {row['rent_burden']*100:.1f}% 로 "
            + ("**이미 넘었다.**" if row["rent_burden"] > row["breakeven_burden"] else "아직 여유가 있다.")
            + " 임차료 외 비용은 전국 평균 가정이므로 서울은 실제로 더 나갈 수 있다.",
        ]

    if prem:
        avg = prem.get("권리금 수준_평균")
        rate = prem.get("권리금 유 비율")
        if avg:
            lines.append(
                f"\n> 💰 서울 **{prem['industry'].strip()}** 평균 권리금 **{avg:,.0f}만원** "
                f"(권리금 있는 점포 비율 {rate:.0f}%, {prem['year']}년 기준)\n"
                "> 초기 자금 계획에 임대 보증금과 인테리어를 별도로 잡아야 합니다."
            )
    return "\n".join(lines)


def build_area_section(row: pd.Series, seoul_vac: float, cfg: dict) -> tuple[str, str, str]:
    """1부 — 이 동네는 어떤 곳인가. 업종과 무관한 동네의 체질.

    (요약, 표, 쉽게 말하면) 세 조각을 돌려준다.
    """
    area = cfg["entry_cost"]["assumed_area_sqm"]
    flow = row.get("footfall")
    life = row.get("TRDAR_CHNGE_IX_NM") or "—"

    summary = (f"**{row['TRDAR_CD_NM']}**{_josa(row['TRDAR_CD_NM'])} "
               f"{row['SIGNGU_CD_NM']}의 {row['TRDAR_SE_CD_NM']}이다. "
               f"상권 흐름은 **{life}** 국면이다.")

    rows = [
        "| 항목 | 값 | 뜻 |",
        "|---|---|---|",
        f"| 분기 유동인구 | {flow:,.0f}명 | 이 골목을 지나는 사람 수 |" if pd.notna(flow)
        else "| 분기 유동인구 | 지표 없음 | |",
        f"| 상권 흐름 | **{life}** | 상권이 커지는 중인지 줄어드는 중인지 |",
    ]
    if pd.notna(row.get("rent_per_sqm")):
        rows.append(
            f"| ㎡당 월 임대료 | {row['rent_per_sqm']:.1f} 천원 | "
            f"{area}㎡(약 {area/3.3:.0f}평) 환산 **{row['monthly_rent']/10000:,.0f}만원** |"
        )
        rows.append(f"| 임대료 출처 | {row['reb_match_reason']} · {row['reb_district']} | |")
    if pd.notna(row.get("vacancy_rate")):
        rows.append(
            f"| 공실률 | **{row['vacancy_rate']:.1f}%** | 서울 주요상권 평균 {seoul_vac:.1f}% |"
        )

    # 쉽게 말하면
    bits = []
    if pd.notna(row.get("vacancy_rate")):
        if row["vacancy_rate"] > seoul_vac * 1.3:
            bits.append("빈 가게가 서울 평균보다 눈에 띄게 많다. 임대인이 아쉬운 상황이라 **월세를 깎기 좋은 때**다")
        elif row["vacancy_rate"] < seoul_vac * 0.7:
            bits.append("빈 가게가 거의 없다. 자리가 귀해서 **협상 여지는 크지 않다**")
    if life in ("상권확장", "다이나믹"):
        bits.append("상권은 커지는 쪽으로 움직이고 있다")
    elif life in ("상권축소", "정체"):
        bits.append(f"상권 흐름은 {life} 국면이라 반등 요인을 따로 봐야 한다")
    plain = ("> 💡 **쉽게 말하면** — " + ". ".join(bits) + ".") if bits else ""
    return summary, "\n".join(rows), plain


def build_biz_section(biz: dict, cost: dict | None, df: pd.DataFrame,
                      prem: dict | None) -> tuple[str, str, str]:
    """2부 — 이 장사는 어떤 장사인가. 지역과 무관한 업종의 체질."""
    med = df["monthly_sales_per_store"].median()
    n = int(df["score"].notna().sum())

    summary = (f"서울에서 **{biz['name']}**{_josa(biz['name'], '을/를')} 하는 상권은 {n:,}곳이고, "
               f"점포당 월매출 중앙값은 **{med/10000:,.0f}만원**이다.")

    rows = ["| 항목 | 값 | 뜻 |", "|---|---|---|",
            f"| 서울 점포당 월매출 (중앙값) | {med/10000:,.0f}만원 | 절반은 이보다 많이, 절반은 적게 판다 |"]

    if cost:
        rows += [
            f"| 식재료비 | 매출의 **{cost['food_cost']*100:.1f}%** | 1만원 팔면 {cost['food_cost']*10000:,.0f}원 |",
            f"| 인건비 | 매출의 **{cost['labor_cost']*100:.1f}%** | 1만원 팔면 {cost['labor_cost']*10000:,.0f}원 |",
            f"| 임대료 제외 원가율 | **{cost['ex_rent_cost_ratio']*100:.1f}%** | 월세 빼고 나가는 돈 |",
            f"| **손익분기 임대료율** | **{cost['breakeven_burden']*100:.1f}%** | "
            f"월세가 매출의 이 비율을 넘으면 적자 |",
        ]
    if prem and prem.get("권리금 수준_평균"):
        rows.append(
            f"| 권리금 (서울 평균) | {prem['권리금 수준_평균']:,.0f}만원 | "
            f"권리금 있는 점포 {prem.get('권리금 유 비율', 0):.0f}% |"
        )

    if cost:
        plain = (
            f"> 💡 **쉽게 말하면** — {biz['name']}{_josa(biz['name'])} 1만원을 팔면 재료비 "
            f"{cost['food_cost']*10000:,.0f}원, 인건비 {cost['labor_cost']*10000:,.0f}원이 나가는 장사다. "
            f"월세가 매출의 **{cost['breakeven_burden']*100:.1f}%** 를 넘으면 평균적인 가게는 적자가 된다."
        )
    else:
        plain = ("> 💡 **쉽게 말하면** — 이 업종은 외식업 경영실태 조사 대상이 아니라 "
                 "원가 구조를 낼 수 없다. 임대료 부담률까지만 본다.")
    return summary, "\n".join(rows), plain


def build_cross_plain(row: pd.Series, cost: dict | None) -> str:
    """3부 — 동네×업종 교차 결과를 한 문단으로."""
    if pd.isna(row.get("rent_burden")) or row.get("entry_cost") == "판정보류":
        return ("> 💡 **쉽게 말하면** — 이 상권은 임대료 실측 자료가 없어 "
                "'얼마 남는지'까지는 계산하지 않았다. 장사가 되는지(위 등급)까지만 보면 된다.")
    burden, be = row["rent_burden"], row.get("breakeven_burden")
    profit = row.get("monthly_profit")
    s = (f"> 💡 **쉽게 말하면** — 이 동네에서 이 장사를 하면 월 "
         f"**{row['monthly_sales_per_store']/10000:,.0f}만원** 팔고 월세로 "
         f"**{row['monthly_rent']/10000:,.0f}만원**(매출의 {burden*100:.0f}%)을 낸다.")
    if pd.notna(profit) and pd.notna(be):
        if burden <= be:
            s += (f" 손익분기선이 {be*100:.0f}% 인데 그 아래라서, 평균 원가구조로 "
                  f"월 **{profit/10000:,.0f}만원**이 남는다.")
        else:
            s += (f" 손익분기선 {be*100:.0f}% 를 넘어서, 평균 원가구조로는 월 "
                  f"**{abs(profit)/10000:,.0f}만원이 모자란다.** 매출을 평균 이상으로 올리거나 "
                  "월세를 낮춰야 한다.")
    return s


def build_final_summary(row: pd.Series, cost: dict | None, pol: dict | None,
                        region: str, biz_name: str) -> str:
    """5부 — 마지막 요약. 결론 3줄 + 다음 행동."""
    lines = []
    grade_word = {"A": "좋은 편", "B": "괜찮은 편", "C": "보통",
                  "D": "아쉬운 편", "E": "어려운 편"}.get(row["grade"], "판정 불가")
    lines.append(f"1. **{region}에서 {biz_name}은 {grade_word}다** — "
                 f"{row['TRDAR_CD_NM']} 기준 종합 {row['grade']} 등급.")

    if pd.notna(row.get("monthly_profit")):
        p = row["monthly_profit"] / 10000
        lines.append(f"2. **월 {p:,.0f}만원 " + ("남는" if p >= 0 else "모자라는") +
                     f" 구조다** — 매출 {row['monthly_sales_per_store']/10000:,.0f}만원에서 "
                     f"월세 {row['monthly_rent']/10000:,.0f}만원과 원가를 뺀 값.")
    elif pd.notna(row.get("rent_burden")):
        lines.append(f"2. **월세 부담률은 {row['rent_burden']*100:.0f}%** 다.")
    else:
        lines.append("2. **임대료 자료가 없어 수익은 계산하지 못했다.**")

    n_pol = sum(len(v) for v in pol["branches"].values()) if pol else 0
    if n_pol:
        who = "답해주신 조건에 맞는" if (pol and pol.get("profile")) else "조건별로 나눈"
        lines.append(f"3. **{who} 지원제도가 {n_pol}건 있다** — 마감일과 자격을 위에서 확인할 것.")

    lines.append("")
    lines.append("**다음에 할 일**")
    todo = []
    if pd.notna(row.get("vacancy_rate")) and row["vacancy_rate"] > 8:
        todo.append("공실이 많은 편이니 계약 전 **임대료·렌트프리 협상**을 꼭 시도한다")
    todo.append("위 지원제도 중 **마감이 가까운 것부터** 공고문을 열어 자격을 확인한다")
    if row["grade"] in ("A", "B"):
        todo.append("같은 지역의 다른 상권 매물도 함께 보고 **월세를 비교**한다")
    else:
        todo.append("이 상권이 어려우면 **다른 후보 상권**을 위 비교표에서 골라 다시 본다")
    lines += [f"- {t}" for t in todo]
    return "\n".join(lines)


def build_region_choice(region: str, sub: pd.DataFrame, row: pd.Series) -> str:
    """지역명이 여러 상권에 걸리면 선택지를 제시한다.

    되묻되 답에 의존하지 않는다. 매출 1위로 이미 진행했고,
    답이 오면 --trdar 로 다시 뽑으면 된다. 자동 실행에서도 멈추지 않는다.
    """
    if len(sub) <= 1:
        return ""

    # 후보는 '창업 후보가 될 만한 곳' 중에서 고른다
    cand = sub[sub["monthly_sales_per_store"].fillna(0) >= 3_000_000]
    if cand.empty:
        cand = sub
    top = cand.head(1)                                    # 매출 1위 (=기본값)
    best = cand[cand["grade"].isin(["A", "B"])].nlargest(2, "score")
    picks, seen = [], set()
    for _, r in pd.concat([top, best]).iterrows():
        if r["TRDAR_CD_NM"] in seen:
            continue
        seen.add(r["TRDAR_CD_NM"])
        picks.append(r)
        if len(picks) == 3:
            break

    lines = [
        f'> ❓ **"{region}" 은 상권이 {len(sub)}곳으로 갈립니다.** '
        f"매출이 가장 큰 **{row['TRDAR_CD_NM']}** 기준으로 봤습니다.",
        ">",
    ]
    for i, r in enumerate(picks, 1):
        tag = " ← 지금 이 리포트" if r["TRDAR_CD_NM"] == row["TRDAR_CD_NM"] else ""
        rb = "" if pd.isna(r["rent_burden"]) or r["entry_cost"] == "판정보류" \
            else f" · 부담률 {r['rent_burden']*100:.0f}%"
        lines.append(
            f"> {i}. **{r['TRDAR_CD_NM']}** — {r['TRDAR_SE_CD_NM']} · 상권력 {r['grade']}"
            f" · 점포당 월매출 {r['monthly_sales_per_store']/10000:,.0f}만원{rb}{tag}"
        )
    lines += [
        ">",
        "> 다른 곳으로 볼까요? 상권명을 말씀하시면 그 기준으로 다시 뽑습니다.",
        f'> "{region} 전체" 라고 하시면 {len(sub)}곳을 한 번에 비교해 드립니다.',
    ]
    return "\n".join(lines)


def _josa(word: str, pair: str = "은/는") -> str:
    """받침 유무로 조사를 고른다. ('경쟁 밀도이' 같은 어색함 방지)"""
    a, b = pair.split("/")
    ch = (word or "").strip()[-1:]
    if not ch or not ("가" <= ch <= "힣"):
        return a
    return a if (ord(ch) - 0xAC00) % 28 else b


def build_power_summary(row: pd.Series, population: int, biz: str) -> str:
    """지표 표를 보기 전에 결론부터 준다."""
    strong = [(m, row[f"pct_{m}"]) for m in METRICS if pd.notna(row[f"pct_{m}"])]
    strong.sort(key=lambda x: x[1], reverse=True)
    good = [METRIC_LABELS[m] for m, p in strong if p >= 0.6][:2]
    bad = [METRIC_LABELS[m] for m, p in strong if p < 0.4][-2:]

    rank = 100 - row["score_pct"] if pd.notna(row["score_pct"]) else None
    head = (f"**{row['TRDAR_CD_NM']}** 은 서울 {biz} 상권 {population:,}곳 중 "
            f"**상위 {rank:.0f}%**, 상권력 **{row['grade']}** 다.") if rank is not None else \
           f"**{row['TRDAR_CD_NM']}** 은 표본이 부족해 등급을 내지 않았다."

    g, b = " · ".join(good), " · ".join(bad)
    if good and bad:
        return head + f" {g}{_josa(good[-1])} 강하지만, {b}{_josa(bad[-1], '이/가')} 발목을 잡는다."
    if good:
        return head + f" 특히 {g}{_josa(good[-1], '이/가')} 강하다."
    if bad:
        return head + f" {b}{_josa(bad[-1], '이/가')} 약하다."
    return head


def build_cost_summary(row: pd.Series, cfg: dict) -> str:
    if row.get("entry_cost") == "판정보류":
        return "이 상권은 임대료 실측 자료가 없어 진입비용을 판정하지 않았다. 아래 이유를 참고할 것."
    if pd.isna(row.get("rent_burden")):
        return "점포당 매출을 산출할 수 없어 임대료 부담률을 내지 않았다."
    area = cfg["entry_cost"]["assumed_area_sqm"]
    s = (f"{area}㎡(약 {area/3.3:.0f}평) 기준 월 임대료는 **{row['monthly_rent']/10000:,.0f}만원**, "
         f"점포당 월매출의 **{row['rent_burden']*100:.1f}%** 다.")
    if pd.notna(row.get("monthly_profit")):
        p = row["monthly_profit"] / 10000
        s += (f" 업종 평균 원가구조를 적용하면 월 **{p:,.0f}만원**이 "
              + ("남는다." if p >= 0 else "모자란다."))
    return s


def build_candidate_summary(region: str, sub: pd.DataFrame, row: pd.Series) -> str:
    n = len(sub)
    a_grade = sub[sub["grade"] == "A"]
    s = f'"{region}" 으로 검색되는 상권은 **{n}곳**이다.'
    if not a_grade.empty:
        names = " · ".join(a_grade["TRDAR_CD_NM"].head(3))
        s += f" 이 중 상권력 A는 **{names}** 이다."
    s += " 매출 순으로 정렬했다."
    return s


def build_policy_summary(pol: dict | None) -> str:
    if not pol:
        return "_정책 조회를 생략했다._"
    total = sum(len(v) for v in pol["branches"].values())
    uncond = len(pol["branches"].get("unconditional", []))
    return (f"오늘 기준 접수 중인 제도를 조건별로 갈랐다. "
            f"전체 {pol['total_fetched']:,}건 → 지역 {pol['after_region']}건 → "
            f"소상공인 관련 {pol['after_relevance']}건 중 **{total}건**을 골랐다. "
            f"**아무 조건 없이 신청 가능한 것만 {uncond}건**이다.")


def build_verdict(row: pd.Series, sub: pd.DataFrame, seoul_vac: float,
                  prem: dict | None, pol: dict | None) -> str:
    """숫자들을 엮어 '그래서 뭘 하라'를 만든다.

    개별 지표는 위 섹션에 다 있다. 여기서는 그것들을 교차해
    행동으로 옮길 수 있는 문장만 남긴다.
    """
    lines: list[str] = []
    burden, be = row.get("rent_burden"), row.get("breakeven_burden")

    # ① 어디로 갈 것인가
    lines.append(
        f"**{row['TRDAR_CD_NM']}** 을 기준으로 봤다. "
        f"상권력 **{row['grade']}**, 진입비용 **{row['entry_cost']}**."
    )

    # ② 얼마나 버틸 수 있나 — 손익분기까지의 여유를 금액으로 환산
    if pd.notna(burden) and pd.notna(be) and row["entry_cost"] != "판정보류":
        room = (be - burden) * row["monthly_sales_per_store"]
        if room > 0:
            lines.append(
                f"- 손익분기 임대료율이 {be*100:.1f}% 인데 지금은 {burden*100:.1f}% 다. "
                f"**월세를 {room/10000:,.0f}만원 더 내도 적자는 아니다.** 그만큼이 협상·입지 선택의 여유폭이다."
            )
        else:
            lines.append(
                f"- 손익분기 임대료율 {be*100:.1f}% 를 이미 **{(burden-be)*100:.1f}%p 넘겼다.** "
                f"평균 원가구조로는 월 {abs(room)/10000:,.0f}만원이 부족하다. "
                "매출을 평균 이상으로 끌어올리거나 임대료를 낮추지 않으면 버티기 어렵다."
            )

    # ③ 공실률을 협상 카드로
    if pd.notna(row.get("vacancy_rate")) and row["vacancy_rate"] > seoul_vac * 1.3:
        lines.append(
            f"- 공실률이 {row['vacancy_rate']:.1f}% 로 서울 평균({seoul_vac:.1f}%)의 "
            f"{row['vacancy_rate']/seoul_vac:.1f}배다. **빈 점포가 많다는 건 임대인이 아쉽다는 뜻**이니 "
            "계약 전 임대료·렌트프리 협상을 반드시 시도할 것."
        )

    # ④ 같은 이름의 다른 상권 중 더 나은 대안이 있는가
    alt = sub[(sub["grade"].isin(["A", "B"])) & (sub["TRDAR_CD_NM"] != row["TRDAR_CD_NM"])]
    alt = alt[alt["entry_cost"].isin(["저", "중"])]
    if not alt.empty:
        a = alt.iloc[0]
        lines.append(
            f"- 같은 지역 안에 **{a['TRDAR_CD_NM']}**(상권력 {a['grade']}, "
            f"부담률 {a['rent_burden']*100:.0f}%) 도 있다. 매물을 함께 보는 편이 낫다."
        )

    # ⑤ 초기자금
    if prem and prem.get("권리금 수준_평균"):
        lines.append(
            f"- 초기자금은 권리금 평균 **{prem['권리금 수준_평균']:,.0f}만원**에 "
            "보증금·인테리어를 더해 잡아야 한다. 월 수익만 보고 들어가면 자금이 막힌다."
        )

    # ⑥ 마감 임박 제도
    if pol:
        urgent = [
            p for items in pol["branches"].values() for p in items
            if p["_deadline"]["days_left"] is not None and p["_deadline"]["days_left"] <= 14
            and _is_core(p)          # 소상공인 직결 제도만. 수출·제조 공고는 뺀다
        ]
        seen, uniq = set(), []
        for p in urgent:
            t = _short_title(p.get("pblancNm", ""))
            if t not in seen:
                seen.add(t)
                uniq.append((t, p["_deadline"]["days_left"]))
        if uniq:
            items = " · ".join(f"{t}(D-{d})" for t, d in sorted(uniq, key=lambda x: x[1])[:3])
            lines.append(f"- **2주 내 마감**되는 지원제도가 {len(uniq)}건 있다 — {items}")

    return "\n".join(lines)


def build_region_note(region: str, sub: pd.DataFrame, row: pd.Series) -> str:
    """같은 이름의 동이 여러 자치구에 있으면 그 사실을 알린다.

    되묻지 않는다. 매출 규모가 가장 큰 곳으로 진행하되, 좁히는 방법을 알려준다.
    ('신사동' → 강남구·관악구 / '도곡동' → 강남구·서초구)
    """
    gus = sub["SIGNGU_CD_NM"].unique().tolist()
    if len(gus) <= 1:
        return ""
    others = [g for g in gus if g != row["SIGNGU_CD_NM"]]
    return (
        f'> ⚠️ **"{region}" 은 {", ".join(gus)} 여러 자치구에 걸쳐 있습니다.**\n'
        f"> 매출 규모가 가장 큰 **{row['SIGNGU_CD_NM']} {row['TRDAR_CD_NM']}** 기준으로 진단했습니다.\n"
        f'> 다른 쪽을 보시려면 `"{others[0]} {region}"` 처럼 자치구를 함께 적어주세요.'
    )


def _clean_summary(raw: str, limit: int = 110) -> str:
    """공고 요약에서 HTML·공백을 걷어내고 한 줄로 줄인다."""
    import html as _html
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", raw or "")
    text = _html.unescape(text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _short_title(raw: str) -> str:
    """`[서울] 2026년 … 모집 공고` 에서 껍데기를 벗긴다."""
    import re as _re

    t = (raw or "").strip()
    t = _re.sub(r"^\[[^\]]*\]\s*", "", t)                 # [서울] 제거
    t = _re.sub(r"^20\d{2}년\s*(상반기|하반기)?\s*", "", t)  # 2026년 하반기 제거
    t = _re.sub(r"\s*(수정\s*)?(재)?공고$", "", t)          # 끝의 '공고' 제거
    t = _re.sub(r"\s*(참여\s*)?(기업|기업체|소상공인|대상자|교육생|단원|참가자)?\s*모집$", "", t)
    return t.strip() or (raw or "").strip()


CORE_WORDS = ("소상공인", "자영업", "소공인", "점포", "상점가", "전통시장", "골목", "경영안정", "임차")
# 본문에 '소상공인' 이 한 번 스쳤다고 동네 카페 제도가 되지는 않는다.
# 공고명에 이 말이 있으면 수출·제조·유통 지원이므로 뺀다.
NOT_CORE_TITLE = ("시장개척단", "박람회", "홈쇼핑", "무역", "수출", "해외", "매칭 상담",
                  "비즈니스 매칭", "전시회", "IR", "투자유치")


def _is_core(p: dict) -> bool:
    """카페 같은 동네 점포 창업에 바로 쓰이는 제도인가."""
    title = str(p.get("pblancNm", ""))
    if any(w in title for w in NOT_CORE_TITLE):
        return False
    blob = " ".join(str(p.get(f, "")) for f in ("pblancNm", "hashtags", "bsnsSumryCn", "trgetNm"))
    return any(w in blob for w in CORE_WORDS)


def _policy_card(p: dict, detail: bool) -> list[str]:
    dl = p["_deadline"]
    title = _short_title(p.get("pblancNm", ""))
    urgent = " 🔥" if (dl["days_left"] is not None and dl["days_left"] <= 14) else ""
    url = p.get("pblancUrl", "")

    if not detail:                                    # 한 줄 요약형
        link = f"[{title}]({url})" if url else title
        return [f"- {link}{urgent} — {dl['label']}"]

    lines = [f"**{title}**{urgent}  ·  {dl['label']}"]
    meta = [f"`{p.get('pldirSportRealmLclasCodeNm','')}`", p.get("jrsdInsttNm", "")]
    if (p.get("trgetNm") or "").strip():
        meta.append(p["trgetNm"].strip())
    lines.append("　" + " · ".join(x for x in meta if x))
    summary = _clean_summary(p.get("bsnsSumryCn", ""), 130)
    if summary:
        lines.append(f"　{summary}")
    if p.get("_eligibility"):
        lines.append("　**자격** " + " · ".join(p["_eligibility"]))
    if url:
        lines.append(f"　[공고문·신청하기]({url}) · 신청기간 {dl['raw']}")
    lines.append("")
    return lines


def build_policy_section(pol: dict) -> str:
    """지원제도.

    29건을 같은 무게로 나열하면 아무것도 읽히지 않는다.
    소상공인 직결 제도를 앞에 자세히 두고, 나머지는 조건별로 접어서 준다.
    """
    branches, labels = pol["branches"], pol["labels"]
    out: list[str] = []

    # ① 조건 없이 신청 가능 — 소상공인 직결부터, 상위 3건은 자세히
    uncond = branches.get("unconditional", [])
    core = [p for p in uncond if _is_core(p)]
    rest = [p for p in uncond if not _is_core(p)]
    ordered = core + rest

    # 자세히 보여줄 3건에만 공고문을 열어 지원자격을 붙인다.
    # API 에 자격 필드가 없어서 첨부를 읽는 수밖에 없다. 실패하면 그냥 없이 간다.
    if ordered:
        try:
            from policy_detail import enrich
            got = enrich(ordered[:3], top_n=3)
            print(f"지원자격 확인: {got}/3건")
        except Exception as e:                            # noqa: BLE001
            print(f"지원자격 확인 생략 ({type(e).__name__})")

    if ordered:
        out.append("### 조건 없이 지금 신청 가능\n")
        for p in ordered[:3]:
            out += _policy_card(p, detail=True)
        if len(ordered) > 3:
            out.append("<details><summary>나머지 "
                       f"{len(ordered) - 3}건 펼치기</summary>\n")
            for p in ordered[3:]:
                out += _policy_card(p, detail=False)
            out.append("\n</details>\n")

    # ② 조건별 분기 — 조건은 필터가 아니라 분기다
    others = [(k, v) for k, v in branches.items() if k != "unconditional" and v]
    if others:
        out.append("### 조건이 맞으면 이만큼 더\n")
        out.append("나이·성별·창업경력을 묻지 않았다. 해당되는 줄만 보면 된다.\n")
        for name, items in others:
            items = sorted(items, key=lambda p: (not _is_core(p),
                                                 p["_deadline"]["days_left"] or 999))
            head = _short_title(items[0].get("pblancNm", ""))
            url = items[0].get("pblancUrl", "")
            first = f"[{head}]({url})" if url else head
            more = f" 외 {len(items) - 1}건" if len(items) > 1 else ""
            out.append(f"- **{labels[name]}** → {first}{more}")
            if len(items) > 1:
                inner = " · ".join(
                    f"[{_short_title(p.get('pblancNm',''))}]({p.get('pblancUrl','')})"
                    for p in items[1:6]
                )
                out.append(f"　　{inner}")
        out.append("")

    return "\n".join(out) if out else "_해당하는 지원제도를 찾지 못했습니다._"


def build_sources(meta: dict, row: pd.Series, pol: dict | None) -> str:
    """리포트가 실제로 쓴 데이터의 출처를 전부 적는다.

    임대료·원가·정책은 상권 데이터와 다른 기관에서 온다. 본문에서 쓰는데
    출처 목록에 없으면 안 된다.

    임대료 원본은 여기서 최신 여부도 함께 확인한다. 게시판 제목만 읽는 무료
    조회이고 주 1회로 제한되며, 실패해도 리포트를 막지 않는다.
    """
    lines = [
        f"- 상권 데이터: {meta['source']} — {meta['license']}",
        f"  - 기준 분기 {meta['latest_quarter']}, 수집 {meta['fetched_at']}",
    ]

    try:
        import parse_lease_pdf as lease
        stamp = lease.read_stamp()
        state, _ = lease.check_freshness()
    except Exception:                                     # noqa: BLE001
        stamp, state = {}, "unknown"

    if stamp.get("year"):
        tail = {
            "stale": "⚠️ 새 회차가 공개되었습니다 — `parse_lease_pdf.py` 로 갱신하세요",
            "unknown": "최신 여부 확인 실패 — 이 회차로 진행",
        }.get(state, "최신본 확인 완료")
        lines += [
            f"- 실측 임대료: 서울시 「{stamp['title']}」 (서울시 공정거래종합상담센터)",
            f"  - 145개 주요상권 1층 점포 12,531개 조사 · {stamp.get('rows', '?')}개 상권을 "
            f"Upstage Document Parse + Solar 로 추출 · {tail}",
        ]

    if pd.notna(row.get("rent_per_sqm")):
        q = row.get("rent_quarter")
        lines.append(f"- 임대료·공실률·권리금: {rent_mod.SOURCE} — {rent_mod.LICENSE}"
                     + (f" · 기준 분기 {q}" if q else ""))

    if row.get("krei_name"):
        lines.append("- 업종 원가 구조: 한국농촌경제연구원(KREI) 「외식업체 경영실태 조사」"
                     f" — {row['krei_name']} 기준")

    if pol:
        lines.append(f"- 지원제도: {pol['source']} — 조회 {pol['as_of']}")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="상권 진단 리포트 생성")
    ap.add_argument("--region", required=True)
    ap.add_argument("--biz", required=True)
    ap.add_argument("--trdar", help="상권명을 직접 지정 (기본값: 매출 1위)")
    ap.add_argument("--out", help="출력 경로")
    ap.add_argument("--skip-policy", action="store_true", help="정책 조회 생략 (오프라인 테스트)")
    ap.add_argument("--profile", help="사용자 조건: 청년 최초창업 여성 장애인 (띄어쓰기로 여러 개). "
                                      "없으면 조건별 분기를 전부 보여준다")
    args = ap.parse_args()

    cfg = load_config()
    cache = load_cache(cfg)
    meta = cache["_meta"]

    # ── Step 0. 입력 정규화 ──
    biz = resolve_biz(args.biz, cache["sales"])
    if not biz["code"]:
        sys.exit(f"{biz['note']}\n후보: {', '.join(biz.get('candidates', []))}")

    hit = resolve_region(args.region, cache["area"])
    if hit.empty:
        sys.exit(
            f"'{args.region}' 에 해당하는 서울 상권을 찾지 못했습니다.\n"
            "이 스킬은 서울시 상권 데이터만 다룹니다. 서울 내 지역명으로 다시 시도해 주세요.\n"
            "지원제도만 확인하려면: python scripts/match_policy.py --district <자치구>"
        )

    # ── Step 1~3. 지표 → 등급 ──
    print("지표 계산 중...")
    df = add_grades(build_metrics(cache, biz["code"], cfg), cfg)

    # ── 축 2. 진입비용 ──
    print("임대료·공실률 붙이는 중...")
    reb = rent_mod.ensure(cfg)
    df = rent_mod.attach(df, cfg, reb, biz["code"])
    sub = df[df["TRDAR_CD"].isin(hit["TRDAR_CD"])].sort_values("sales_amt", ascending=False)
    if sub.empty:
        sys.exit(f"'{args.region}' 상권에 {biz['name']} 매출 데이터가 없습니다.")

    row = sub[sub["TRDAR_CD_NM"] == args.trdar].iloc[0] if args.trdar else sub.iloc[0]


    # ── Step 4. 정책 ──
    pol = None
    if not args.skip_policy:
        print("지원제도 조회 중...")
        from match_policy import match, parse_profile
        pol = match(row["SIGNGU_CD_NM"], row["TRDAR_SE_CD_NM"],
                    cfg["report"]["top_n_policies"], parse_profile(args.profile))

    # ── Step 5. 렌더 ──
    tpl = (SKILL_ROOT / cfg["paths"]["template"]).read_text(encoding="utf-8")
    weights = cfg["commercial_power"]["weights"]
    presc = rent_mod.prescribe(row["grade"], row["entry_cost"])
    prem = rent_mod.premium_for(reb, biz["code"], rent_mod.load_mapping())
    seoul_vac = df.drop_duplicates("reb_district")["vacancy_rate"].mean()
    _cost = rent_mod.load_cost_ratio(cfg, biz["code"])
    _area = build_area_section(row, seoul_vac, cfg)
    _bizs = build_biz_section(biz, _cost, df, prem)


    fields = {
        "region": args.region,
        "biz_name": biz["name"],
        "biz_note": f"\n> ※ {biz['note']}" + (
            f" 다르게 보면 **{biz['alternative']['name']}** 기준이 될 수 있다."
            if biz.get("alternative") else ""
        ) if biz["note"] else "",
        "quarter": f"{meta['latest_quarter'][:4]}년 {meta['latest_quarter'][4:]}분기",
        "as_of": pol["as_of"] if pol else "-",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "trdar_name": row["TRDAR_CD_NM"],
        "trdar_type": row["TRDAR_SE_CD_NM"],
        "district": row["SIGNGU_CD_NM"],
        "grade": row["grade"],
        "rank_pct": f"{100 - row['score_pct']:.0f}" if pd.notna(row["score_pct"]) else "—",
        "cost_grade": (
            f"**{row['entry_cost']}** — 임대료 부담률 {row['rent_burden']*100:.1f}%"
            if pd.notna(row.get("rent_burden")) else "판정불가"
        ),
        "prescription": f"{presc[0]} — {presc[1]}",
        "population": f"{df['score'].notna().sum():,}",
        "metric_table": build_metric_table(row, weights),
        "demotion_note": (
            f"\n> ⚠️ **강등 적용**: 폐업률 {row['CLSBIZ_RT']:.1f}% 로 서울 평균의 1.5배를 넘어 "
            f"다른 지표와 무관하게 C 이하로 조정되었습니다."
            if row.get("demoted") else ""
        ),
        "cost_section": build_cost_section(row, seoul_vac, prem, cfg),
        "candidate_count": len(sub),
        "region_note": build_region_note(args.region, sub, row),
        "candidate_table": build_candidate_table(sub, row["TRDAR_CD_NM"]),
        "region_choice": build_region_choice(args.region, sub, row),
        "power_summary": build_power_summary(row, int(df["score"].notna().sum()), biz["name"]),
        "cost_summary": build_cost_summary(row, cfg),
        "candidate_summary": build_candidate_summary(args.region, sub, row),
        "policy_summary": build_policy_summary(pol),
        "area_summary": _area[0], "area_table": _area[1], "area_plain": _area[2],
        "biz_summary": _bizs[0], "biz_table": _bizs[1], "biz_plain": _bizs[2],
        "cross_plain": build_cross_plain(row, _cost),
        "final_summary": build_final_summary(row, _cost, pol, args.region, biz["name"]),
        "verdict": build_verdict(row, sub, seoul_vac, prem, pol),
        "policy_total": pol["total_fetched"] if pol else "-",
        "policy_region": pol["after_region"] if pol else "-",
        "policy_relevant": pol["after_relevance"] if pol else "-",
        "policy_section": build_policy_section(pol) if pol else "_정책 조회를 생략했습니다._",
        "sources": build_sources(meta, row, pol),
        "limitations": (
            "- 매출은 카드 데이터 기반 **추정치**이며 현금 거래가 반영되지 않는다.\n"
            "- 점포 3개 미만 상권은 평균이 대표성을 갖지 못해 등급을 내지 않는다.\n"
            "- 전년 매출이 업종 중앙값의 50% 미만이면 성장률을 산출하지 않는다 (기저효과).\n"
            "- 지원제도는 공고 본문 키워드로 분류한 것이며, **최종 자격은 공고문을 직접 확인해야 한다.**"
        ),
    }

    for k, v in fields.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))

    out_dir = SKILL_ROOT / cfg["paths"]["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else out_dir / f"{args.region}_{biz['name']}_{meta['latest_quarter']}.md"
    out.write_text(tpl, encoding="utf-8")
    print(f"\n리포트 생성 완료 → {out}")


if __name__ == "__main__":
    main()
