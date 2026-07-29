"""마크다운 리포트를 한 장짜리 HTML 로 바꾼다.

채팅에 235줄을 쏟아내면 읽는 사람도 힘들고, 에이전트가 못 참고 요약하다가
표와 신청 링크를 버린다. 파일 한 장으로 주고 채팅은 짧게 가는 편이 낫다.

마크다운 라이브러리를 쓰지 않는다. 변환할 문법이 우리가 직접 만든 것뿐이라
(제목·표·굵게·링크·인용·목록·접이식) 필요한 만큼만 직접 옮긴다.
의존성을 늘리면 실행 환경에서 설치가 또 하나 늘어난다.

CSS 는 파일 안에 박는다. 외부에서 뭘 불러오지 않으므로 인터넷 없이 열린다.
"""

from __future__ import annotations

import html
import re

CSS = """
:root {
  --ink:#1a1a1a; --dim:#666; --line:#e5e5e5; --bg:#fff; --soft:#f7f7f8;
  --good:#0a7d3e; --bad:#c0392b; --warn:#b8860b; --accent:#1a5fb4;
}
@media (prefers-color-scheme: dark) {
  :root { --ink:#e8e8e8; --dim:#a0a0a0; --line:#333; --bg:#161618; --soft:#1f1f22;
          --good:#4ade80; --bad:#f87171; --warn:#fbbf24; --accent:#7cb0ff; }
}
* { box-sizing:border-box; }
body { margin:0; padding:2.2rem 1.2rem 4rem; background:var(--bg); color:var(--ink);
  font:16px/1.75 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif;
  -webkit-text-size-adjust:100%; }
main { max-width:860px; margin:0 auto; }
h1 { font-size:1.85rem; line-height:1.35; margin:0 0 .6rem; letter-spacing:-.02em; }
h2 { font-size:1.3rem; margin:2.6rem 0 .9rem; padding-top:1.4rem;
     border-top:1px solid var(--line); letter-spacing:-.01em; }
h2:first-of-type { border-top:0; padding-top:0; }
h3 { font-size:1.05rem; margin:1.8rem 0 .6rem; color:var(--dim); }
p { margin:.7rem 0; }
a { color:var(--accent); text-decoration:none; border-bottom:1px solid transparent; }
a:hover { border-bottom-color:currentColor; }
code { background:var(--soft); padding:.12em .4em; border-radius:4px;
       font:.88em/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
pre { background:var(--soft); padding:.9rem 1.1rem; border-radius:8px; overflow-x:auto;
      font:.85rem/1.7 ui-monospace,SFMono-Regular,Menlo,monospace; }
hr { border:0; border-top:1px solid var(--line); margin:2rem 0; }

/* 표는 좁은 화면에서 가로로만 스크롤된다. 페이지 자체는 안 밀린다. */
.tw { overflow-x:auto; margin:1rem 0; }
table { border-collapse:collapse; width:100%; font-size:.94rem; }
th,td { padding:.6rem .7rem; text-align:left; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { background:var(--soft); font-weight:600; white-space:nowrap; }
tr:last-child td { border-bottom:0; }

blockquote { margin:1rem 0; padding:.75rem 1rem; background:var(--soft);
             border-left:3px solid var(--line); border-radius:0 6px 6px 0; }
blockquote p { margin:.3rem 0; }
blockquote.tip { border-left-color:var(--warn); }
blockquote.warn { border-left-color:var(--bad); }

details { margin:1rem 0; padding:.6rem 1rem; background:var(--soft); border-radius:8px; }
summary { cursor:pointer; font-weight:600; }
details[open] summary { margin-bottom:.6rem; }

.meta { color:var(--dim); font-size:.9rem; margin-bottom:1.6rem; }
.g { display:inline-block; min-width:1.6em; padding:.05em .45em; border-radius:5px;
     font-weight:700; text-align:center; }
.gA{background:#0a7d3e;color:#fff}.gB{background:#1a5fb4;color:#fff}
.gC{background:#8b8b8b;color:#fff}.gD{background:#b8860b;color:#fff}
.gE{background:#c0392b;color:#fff}
ul,ol { margin:.7rem 0; padding-left:1.4rem; }
li { margin:.3rem 0; }
"""

_GRADE = re.compile(r"\*\*([A-E])\*\*")


def _inline(s: str) -> str:
    """굵게·코드·링크만 옮긴다. 나머지는 글자 그대로."""
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _badge(cell: str) -> str:
    """등급 한 글자는 색 배지로. 표에서 한눈에 들어와야 한다."""
    m = re.fullmatch(r"<strong>([A-E])</strong>", cell)
    return f'<span class="g g{m.group(1)}">{m.group(1)}</span>' if m else cell


def render(md: str, title: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        ln = lines[i]

        # 접이식·원시 HTML 은 그대로 통과시킨다
        if ln.lstrip().startswith(("<details", "</details", "<summary")):
            out.append(ln)
            i += 1
            continue

        if ln.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre>" + "\n".join(buf) + "</pre>")
            continue

        # 표 — 헤더 + 구분선 + 본문
        if ln.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1]):
            head = _cells(ln)
            i += 2
            body = []
            while i < n and lines[i].startswith("|"):
                body.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            trs = "".join(
                "<tr>" + "".join(f"<td>{_badge(_inline(c))}</td>" for c in r) + "</tr>"
                for r in body
            )
            out.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead>'
                       f"<tbody>{trs}</tbody></table></div>")
            continue

        if ln.startswith("> "):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            body = "\n".join(buf)
            cls = " class=\"tip\"" if "💡" in body or "💬" in body or "💰" in body else (
                  " class=\"warn\"" if "⚠️" in body or "❓" in body else "")
            inner = "".join(f"<p>{_inline(b)}</p>" for b in buf if b)
            out.append(f"<blockquote{cls}>{inner}</blockquote>")
            continue

        if ln.startswith("- "):
            buf = []
            while i < n and lines[i].startswith("- "):
                buf.append(lines[i][2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in buf) + "</ul>")
            continue

        if re.match(r"^\d+\. ", ln):
            buf = []
            while i < n and re.match(r"^\d+\. ", lines[i]):
                buf.append(re.sub(r"^\d+\. ", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(b)}</li>" for b in buf) + "</ol>")
            continue

        if ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>")
        elif ln.strip() == "---":
            pass                              # h2 위 선이 이미 구분 역할을 한다
        elif ln.strip():
            # 전각 공백(　)으로 들여쓴 정책 카드 줄은 그대로 살린다
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1

    return (
        f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{''.join(out)}</main></body></html>"
    )
