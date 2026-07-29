"""Upstage Document Parse — 서울시 상가임대차 실태조사 스캔 PDF에서 상권별 임대료를 뽑는다.

이 보고서는 전 쪽이 스캔 이미지라 일반 PDF 도구로는 글자 한 자도 못 읽는다.
그런데 그 안에 부동산원(서울 59개)보다 촘촘한 **145개 주요상권 실측 임대료**가 들어 있다.
Document Parse 로 표를 정형 데이터로 바꿔 골목상권 커버리지를 넓힌다.

사람 손이 필요한 곳을 전부 없앴다:
  - 다운로드 주소 → 자료실에서 그때그때 찾는다 (박아두면 UUID 가 바뀌며 죽는다)
  - 몇 쪽에 표가 있는지 → 몇 쪽씩 던져보고 찾는다 (판본마다 다르다)
  - 언제 갱신할지 → 게시글 번호를 비교한다 (발행 주기가 불규칙해 예측이 불가능하다)

크레딧을 아끼려고 표가 있는 구간만 잘라서 보낸다. 최신 여부 확인(--check)은 무료다.

사용법:
    python scripts/parse_lease_pdf.py --check     # 새 조사 떴는지만 확인 (크레딧 0)
    python scripts/parse_lease_pdf.py             # 최신본 받아서 파싱 → CSV 갱신
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_ROOT = Path(__file__).resolve().parent.parent
API = "https://api.upstage.ai/v1/document-digitization"
SOURCE = "서울시 「상가임대차 실태조사」 (서울시 공정거래종합상담센터)"


def load_key(name: str = "UPSTAGE_API_KEY") -> str:
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


# ── 원본 탐색 ──────────────────────────────────────────────────
#
# 다운로드 주소를 코드에 박아두지 않는다. 그 주소는 서울시 서버가 파일에 붙인
# 내부 UUID 라서 게시판을 개편하면 그날로 죽는다. 대신 자료실에서 그때그때 찾는다.
#
# 조사 발행 주기는 불규칙하다(2015·2017 → 2019 → 2022 → 2023, 이후 중단).
# 그래서 "언제 다시 볼지"를 예측하지 않는다. 게시판이 알고 있으니 물어보면 된다.
# 목록을 읽는 건 인증도 크레딧도 필요 없는 GET 한 번이므로, 보는 건 매번 하고
# 실제로 받아서 파싱하는 것만 새 조사가 떴을 때 한다.

BOARD = "https://sftc.seoul.go.kr/fe/bbs/NR_list.do?bbsCd=2&ctgCd=4"
BOARD_VIEW = "https://sftc.seoul.go.kr/fe/bbs/NR_view.do"
DOWNLOAD = "https://sftc.seoul.go.kr/common/file/NR_download.do?id={}"
UA = {"User-Agent": "Mozilla/5.0", "Referer": BOARD}

STAMP = SKILL_ROOT / "reference" / "seoul_lease_source.json"
CHECK_INTERVAL_DAYS = 7          # 매번 두드리지 않는다. 리포트를 열 번 뽑아도 확인은 주 1회.


def _title_year(title: str) -> int:
    """제목에서 조사 연도를 뽑는다. "'23년" → 2023, "2015 및 2017년" → 2017."""
    import re

    years = [2000 + int(y) for y in re.findall(r"'(\d{2})\s*년", title)]
    years += [int(y) for y in re.findall(r"(20\d{2})", title)]
    return max(years) if years else 0


def find_latest_report() -> dict:
    """자료실에서 가장 최신 「상가임대차 실태조사」 보고서를 찾는다.

    돌려주는 값: {"bbs_id", "title", "year", "url"}
    """
    import re

    html = ""
    for page in (1, 2):
        req = urllib.request.Request(f"{BOARD}&pageIndex={page}", headers=UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            html += resp.read().decode("utf-8", "replace")

    posts = []
    for m in re.finditer(r"BBS\.view\('(\d+)'\).*?>\s*([^<]+?)\s*</a>", html, re.S):
        bbs_id, title = m.group(1), m.group(2).strip()
        # 상담사례집·보도자료 등이 섞여 있으므로 실태조사 보고서만 고른다
        if "실태조사" in title and _title_year(title):
            posts.append({"bbs_id": bbs_id, "title": title, "year": _title_year(title)})
    if not posts:
        raise RuntimeError("자료실에서 실태조사 보고서를 찾지 못했습니다.")

    latest = max(posts, key=lambda p: p["year"])
    latest["url"] = _download_url(latest["bbs_id"])
    return latest


def _download_url(bbs_id: str) -> str:
    """상세 페이지에서 첨부파일 링크를 캐낸다. GET 은 막혀 있어 POST 로 연다."""
    import re

    data = urllib.parse.urlencode({"bbsCd": "2", "ctgCd": "4", "bbsSeq": bbs_id}).encode()
    req = urllib.request.Request(BOARD_VIEW, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"NR_download\.do\?id=([0-9a-f-]{36})", html)
    if not m:
        raise RuntimeError(f"게시글 {bbs_id} 에서 첨부파일 링크를 찾지 못했습니다.")
    return DOWNLOAD.format(m.group(1))


def read_stamp() -> dict:
    if STAMP.exists():
        try:
            return json.loads(STAMP.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            pass
    return {}


def write_stamp(report: dict, page_range: str, rows: int) -> None:
    STAMP.write_text(
        json.dumps(
            {
                "bbs_id": report["bbs_id"],
                "title": report["title"],
                "year": report["year"],
                "page_range": page_range,
                "rows": rows,
                "parsed_at": time.strftime("%Y-%m-%d"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def check_freshness() -> tuple[str, str]:
    """지금 쓰는 자료가 최신인지 게시판에 물어본다. (상태, 사람이 읽을 한 줄)

    상태: fresh(최신) / stale(새 조사 있음) / unknown(확인 실패)

    확인이 실패해도 리포트를 막지 않는다. 서울시 서버가 죽어 있다는 이유로
    분석이 멈추면 그게 더 나쁘다. 못 했으면 못 했다고 적고 진행한다.
    """
    stamp = read_stamp()
    have = stamp.get("year", 0)
    label = stamp.get("title") or "서울시 상가임대차 실태조사"

    last = stamp.get("checked_at")
    if last:
        gap = (date.fromisoformat(time.strftime("%Y-%m-%d")) - date.fromisoformat(last)).days
        if gap < CHECK_INTERVAL_DAYS:
            return "fresh", f"{have}년 조사 사용 ({gap}일 전 최신 여부 확인함)"

    try:
        latest = find_latest_report()
    except Exception as e:                                # noqa: BLE001
        return "unknown", f"{have}년 조사 사용 (최신 여부 확인 실패: {type(e).__name__})"

    stamp["checked_at"] = time.strftime("%Y-%m-%d")
    if stamp:
        STAMP.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")

    if latest["bbs_id"] != stamp.get("bbs_id"):
        return "stale", (
            f"새 조사가 올라왔습니다 — {latest['title']}. "
            f"`python scripts/parse_lease_pdf.py` 로 갱신하세요. (현재 {have}년 자료 사용 중)"
        )
    return "fresh", f"{have}년 조사 사용 (최신본 확인 완료)"


def ensure_pdf(pdf: Path, report: dict) -> Path:
    """PDF 가 없으면 받아온다. 상권 데이터의 '캐시 없으면 수집' 규칙과 같다.

    10~26MB 라 스킬에 넣지 않는다. 파싱 결과 CSV 만 스킬에 들어간다.
    """
    if pdf.exists() and pdf.stat().st_size > 1_000_000:
        return pdf

    pdf.parent.mkdir(parents=True, exist_ok=True)
    print(f"보고서 PDF 다운로드 중... ({report['title'][:40]})")
    req = urllib.request.Request(report["url"], headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
    except Exception as e:                                # noqa: BLE001
        sys.exit(
            f"PDF 다운로드 실패: {e}\n"
            f"직접 받아서 {pdf} 로 저장한 뒤 다시 실행하세요.\n"
            f"출처: 서울시 공정거래종합상담센터 {BOARD}"
        )
    if not data.startswith(b"%PDF"):
        sys.exit("받은 파일이 PDF 가 아닙니다. 게시판 구조가 바뀌었을 수 있습니다.")
    pdf.write_bytes(data)
    print(f"  {len(data) / 1024 / 1024:.1f}MB 저장 완료")
    return pdf


def extract_pages(pdf: Path, start: int, end: int) -> Path:
    """해당 구간만 잘라낸다. 통째로 보내면 크레딧이 수십 배 든다. (쪽 번호는 1부터)"""
    from pypdf import PdfReader, PdfWriter

    out = pdf.with_name(f"{pdf.stem}_p{start}-{end}.pdf")
    if out.exists():
        return out
    reader = PdfReader(pdf)
    writer = PdfWriter()
    for i in range(start - 1, min(end, len(reader.pages))):
        writer.add_page(reader.pages[i])
    with open(out, "wb") as f:
        writer.write(f)
    return out


MIN_RENT_VALUES = 80          # 상권별 표에는 약 140개 값이 있다. 자치구 표(25개)와 갈린다.


def _is_rent_table(md: str) -> bool:
    """상권별 통상임대료 표인가.

    "통상임대료"라는 글자만 보면 안 된다. 보고서 앞쪽 일반현황 절에도 그 단어가
    수십 번 나오고 표도 많다. 판별은 **값**으로 한다 — 상권별 표에만
    2~20 만원/㎡ 범위 숫자가 100개 넘게 깔려 있다.
    """
    import re

    if "통상임대료" not in md:
        return False
    vals = [float(x) for x in re.findall(r"\d+\.\d{2}", md)]
    return sum(1 for v in vals if 2 <= v <= 20) >= MIN_RENT_VALUES


def locate_table(pdf: Path, key: str, chunk: int = 8, stride: int = 6,
                 max_probes: int = 5) -> tuple[str, str]:
    """표가 몇 쪽에 있는지 찾는다. (markdown, 쪽범위)

    쪽 번호를 코드에 박아두면 보고서가 바뀔 때마다 사람이 뒤져야 한다.
    보고서마다 위치가 다르므로(2022년 78쪽 중 22쪽, 2023년 44쪽 중 17쪽 부근)
    몇 쪽씩 묶어 던져보고 표가 든 묶음을 고른다.

    묶음을 2쪽씩 겹치게 자른다(chunk 8, stride 6). 안 그러면 표가 경계에
    걸쳐 잘린 채로 검출돼 절반만 뽑힌다.

    두 판본 모두 전체의 30~40% 지점이었으므로 거기서부터 바깥으로 훑되,
    거리가 같으면 뒤쪽을 먼저 본다. 앞쪽은 목차·개요라 표가 있을 리 없다.
    찾은 묶음의 markdown 을 그대로 돌려주므로 추가 호출이 들지 않는다.
    """
    from pypdf import PdfReader

    n = len(PdfReader(pdf).pages)
    anchor = max(1, int(n * 0.35))
    starts = sorted(range(1, n + 1, stride), key=lambda s: (abs(s + chunk // 2 - anchor), -s))

    for probe, start in enumerate(starts[:max_probes], 1):
        end = min(start + chunk - 1, n)
        part = extract_pages(pdf, start, end)
        print(f"  탐색 {probe}/{max_probes} — p{start}~{end} ({part.stat().st_size / 1024:.0f}KB)")
        md = parse_document(part, key).get("content", {}).get("markdown", "")
        if _is_rent_table(md):
            print(f"  → p{start}~{end} 에서 표 발견 (markdown {len(md):,}자)")
            return md, f"p{start}-{end}"

    sys.exit(
        f"{max_probes}회 탐색했지만 통상임대료 표를 찾지 못했습니다.\n"
        f"--page 로 쪽 번호를 직접 지정하세요."
    )


def parse_document(path: Path, key: str, mime: str = "application/pdf") -> dict:
    """Document Parse 호출. multipart/form-data 를 직접 만든다(외부 의존성 없이).

    mime 은 PDF 말고도 이미지(png/jpg)·docx 를 보낼 수 있게 열어 둔 것이다.
    정책 공고문 중 12%가 이미지라 그쪽에서 쓴다.
    """
    boundary = uuid.uuid4().hex
    fields = {"model": "document-parse", "output_formats": "['html','markdown']", "ocr": "force"}

    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        API,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            sys.exit(f"Document Parse 실패 (HTTP {e.code}): {detail}")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    sys.exit(f"Document Parse 호출 실패: {last}")


SOLAR_API = "https://api.upstage.ai/v1/chat/completions"

PROMPT = """다음은 서울시 상가임대차 실태조사 보고서에서 OCR로 추출한 문서다.
원본 표는 좌우로 5개 블록이 나란히 놓인 구조라서 셀 정렬이 어긋나 있다.

