"""Upstage Document Parse — 서울시 상가임대차 실태조사 스캔 PDF에서 상권별 임대료를 뽑는다.

이 보고서는 78쪽 전부가 스캔 이미지라 일반 PDF 도구로는 글자 한 자도 못 읽는다.
그런데 그 안에 부동산원(서울 59개)보다 촘촘한 **140개 상권 실측 임대료**가 들어 있다.
Document Parse 로 표를 정형 데이터로 바꿔 골목상권 커버리지를 넓힌다.

크레딧을 아끼려고 표가 있는 쪽만 잘라서 보낸다.

사용법:
    python scripts/parse_lease_pdf.py --pdf data/raw/seoul_lease_2022.pdf --page 22
    python scripts/parse_lease_pdf.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
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


# 서울시 공정거래종합상담센터 「상가임대차 실태조사」 보고서 직링크
PDF_URL = "https://sftc.seoul.go.kr/common/file/NR_download.do?id=212c8fa3-cc90-461f-b412-e3821e499529"


def ensure_pdf(pdf: Path) -> Path:
    """PDF 가 없으면 받아온다. 상권 데이터의 '캐시 없으면 수집' 규칙과 같다.

    26MB 라 스킬에 넣지 않는다. 원본이 연 1회 갱신이므로 한 번 받으면 계속 쓴다.
    """
    if pdf.exists() and pdf.stat().st_size > 1_000_000:
        return pdf

    pdf.parent.mkdir(parents=True, exist_ok=True)
    print(f"보고서 PDF 다운로드 중... ({pdf.name})")
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
    except Exception as e:                                # noqa: BLE001
        sys.exit(
            f"PDF 다운로드 실패: {e}\n"
            f"직접 받아서 {pdf} 로 저장한 뒤 다시 실행하세요.\n"
            "출처: 서울시 공정거래종합상담센터 https://sftc.seoul.go.kr/fe/bbs/NR_list.do?bbsCd=6&ctgCd=2"
        )
    if not data.startswith(b"%PDF"):
        sys.exit("받은 파일이 PDF 가 아닙니다. 원본 링크가 바뀌었을 수 있습니다.")
    pdf.write_bytes(data)
    print(f"  {len(data) / 1024 / 1024:.1f}MB 저장 완료")
    return pdf


def extract_page(pdf: Path, page_no: int) -> Path:
    """해당 쪽만 단일 PDF 로 잘라낸다. 통째로 보내면 크레딧이 78배 든다."""
    from pypdf import PdfReader, PdfWriter

    out = pdf.with_name(f"{pdf.stem}_p{page_no}.pdf")
    if out.exists():
        return out
    reader = PdfReader(pdf)
    writer = PdfWriter()
    writer.add_page(reader.pages[page_no - 1])
    with open(out, "wb") as f:
        writer.write(f)
    return out


def parse_document(path: Path, key: str) -> dict:
    """Document Parse 호출. multipart/form-data 를 직접 만든다(외부 의존성 없이)."""
    boundary = uuid.uuid4().hex
    fields = {"model": "document-parse", "output_formats": "['html','markdown']", "ocr": "force"}

    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
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

PROMPT = """다음은 서울시 상가임대차 실태조사 보고서에서 OCR로 추출한 표다.
원본은 5개 세로 블록으로 나뉜 복잡한 표라서 셀 정렬이 어긋나 있다.

이 표에서 **상권별 통상임대료**만 정확히 골라내라.

원본 표에는 **약 140개 상권**이 있다. 최대한 빠짐없이 뽑아라.
표는 좌우로 5개 블록이 나란히 있으니 **모든 블록을 끝까지 훑어라.**

규칙:
- 각 항목은 (상권명, 평균, 중위수) 세 값이다. 단위는 만원/㎡.
- 자치구 이름(예: "강남구 8.58")은 상권이 아니라 그 구의 평균이다. 제외한다.
- 상권명에 (1층)(2층)(3층) 표기가 있으면 그대로 유지한다.
- 정렬이 어긋나 평균·중위수를 확신할 수 없는 항목은 **버린다.** 추측하지 마라.
- 숫자는 대개 1.00~25.00 범위다. 벗어나면 잘못 읽은 것이다.

JSON 배열만 출력하라. 설명하지 마라.
[{"상권명":"강남 가로수길","평균":8.69,"중위수":8.21}, ...]

표:
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

    # 범위를 벗어난 값은 OCR 오독이므로 버린다
    clean = [
        r for r in rows
        if r.get("상권명") and isinstance(r.get("평균"), (int, float)) and 0.5 <= r["평균"] <= 30
    ]
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
    ap.add_argument("--pdf", default="data/raw/seoul_lease_2022.pdf")
    ap.add_argument("--page", type=int, default=22, help="표가 있는 쪽 번호 (1부터)")
    ap.add_argument("--out", default="data/raw/seoul_lease_parsed.json")
    ap.add_argument("--rounds", type=int, default=3,
                    help="Solar 정형화 반복 횟수. 누락을 메우려 합집합을 취한다")
    args = ap.parse_args()

    pdf = ensure_pdf(SKILL_ROOT / args.pdf)
    page_pdf = extract_page(pdf, args.page)
    print(f"입력: {page_pdf.name} ({page_pdf.stat().st_size / 1024:.0f}KB, {args.page}쪽)")

    print("Document Parse 호출 중...")
    t0 = time.time()
    result = parse_document(page_pdf, load_key())
    print(f"완료 — {time.time() - t0:.1f}초")

    out = SKILL_ROOT / args.out
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    content = result.get("content", {})
    md = content.get("markdown", "")
    print(f"  markdown {len(md):,}자 / 요소 {len(result.get('elements', []))}개")

    print(f"Solar 로 표 정형화 중... ({args.rounds}회 반복)")
    key = load_key()
    rounds = [structure_with_solar(md, key) for _ in range(args.rounds)]
    rows = merge_rounds(rounds)

    import csv
    csv_path = SKILL_ROOT / "reference" / "seoul_lease_districts.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["상권명", "평균", "표본"])
        w.writeheader()
        w.writerows(rows)

    print(f"→ {csv_path}  ({len(rows)}개 상권)")
    for r in rows[:5]:
        print(f"    {r['상권명']:<22} 평균 {r['평균']:>6}  ({r['표본']}/{args.rounds}회 일치)")


if __name__ == "__main__":
    main()
