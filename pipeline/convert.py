"""law.go.kr 행정규칙 스크래핑 텍스트 → Markdown 변환 파이프라인."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# 금융위원회/기획재정부 주요 감독규정 목록
REGULATIONS = [
    {"name": "금융소비자보호에관한감독규정", "seq": "2100000276850", "부처": "금융위원회"},
    {"name": "은행업감독규정", "seq": "2100000276094", "부처": "금융위원회"},
    {"name": "보험업감독규정", "seq": "2100000272874", "부처": "금융위원회"},
    {"name": "금융투자업규정", "seq": "2100000275618", "부처": "금융위원회"},
    {"name": "상호저축은행업감독규정", "seq": "2100000272466", "부처": "금융위원회"},
    {"name": "여신전문금융업감독규정", "seq": "2100000272458", "부처": "금융위원회"},
    {"name": "신용정보업감독규정", "seq": "2100000254238", "부처": "금융위원회"},
    {"name": "금융지주회사감독규정", "seq": "2100000266376", "부처": "금융위원회"},
    {"name": "전자금융감독규정", "seq": "2100000274812", "부처": "금융위원회"},
    {"name": "외국환거래규정", "seq": "2100000276526", "부처": "기획재정부"},
]

# 조문 패턴: 제N조, 제N조의N
_ARTICLE_RE = re.compile(
    r"(제\s*\d+조(?:의\d+)?)\s*\(([^)]+)\)",
)


def extract_metadata(raw_text: str, reg: dict[str, str]) -> dict[str, Any]:
    """스크래핑 텍스트에서 메타데이터 추출."""
    meta: dict[str, Any] = {
        "제목": reg["name"],
        "행정규칙구분": "고시",
        "소관부처": reg["부처"],
        "admRulSeq": reg["seq"],
        "출처": f"https://www.law.go.kr/행정규칙/{reg['name']}",
    }

    # 시행일자 추출: [시행 YYYY. M. D.]
    date_match = re.search(r"\[시행\s+(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]", raw_text)
    if date_match:
        y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
        meta["시행일자"] = date(y, m, d).isoformat()

    # 발령번호 추출: [금융위원회고시 제YYYY-NN호, ...]
    num_match = re.search(r"\[(\S+고시\s+제[\d\-]+호)", raw_text)
    if num_match:
        meta["발령번호"] = num_match.group(1)

    return meta


def parse_articles(raw_text: str) -> list[dict[str, str | None]]:
    """스크래핑 텍스트에서 조문 파싱."""
    # 본문 시작 위치 찾기 — 제1조 이전의 메타데이터/네비게이션 제거
    first_article = re.search(r"제\s*1\s*조\s*\(", raw_text)
    if not first_article:
        return []

    body = raw_text[first_article.start():]

    # 조문 분리
    matches = list(_ARTICLE_RE.finditer(body))
    if not matches:
        return []

    articles: list[dict[str, str | None]] = []
    for i, m in enumerate(matches):
        article_no = re.sub(r"\s+", "", m.group(1))  # 공백 제거: "제 1 조" → "제1조"
        heading = m.group(2).strip()

        # content: 현재 조문 헤딩 끝 ~ 다음 조문 헤딩 시작
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()

        # 연속 빈줄 정리
        content = re.sub(r"\n{3,}", "\n\n", content)

        articles.append({
            "article_no": article_no,
            "heading": heading,
            "content": content,
        })

    return articles


def to_markdown(raw_text: str, reg: dict[str, str]) -> str:
    """스크래핑 텍스트 → YAML frontmatter + Markdown 조문."""
    meta = extract_metadata(raw_text, reg)
    articles = parse_articles(raw_text)

    # YAML frontmatter
    fm = yaml.dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    lines = ["---", fm.strip(), "---", ""]

    # 규정 제목
    lines.append(f"# {meta['제목']}")
    lines.append("")

    # 조문
    for art in articles:
        lines.append(f"##### {art['article_no']} ({art['heading']})")
        lines.append("")
        if art["content"]:
            lines.append(art["content"])
            lines.append("")

    return "\n".join(lines)


def convert_all(raw_dir: Path, output_dir: Path) -> dict[str, Any]:
    """raw 텍스트 파일들을 Markdown으로 변환."""
    stats = {"total": 0, "converted": 0, "errors": 0}

    seq_to_reg = {r["seq"]: r for r in REGULATIONS}

    for txt_file in sorted(raw_dir.glob("*.txt")):
        stats["total"] += 1
        seq = txt_file.stem

        reg = seq_to_reg.get(seq)
        if not reg:
            print(f"  SKIP {seq} (unknown regulation)")
            stats["errors"] += 1
            continue

        raw_text = txt_file.read_text(encoding="utf-8")
        if "제1조" not in raw_text:
            print(f"  SKIP {reg['name']} (no articles found)")
            stats["errors"] += 1
            continue

        md = to_markdown(raw_text, reg)
        articles = parse_articles(raw_text)

        # 저장: kr/{규정명}/고시.md
        reg_dir = output_dir / reg["name"]
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "고시.md").write_text(md, encoding="utf-8")

        print(f"  OK {reg['name']}: {len(articles)}조")
        stats["converted"] += 1

    return stats


if __name__ == "__main__":
    import sys

    raw_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../data/regulate-kr")
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("kr")

    print(f"Input:  {raw_dir}")
    print(f"Output: {output_dir}")
    print()

    stats = convert_all(raw_dir, output_dir)
    print(f"\nDone: {stats}")