여기서 **상권별 통상임대료(만원/㎡)** 표 하나만 골라 뽑아라.

⚠️ 문서 안에 비슷한 표가 여럿 있을 수 있다. 반드시 **단위가 만원/㎡ 인 표**를 골라라.
- 통상임대료/㎡ 는 대개 **2~20** 범위다. (서울 평균 약 7.5)
- 값이 20을 훌쩍 넘어 40~110 대라면 그건 전용면적(㎡) 표다. **쓰지 마라.**

원본에는 **약 145개 상권**이 있다. 5개 블록을 **모두 끝까지 훑어** 빠짐없이 뽑아라.

규칙:
- 각 항목은 (상권명, 통상임대료) 두 값이다.
- 자치구 이름 뒤에 숫자가 붙은 것(예: "중구 4.76")은 그 구의 평균이다. **제외한다.**
- 상권명에 (1층)(2층)(3층) 표기가 있으면 그대로 유지한다.
- 정렬이 어긋나 값을 확신할 수 없는 항목은 **버린다.** 추측하지 마라.
- OCR 때문에 한 셀에 값과 자치구명이 붙어 있을 수 있다(예: `5.48 성동구`).
  그럴 땐 숫자는 앞 상권의 값이고, 자치구명은 **뒤에 오는 상권**의 것이다.
  이런 셀이 있어도 그 행의 상권을 통째로 버리지 마라.
