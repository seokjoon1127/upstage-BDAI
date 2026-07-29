"""공고문 첨부에서 지원자격을 뽑는다.

기업마당 API 는 지원자격을 필드로 주지 않는다. 확인한 결과:
  - 요약(bsnsSumryCn) 은 200~370자뿐이고 자격이 0/5건
  - 공고 상세페이지 HTML 은 1/8건, 그마저 "공고문 참조"
자격은 **첨부 공고문 안에만** 있고, API 가 그 파일 주소를 함께 준다.

첨부 형식이 섞여 있다 (성동구 접수중 77건 기준):
  pdf 49 · hwp/hwpx 18 · png/jpg 9 · docx 1
그래서 형식별로 갈라서 처리한다. PDF 는 pypdf 로 공짜로 읽고,
**이미지 공고문과 스캔 PDF 는 Document Parse 아니면 읽을 방법이 없다.**
HWP 는 지원 형식이 아니라 건너뛴다 — 커버리지 약 77%.

자격을 못 구해도 리포트는 그대로 나온다. 그 자리에 원래 있던
"공고문에서 직접 확인" 안내가 남을 뿐이다.

사용법:
    python scripts/policy_detail.py --district 성동구 --top 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from parse_lease_pdf import SOLAR_API, load_key, parse_document  # noqa: E402

CACHE = SKILL_ROOT / "data" / "cache" / "policy_detail"
MAX_PAGES = 5          # 표본 6건 모두 자격이 1~2쪽에 있었다
MIN_TEXT = 300         # 이보다 적으면 스캔으로 본다
UA = {"User-Agent": "Mozilla/5.0"}

# Document Parse 가 받는 형식만 적는다. hwp/hwpx 는 없다.
IMAGE_EXT = {"png", "jpg", "jpeg", "bmp", "tiff", "heic"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "bmp": "image/bmp", "tiff": "image/tiff", "heic": "image/heic",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf"}


def _ext(p: dict) -> str:
    return (p.get("printFileNm") or "").rsplit(".", 1)[-1].lower().strip()


def _download(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _first_pages(data: bytes) -> bytes:
    """앞쪽 몇 장만 남긴다. 통째로 보내면 47쪽짜리도 있다."""
    import io

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) <= MAX_PAGES:
        return data
    writer = PdfWriter()
    for i in range(MAX_PAGES):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def extract_text(p: dict) -> tuple[str, str] | tuple[None, str]:
    """공고문 본문을 글자로 만든다. (본문, 방법) 또는 (None, 사유).

    방법: pypdf(무료) / document-parse(크레딧)
    """
    ext = _ext(p)
    url = (p.get("printFlpthNm") or "").split("@")[0].strip()
    if not url:
        return None, "첨부 없음"
    if ext in ("hwp", "hwpx"):
        return None, "hwp 미지원"

    try:
        data = _download(url)
    except Exception as e:                                # noqa: BLE001
        return None, f"다운로드 실패({type(e).__name__})"

    if data[:4] == b"%PDF":
        import io

        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "".join((pg.extract_text() or "") for pg in reader.pages[:MAX_PAGES])
        except Exception:                                 # noqa: BLE001
            text = ""
        if len(text) >= MIN_TEXT:
            return text, "pypdf"
        # 글자가 안 나오면 스캔이다. 여기서부터 Document Parse.
        data, ext = _first_pages(data), "pdf"
    elif ext not in IMAGE_EXT and ext != "docx":
        return None, f"{ext or '알 수 없는'} 형식"

    tmp = CACHE / f"_tmp_{p['pblancId']}.{ext or 'pdf'}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        res = parse_document(tmp, load_key(), mime=MIME.get(ext, "application/pdf"))
        return res.get("content", {}).get("markdown", ""), "document-parse"
    except SystemExit as e:                               # parse_document 는 sys.exit 로 죽는다
        return None, f"Document Parse 실패({e})"
    except Exception as e:                                # noqa: BLE001
        return None, f"Document Parse 실패({type(e).__name__})"
    finally:
        tmp.unlink(missing_ok=True)


PROMPT = """다음은 정부 지원사업 공고문에서 뽑은 글이다.
여기서 **누가 신청할 수 있는지(지원자격)** 만 골라내라.

규칙:
- 조건 2~4개, 각 40자 이내. 조건만 적고 서술하지 마라.
- 나이·업력·지역·업종·규모처럼 **구체적인 조건**을 우선한다.
- 신청방법·지원내용·문의처·심사절차는 제외한다.
- 공고문에 자격 조건이 없으면 null 을 반환한다. **지어내지 마라.**

JSON 만 출력하라. 설명하지 마라.
{"eligibility": ["만 40세 이상", "서울 소재 소상공인", "사업자등록 1년 이상"]}
또는 {"eligibility": null}

공고문:
"""


def eligibility(text: str, key: str) -> list[str] | None:
    payload = {
        "model": "solar-pro2",
        "messages": [{"role": "user", "content": PROMPT + text[:12000]}],
        "temperature": 0,
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        SOLAR_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    raw = body["choices"][0]["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e < 0:
        return None
    items = json.loads(raw[s:e + 1]).get("eligibility")
    if not isinstance(items, list):
        return None
    clean = [str(x).strip() for x in items if str(x).strip()][:4]
    return clean or None


def enrich(policies: list[dict], top_n: int = 3) -> int:
    """상위 몇 건에만 자격을 붙인다. 실패는 조용히 넘긴다.

    붙인 건수를 돌려준다. 각 공고는 pblancId 로 캐시하므로 두 번 호출하지 않는다.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    key = None
    done = 0

    for p in policies[:top_n]:
        pid = p.get("pblancId")
        if not pid:
            continue
        cached = CACHE / f"{pid}.json"
        if cached.exists():
            try:
                saved = json.loads(cached.read_text(encoding="utf-8"))
                if saved.get("eligibility"):
                    p["_eligibility"] = saved["eligibility"]
                    p["_eligibility_how"] = saved.get("how", "캐시")
                    done += 1
                continue
            except Exception:                             # noqa: BLE001
                pass

        text, how = extract_text(p)
        items = None
        if text:
            try:
                key = key or load_key()
                items = eligibility(text, key)
            except Exception as e:                        # noqa: BLE001
                how = f"Solar 실패({type(e).__name__})"

        if items:
            p["_eligibility"] = items
            p["_eligibility_how"] = how
            done += 1
        try:
            cached.write_text(
                json.dumps({"eligibility": items, "how": how,
                            "at": time.strftime("%Y-%m-%d")}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:                                 # noqa: BLE001
            pass

    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="공고문에서 지원자격 추출")
    ap.add_argument("--district", required=True)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    from match_policy import match

    r = match(args.district, top_n=args.top)
    items = [p for b in r["branches"].values() for p in b]
    seen, uniq = set(), []
    for p in items:
        if p.get("pblancId") not in seen:
            seen.add(p.get("pblancId"))
            uniq.append(p)

    print(f"상위 {args.top}건에 지원자격을 붙입니다...\n")
    enrich(uniq, args.top)
    for p in uniq[:args.top]:
        print(f"· {p['pblancNm'][:56]}")
        print(f"    첨부 {_ext(p) or '없음'} · 방법 {p.get('_eligibility_how', '-')}")
        el = p.get("_eligibility")
        print(f"    자격 {' · '.join(el) if el else '(못 구함 — 공고문 확인 안내 유지)'}\n")


if __name__ == "__main__":
    main()
