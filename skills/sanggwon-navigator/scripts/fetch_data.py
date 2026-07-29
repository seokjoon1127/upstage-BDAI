"""서울 열린데이터광장에서 상권 데이터를 수집해 parquet 캐시로 만든다.

동작 방식 (냉장고 규칙):
  · 캐시가 있고 최신이면      → 그냥 쓴다
  · 캐시가 없으면            → 받는다
  · 캐시가 낡았으면(분기 경과) → 다시 받는다
  · --refresh                → 무조건 다시 받는다

캐시에는 "어느 분기 데이터인지 / 언제 받았는지" 스탬프가 함께 저장되고,
리포트는 그 스탬프를 출처 각주로 인용한다.

사용법:
    python scripts/fetch_data.py              # 필요하면 수집
    python scripts/fetch_data.py --refresh    # 강제 재수집
    python scripts/fetch_data.py --status     # 캐시 상태만 확인
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
BASE = "http://openapi.seoul.go.kr:8088"

# 한국어 콘솔(cp949)에서 유니코드 기호가 깨지지 않도록
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 서비스명·컬럼명은 probe_api.py 로 실측 확인한 값이다 (2026-07-29).
# quarters: 몇 개 분기를 받을지. None 이면 분기 개념이 없는 마스터 데이터.
DATASETS = {
    "area":     {"service": "TbgisTrdarRelm",       "quarters": None, "label": "영역-상권"},
    "sales":    {"service": "VwsmTrdarSelngQq",     "quarters": 5,    "label": "추정매출-상권"},
    "store":    {"service": "VwsmTrdarStorQq",      "quarters": 1,    "label": "점포-상권"},
    "footfall": {"service": "VwsmTrdarFlpopQq",     "quarters": 1,    "label": "길단위인구-상권"},
    "change":   {"service": "VwsmTrdarIxQq",        "quarters": 1,    "label": "상권변화지표-상권"},
}

SOURCE = "서울 열린데이터광장 (data.seoul.go.kr) / 서울신용보증재단"
LICENSE = "공공누리 제1유형 (출처표시)"


# ── 설정·키 ────────────────────────────────────────────────────

def load_config() -> dict:
    with open(SKILL_ROOT / "config" / "defaults.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


# ── 분기 계산 ──────────────────────────────────────────────────

def prev_quarter(q: str) -> str:
    year, n = int(q[:4]), int(q[4:])
    return f"{year - 1}4" if n == 1 else f"{year}{n - 1}"


def quarter_distance(newer: str, older: str) -> int:
    """두 분기 사이가 몇 분기 벌어져 있는지."""
    a = int(newer[:4]) * 4 + int(newer[4:])
    b = int(older[:4]) * 4 + int(older[4:])
    return a - b


# ── API 호출 ───────────────────────────────────────────────────

def call(key: str, service: str, start: int, end: int, quarter: str | None = None) -> dict:
    url = f"{BASE}/{key}/json/{service}/{start}/{end}/"
    if quarter:
        url += f"{quarter}/"
    last_err = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{service} 호출 실패: {last_err}")


def _payload(body: dict) -> dict:
    if "RESULT" in body:  # 최상위 RESULT 는 에러 응답
        r = body["RESULT"]
        raise RuntimeError(f"{r.get('CODE')} {r.get('MESSAGE')}")
    return next(iter(body.values()))


def fetch_all(key: str, service: str, quarter: str | None, page: int) -> list[dict]:
    """페이지네이션을 돌며 전부 받는다. API 는 1회 최대 1,000건."""
    rows: list[dict] = []
    start = 1
    total = None
    while True:
        payload = _payload(call(key, service, start, start + page - 1, quarter))
        if total is None:
            total = payload.get("list_total_count") or 0
            if total == 0:
                return []
        chunk = payload.get("row") or []
        if not chunk:
            break
        prev = len(rows)
        rows.extend(chunk)
        # 1,000건마다 찍으면 로그가 수백 줄이 된다. \r 를 안 먹는 실행 환경도 있다.
        if len(rows) // 10_000 != prev // 10_000:
            print(f"      {len(rows):,} / {total:,}", flush=True)
        if len(rows) >= total:
            break
        start += page
    return rows


def find_latest_quarter(key: str) -> str:
    """가장 가벼운 데이터셋으로 최신 분기를 탐색한다."""
    today = date.today()
    q = f"{today.year}{(today.month - 1) // 3 + 1}"
    for _ in range(8):
        try:
            if _payload(call(key, "VwsmTrdarIxQq", 1, 1, q)).get("list_total_count"):
                return q
        except RuntimeError:
            pass
        q = prev_quarter(q)
    sys.exit("최신 분기를 찾지 못했습니다.")


# ── 캐시 ───────────────────────────────────────────────────────

def cache_dir(cfg: dict) -> Path:
    d = SKILL_ROOT / cfg["paths"]["cache_dir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_meta(cfg: dict) -> dict | None:
    path = cache_dir(cfg) / "_meta.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_stale(meta: dict | None, latest: str, max_age: int) -> tuple[bool, str]:
    if meta is None:
        return True, "캐시 없음"
    cached = meta.get("latest_quarter")
    if not cached:
        return True, "스탬프 없음"
    if not meta.get("complete", True):
        n, total = len(meta.get("datasets", {})), len(DATASETS)
        return True, f"수집이 중단됨 ({n}/{total}) — 이어받기"
    gap = quarter_distance(latest, cached)
    if gap > max_age:
        return True, f"{gap}개 분기 경과 (캐시 {cached} / 최신 {latest})"
    return False, f"최신 (캐시 {cached})"


# ── 메인 ───────────────────────────────────────────────────────

def collect(cfg: dict, key: str, latest: str, refresh: bool = False) -> dict:
    """상권 데이터를 받아 캐시로 만든다. **중단되면 이어받는다.**

    매출 데이터만 5개 분기 26MB라 전체 수집이 2분 넘게 걸린다. 실행 환경에
    명령 타임아웃이 있으면(Timely 는 180초) 통째로 받다가 죽고, 다음 실행에서
    처음부터 다시 받게 된다. 영원히 못 끝난다.

    그래서 **분기 하나를 받을 때마다 조각으로 저장**한다. 죽어도 받은 만큼은
    남고, 다시 실행하면 없는 조각부터 이어받는다. 한 데이터셋의 조각이 다
    모이면 하나로 합치고 조각을 지운다.
    """
    page = cfg["sources"]["seoul_commerce"]["page_size"]
    out = cache_dir(cfg)
    summary = {}
    todo = []

    for name, spec in DATASETS.items():
        final = out / f"{name}.parquet"
        quarters = [None]
        if spec["quarters"]:
            quarters = []
            q = latest
            for _ in range(spec["quarters"]):
                quarters.append(q)
                q = prev_quarter(q)

        if final.exists() and not refresh:
            import pyarrow.parquet as pq          # 26MB 를 다 읽지 않고 행 수만 본다
            summary[name] = {
                "service": spec["service"], "label": spec["label"],
                "rows": pq.ParquetFile(final).metadata.num_rows,
                "quarters": [q for q in quarters if q],
            }
            print(f"  [{spec['label']}] 이미 있음 — 건너뜀")
            continue

        print(f"  [{spec['label']}]")
        shards = []
        for q in quarters:
            shard = out / f"_{name}__{q or 'all'}.parquet"
            if shard.exists() and not refresh:
                print(f"    분기 {q or '-'} 이미 있음")
                shards.append(shard)
                continue
            print(f"    분기 {q or '-'} 수집 중...", flush=True)
            rows = fetch_all(key, spec["service"], q, page)
            if not rows:
                print("      ⚠️ 빈 결과")
                continue
            pd.DataFrame(rows).to_parquet(shard, index=False)
            print(f"      {len(rows):,} 행 저장", flush=True)
            shards.append(shard)

        if not shards:
            print("    ⚠️ 받은 게 없어 건너뜀")
            todo.append(spec["label"])
            continue

        df = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
        df.to_parquet(final, index=False)
        for s in shards:
            s.unlink(missing_ok=True)
        summary[name] = {
            "service": spec["service"], "label": spec["label"],
            "rows": len(df), "quarters": [q for q in quarters if q],
        }
        print(f"    합쳐서 {len(df):,} 행 → {name}.parquet")

    done = len(summary) == len(DATASETS)
    meta = {
        "latest_quarter": latest,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "license": LICENSE,
        "complete": done,
        "datasets": summary,
    }
    (out / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not done:
        print(f"\n⚠️ {len(summary)}/{len(DATASETS)} 완료. 남은 것: {', '.join(todo) or '중단됨'}")
        print("   같은 명령을 다시 실행하면 **이어서** 받습니다.")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description="상권 데이터 수집 · 캐시")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 강제 재수집")
    ap.add_argument("--status", action="store_true", help="캐시 상태만 출력")
    args = ap.parse_args()

    cfg = load_config()
    meta = read_meta(cfg)

    if args.status:
        if meta is None:
            print("캐시 없음")
            return
        print(f"기준 분기 : {meta['latest_quarter']}")
        print(f"수집 시각 : {meta['fetched_at']}")
        print(f"출처      : {meta['source']}")
        for name, info in meta["datasets"].items():
            qs = ", ".join(info["quarters"]) if info["quarters"] else "-"
            print(f"  {info['label']:<16} {info['rows']:>8,} 행   [{qs}]")
        if not meta.get("complete", True):
            missing = [s["label"] for n, s in DATASETS.items() if n not in meta["datasets"]]
            print(f"\n⚠️ 미완료 — 남은 것: {', '.join(missing)}")
            print("   `python scripts/fetch_data.py` 를 다시 실행하면 이어받습니다.")
        return

    key = load_key()
    print("최신 분기 확인 중...")
    latest = find_latest_quarter(key)
    print(f"최신 분기: {latest}\n")

    stale, reason = is_stale(meta, latest, cfg["freshness"]["max_age_quarters"])
    if not stale and not args.refresh:
        print(f"캐시 {reason} — 재수집 불필요. 강제하려면 --refresh")
        return

    print(f"수집 시작 ({'강제 재수집' if args.refresh else reason})\n")
    t0 = time.time()
    meta = collect(cfg, key, latest, refresh=args.refresh)
    tag = "완료" if meta.get("complete") else "여기까지"
    print(f"\n{tag} — {time.time() - t0:.1f}초, 기준 분기 {meta['latest_quarter']}")


if __name__ == "__main__":
    main()