- ⚠️ **표의 마지막 행까지 반드시 훑어라.** 마지막 몇 행이 누락되는 일이 잦다.
  각 블록의 맨 아래 행에도 상권이 들어 있다.

JSON 배열만 출력하라. 설명하지 마라.
[{"상권명":"가로수길","임대료":11.54}, ...]

문서:
"""


def structure_with_solar(markdown: str, key: str) -> list[dict]:
    """OCR 표를 Solar 로 정형화한다. 정렬이 깨진 표는 규칙으로 못 풀어서 LLM 을 쓴다."""
    payload = {
        "model": "solar-pro2",
        "messages": [{"role": "user", "content": PROMPT + markdown}],
        "temperature": 0,
        "max_tokens": 8000,
    }
    req = urllib.request.Request(
        SOLAR_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"Solar 호출 실패 (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:400]}")

    text = body["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        sys.exit(f"Solar 응답에서 JSON 배열을 찾지 못했습니다: {text[:300]}")
    rows = json.loads(text[start : end + 1])

    # 범위를 벗어난 값은 OCR 오독이므로 버린다.
    # 판본에 따라 키가 '임대료' 또는 '평균' 으로 나오므로 '평균' 으로 통일한다.
    clean = []
    for r in rows:
        val = r.get("임대료", r.get("평균"))
        if r.get("상권명") and isinstance(val, (int, float)) and 0.5 <= val <= 25:
            clean.append({"상권명": r["상권명"], "평균": float(val)})
    usage = body.get("usage", {})
    print(f"  Solar 정형화: {len(rows)}건 → 유효 {len(clean)}건 "
          f"(토큰 {usage.get('total_tokens', '?')})")
    return clean


def merge_rounds(rounds: list[list[dict]]) -> list[dict]:
    """여러 번 파싱한 결과를 합친다.

    LLM 은 값을 틀리게 읽지는 않지만 **일부를 빠뜨린다.** 같은 입력을 세 번 돌리면
    125 / 111 / 117 개처럼 건수가 흔들린다. 그래서 합집합을 취해 커버리지를 채우고,
    값이 엇갈리는 상권은 다수결로 정한다. 표가 있는 쪽 하나만 보내므로 비용도 작다.
    """
    from collections import Counter, defaultdict

    votes: dict[str, Counter] = defaultdict(Counter)
    for rows in rounds:
        for r in rows:
            name = str(r.get("상권명", "")).strip()
            if name:
                votes[name][round(float(r["평균"]), 2)] += 1

    merged, disputed = [], 0
    for name, counter in votes.items():
        (val, n), = counter.most_common(1)
        if len(counter) > 1:
            disputed += 1
        merged.append({"상권명": name, "평균": val, "표본": n})

    merged.sort(key=lambda r: r["상권명"])
    each = " / ".join(str(len(r)) for r in rounds)
    print(f"  {len(rounds)}회 병합: {each} → **{len(merged)}개** (값 불일치 {disputed}건은 다수결)")
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description="스캔 PDF에서 상권별 임대료 표 추출")
    ap.add_argument("--check", action="store_true",
                    help="최신 여부만 확인하고 끝낸다. 크레딧을 쓰지 않는다")
    ap.add_argument("--pdf", help="PDF 경로. 생략하면 자료실 최신본을 받는다")
    ap.add_argument("--page", type=int, help="표가 있는 쪽. 생략하면 자동 탐색")
    ap.add_argument("--rounds", type=int, default=4,
                    help="Solar 정형화 반복 횟수. 누락을 메우려 합집합을 취한다")
    args = ap.parse_args()

    if args.check:
        state, note = check_freshness()
        print(f"[{state}] {note}")
        return

    report = find_latest_report()
    print(f"자료실 최신본: {report['title']} (게시글 {report['bbs_id']}, {report['year']}년)")

    pdf = SKILL_ROOT / (args.pdf or f"data/raw/seoul_lease_{report['year']}.pdf")
    ensure_pdf(pdf, report)

    key = load_key()
    t0 = time.time()
    if args.page:
        part = extract_pages(pdf, args.page, args.page)
        md = parse_document(part, key).get("content", {}).get("markdown", "")
        page_range = f"p{args.page}"
    else:
        print("Document Parse 로 표 위치 탐색 중...")
        md, page_range = locate_table(pdf, key)
    print(f"완료 — {time.time() - t0:.1f}초")

    print(f"Solar 로 표 정형화 중... ({args.rounds}회 반복)")
    rows = merge_rounds([structure_with_solar(md, key) for _ in range(args.rounds)])

    # 엉뚱한 표를 뽑았는지 확인한다. 통상임대료/㎡ 의 중앙값은 서울 기준 3~15 만원.
    mid = sorted(r["평균"] for r in rows)[len(rows) // 2] if rows else 0
    if not 2 <= mid <= 15:
        sys.exit(f"뽑힌 값의 중앙값이 {mid} 입니다. 통상임대료 표가 아닐 수 있어 중단합니다.")

    import csv
    csv_path = SKILL_ROOT / "reference" / "seoul_lease_districts.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["상권명", "평균", "표본"])
        w.writeheader()
        w.writerows(rows)
    write_stamp(report, page_range, len(rows))

    print(f"→ {csv_path}  ({len(rows)}개 상권, 중앙값 {mid}만원/㎡, {page_range})")
    for r in rows[:5]:
        print(f"    {r['상권명']:<22} {r['평균']:>6}  ({r['표본']}/{args.rounds}회 일치)")


if __name__ == "__main__":
    main()
