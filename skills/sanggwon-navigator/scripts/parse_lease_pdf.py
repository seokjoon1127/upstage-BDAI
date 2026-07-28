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


def main() -> None:
    ap = argparse.ArgumentParser(description="스캔 PDF에서 상권별 임대료 표 추출")
    ap.add_argument("--pdf", default="data/raw/seoul_lease_2022.pdf")
    ap.add_argument("--page", type=int, default=22, help="표가 있는 쪽 번호 (1부터)")
    ap.add_argument("--out", default="data/raw/seoul_lease_parsed.json")
    args = ap.parse_args()

    pdf = SKILL_ROOT / args.pdf
    if not pdf.exists():
        sys.exit(f"{pdf} 가 없습니다.")

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

    print("Solar 로 표 정형화 중...")
    rows = structure_with_solar(md, load_key())

    import csv
    csv_path = SKILL_ROOT / "reference" / "seoul_lease_districts.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["상권명", "평균", "중위수"])
        w.writeheader()
        w.writerows({k: r.get(k) for k in ("상권명", "평균", "중위수")} for r in rows)

    print(f"→ {csv_path}  ({len(rows)}개 상권)")
    for r in rows[:5]:
        print(f"    {r['상권명']:<22} 평균 {r['평균']:>6} · 중위 {r.get('중위수')}")


if __name__ == "__main__":
    main()
