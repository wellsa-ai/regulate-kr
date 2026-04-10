#!/usr/bin/env python3
"""
법제처 DRF API로 금융 감독규정 10개를 XML로 받아 Markdown으로 변환.
기존 Synap 뷰어 스크래핑 결과를 API 원본으로 교체.
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent / "kr"

REGULATIONS = [
    ("금융소비자보호에관한감독규정", "2100000276850"),
    ("은행업감독규정", "2100000276094"),
    ("보험업감독규정", "2100000272874"),
    ("금융투자업규정", "2100000275618"),
    ("상호저축은행업감독규정", "2100000272466"),
    ("여신전문금융업감독규정", "2100000272458"),
    ("신용정보업감독규정", "2100000254238"),
    ("금융지주회사감독규정", "2100000266376"),
    ("전자금융감독규정", "2100000274812"),
    ("외국환거래규정", "2100000276526"),
]


def fetch_xml(admrul_id: str) -> str:
    """법제처 DRF API로 행정규칙 XML을 가져온다."""
    resp = httpx.get(
        "https://www.law.go.kr/DRF/lawService.do",
        params={
            "OC": "openclaw",
            "target": "admrul",
            "ID": admrul_id,
            "type": "XML",
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text


def parse_metadata(root: ET.Element) -> dict:
    """행정규칙기본정보에서 메타데이터 추출."""
    meta_el = root.find("행정규칙기본정보")
    if meta_el is None:
        return {}

    fields = {}
    for child in meta_el:
        if child.text:
            fields[child.tag] = child.text

    # 시행일자 포맷: YYYYMMDD -> YYYY-MM-DD
    raw_date = fields.get("시행일자", "")
    if len(raw_date) == 8:
        formatted_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        formatted_date = raw_date

    return {
        "제목": fields.get("행정규칙명", ""),
        "행정규칙구분": fields.get("행정규칙종류", ""),
        "소관부처": fields.get("소관부처명", ""),
        "시행일자": formatted_date,
        "발령번호": fields.get("발령번호", ""),
        "admRulSeq": fields.get("행정규칙일련번호", ""),
    }


def format_article_text(text: str) -> str:
    """조문내용 텍스트를 Markdown으로 변환.

    - 제N조(...) -> ##### 제N조 (...)
    - 제N장/편/절 제목 -> 장/편/절 그대로
    - 항/호 들여쓰기 정리
    """
    text = text.strip()
    if not text:
        return ""

    # 장/편/절 제목 (e.g. "제1장 총칙") -> 그대로 빈줄 넣어서
    chapter_match = re.match(
        r"^(제\d+장|제\d+편|제\d+절|제\d+관)\s*(.*)$", text
    )
    if chapter_match and len(text) < 100:
        return f"\n{text.strip()}\n"

    # 부칙
    if text.startswith("부칙") and len(text.split("\n")[0]) < 80:
        return f"\n{text.strip()}\n"

    # 제N조 패턴 -> ##### heading
    article_match = re.match(r"^(제\d+조(?:의\d+)?)\s*(\(.*?\))?\s*", text)
    if article_match:
        article_num = article_match.group(1)
        article_title = article_match.group(2) or ""
        # Space between 조번호 and 제목
        if article_title:
            heading = f"##### {article_num} {article_title}"
        else:
            heading = f"##### {article_num}"
        body = text[article_match.end():].strip()

        # 본문 정리: 각 항은 이미 들여쓰기로 구분됨
        if body:
            return f"{heading}\n\n{body}\n"
        else:
            return f"{heading}\n"

    # 그 외 (삭제 등) -> 그대로
    return f"{text}\n"


def build_gosi_md(metadata: dict, articles_text: list[str]) -> str:
    """고시.md 내용을 생성."""
    title = metadata["제목"]
    source_url = f"https://www.law.go.kr/행정규칙/{title}"

    # YAML frontmatter
    lines = [
        "---",
        f"제목: {title}",
        f"행정규칙구분: {metadata['행정규칙구분']}",
        f"소관부처: {metadata['소관부처']}",
        f'시행일자: "{metadata["시행일자"]}"',
        f'발령번호: "{metadata["발령번호"]}"',
        f'admRulSeq: "{metadata["admRulSeq"]}"',
        f"출처: {source_url}",
        "---",
        "",
        f"# {title}",
        "",
    ]

    for raw_text in articles_text:
        formatted = format_article_text(raw_text)
        if formatted:
            lines.append(formatted)

    return "\n".join(lines)


def _normalize_num(num_str: str) -> str:
    """Normalize number part for filenames: '2의3' stays, '2-3' stays as-is."""
    return num_str.strip()


def _skip_amendments(text: str) -> str:
    """Skip leading amendment tags like <개정 ...>, <전문개정>, <신설 ...> etc."""
    while True:
        m = re.match(r"\s*[<(〈\[]\s*(?:개정|전문개정|신설|삭제|일부개정)[^>)〉\]]*[>)〉\]]\s*", text)
        if m:
            text = text[m.end():]
        else:
            break
    return text.strip()


def _first_meaningful_line(text: str) -> str:
    """Get first non-empty line from text."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def extract_byulpyo_title(text: str) -> tuple[str, str]:
    """별표/별지 내용에서 파일명과 표시 제목을 추출.

    Returns: (filename_stem, display_title)

    지원하는 패턴:
      <별표 1>, <별표 2의2>, <별표 1-2>, <별표2-7>
      ■ 규정명 [별표 N]
      <별지 제1호 서식>, <별지 제1-1호 서식>
      <별지 제2호의3>, <별지 제2의2호>
      〈별지 제1호 서식〉
      [별지 제N호 서식], [별지 N]
      <별지 N>
    """
    text = text.strip()

    # ──────────────────────────────────────────────────────
    # Pattern A: ■ 규정명 [별표 N] (금융소비자보호, 외국환거래)
    # ──────────────────────────────────────────────────────
    bp_bullet = re.match(
        r"■\s*.*?\[(별표\s*(\d+(?:의\d+)?))\]",
        text,
    )
    if bp_bullet:
        label = bp_bullet.group(1).strip()      # "별표 1"
        num_part = bp_bullet.group(2).strip()    # "1"
        after = text[bp_bullet.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        filename = f"별표{num_part}"
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    # ──────────────────────────────────────────────────────
    # Pattern B: <별표 N>, <별표 N의N>, <별표 N-N>, <별표N-N>
    #   Brackets: < >, 〈 〉
    # ──────────────────────────────────────────────────────
    bp_angle = re.match(
        r"[<〈]\s*(별표\s*(\d+(?:(?:의|-)\d+)?))\s*[>〉]",
        text,
    )
    if bp_angle:
        label = bp_angle.group(1).strip()
        num_raw = bp_angle.group(2).strip()
        # Normalize: "별표 1-2" -> filename "별표1-2"
        after = text[bp_angle.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        filename = f"별표{num_raw}"
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    # Pattern B2: [별표 N], [별표 N의N] (square brackets)
    bp_square = re.match(
        r"\[(별표\s*(\d+(?:(?:의|-)\d+)?))\]",
        text,
    )
    if bp_square:
        label = bp_square.group(1).strip()
        num_raw = bp_square.group(2).strip()
        after = text[bp_square.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        filename = f"별표{num_raw}"
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    # Pattern B3: [규정명 별표 N] (e.g. [금융투자업규정 별표 9])
    bp_named = re.match(
        r"\[.+?(별표\s*(\d+(?:(?:의|-)\d+)?))\]",
        text,
    )
    if bp_named:
        label = bp_named.group(1).strip()
        num_raw = bp_named.group(2).strip()
        after = text[bp_named.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        filename = f"별표{num_raw}"
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    # ──────────────────────────────────────────────────────
    # Pattern C: 별지 with various formats
    #   <별지 제N호 서식>, <별지 제N호>, <별지 N>
    #   <별지 제N-N호 서식>, <별지 제N호의N 서식>, <별지 제N호의N>
    #   〈별지 제N호 서식〉, [별지 제N호 서식], [별지 N]
    # ──────────────────────────────────────────────────────
    # Comprehensive 별지 pattern
    # Handles: 별지 제N호, 별지 제N-N호, 별지 제N호의N, 별지 제N의N호, 별지 N
    bj_match = re.match(
        r"[<〈\[]\s*(별지)\s*"
        r"(?:제\s*)?"
        r"(\d+)"                                      # 주번호
        r"(?:\s*[-]\s*(\d+))?"                         # 대시 부번호 (N-N호)
        r"(?:\s*의\s*(\d+))?"                          # 의 부번호 (N의N호 or N호의N)
        r"(?:\s*호)?"                                  # optional 호
        r"(?:\s*의\s*(\d+))?"                          # 호의 부번호 (N호의N)
        r"(?:\s*서식)?"                                # 서식 (선택)
        r"\s*[>〉\]]",
        text,
    )
    if bj_match:
        num1 = bj_match.group(2)
        dash_num = bj_match.group(3)       # N-N
        ui_num1 = bj_match.group(4)        # 의N (before 호)
        ui_num2 = bj_match.group(5)        # 의N (after 호)
        ui_num = ui_num1 or ui_num2

        # Reconstruct label for display
        if dash_num:
            filename = f"별지{num1}-{dash_num}"
        elif ui_num:
            filename = f"별지{num1}의{ui_num}"
        else:
            filename = f"별지{num1}"

        after = text[bj_match.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)

        # Reconstruct display label from original match text
        orig_label = text[: bj_match.end()]
        orig_label = re.sub(r"^[<〈\[]\s*", "", orig_label)
        orig_label = re.sub(r"\s*[>〉\]]$", "", orig_label)
        display = f"[{orig_label}] {title_line}" if title_line else f"[{orig_label}]"
        return filename, display

    # ──────────────────────────────────────────────────────
    # Fallback: try to extract any 별표/별지 reference from text
    # ──────────────────────────────────────────────────────
    # Look for [별표 N] or [별지 ...] anywhere in first 200 chars
    fb = re.search(r"\[(별표\s*(\d+(?:(?:의|-)\d+)?))\]", text[:200])
    if fb:
        label = fb.group(1).strip()
        num_raw = fb.group(2).strip()
        after = text[fb.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        filename = f"별표{num_raw}"
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    fb2 = re.search(r"\[(별지[^\]]*)\]", text[:200])
    if fb2:
        label = fb2.group(1).strip()
        # Extract number from label
        num_m = re.search(r"(\d+)(?:[-](\d+))?(?:의(\d+))?", label)
        if num_m:
            n1 = num_m.group(1)
            n2 = num_m.group(2)
            n3 = num_m.group(3)
            if n2:
                filename = f"별지{n1}-{n2}"
            elif n3:
                filename = f"별지{n1}의{n3}"
            else:
                filename = f"별지{n1}"
        else:
            filename = f"별지_unknown"
        after = text[fb2.end():]
        after = _skip_amendments(after)
        title_line = _first_meaningful_line(after)
        display = f"[{label}] {title_line}" if title_line else f"[{label}]"
        return filename, display

    # Last resort: use sequential numbering (caller handles collision)
    first_line = _first_meaningful_line(text)
    return "unknown", first_line or "(untitled)"


def build_byulpyo_md(title: str, content: str) -> str:
    """별표/별지 Markdown 파일 내용을 생성."""
    return f"# {title}\n\n{content.strip()}\n"


def process_one(name: str, admrul_id: str) -> dict:
    """하나의 규정을 처리: API 호출 -> XML 파싱 -> MD 파일 생성."""
    print(f"\n{'='*60}")
    print(f"처리중: {name} (ID: {admrul_id})")
    print(f"{'='*60}")

    # 1) API 호출
    print("  API 호출중...")
    xml_text = fetch_xml(admrul_id)
    print(f"  XML 수신: {len(xml_text):,} bytes")

    # 2) XML 파싱
    root = ET.fromstring(xml_text)

    # 메타데이터
    metadata = parse_metadata(root)
    print(f"  제목: {metadata.get('제목', '?')}")
    print(f"  시행일자: {metadata.get('시행일자', '?')}")
    print(f"  발령번호: {metadata.get('발령번호', '?')}")

    # 조문
    article_elements = root.findall("조문내용")
    articles_text = [el.text for el in article_elements if el.text]
    print(f"  조문: {len(articles_text)}개")

    # 별표
    byulpyo_el = root.find("별표")
    byulpyo_units = []
    if byulpyo_el is not None:
        byulpyo_units = byulpyo_el.findall("별표단위")
    print(f"  별표/별지: {len(byulpyo_units)}개")

    # 3) 디렉토리 준비
    out_dir = BASE_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)

    # 기존 파일 삭제 (교체 목적) -- 고시.md 외 모든 .md 삭제
    for old_file in out_dir.glob("*.md"):
        if old_file.name != "고시.md":  # 고시.md도 곧 덮어쓸 것이므로 남겨둬도 OK
            old_file.unlink()

    # 4) 고시.md 생성
    gosi_content = build_gosi_md(metadata, articles_text)
    gosi_path = out_dir / "고시.md"
    gosi_path.write_text(gosi_content, encoding="utf-8")
    print(f"  -> {gosi_path.name} ({len(articles_text)} 조문)")

    # 5) 별표 파일 생성
    bp_count = 0
    bj_count = 0
    used_filenames = set()
    unknown_counter = 0
    for unit in byulpyo_units:
        content_el = unit.find("별표내용")
        if content_el is None or not content_el.text:
            continue

        raw_content = content_el.text
        filename_stem, display_title = extract_byulpyo_title(raw_content)

        # Handle "unknown" fallback with sequential numbering
        if filename_stem == "unknown":
            unknown_counter += 1
            filename_stem = f"기타{unknown_counter}"

        # Handle filename collisions
        if filename_stem in used_filenames:
            suffix = 2
            while f"{filename_stem}_{suffix}" in used_filenames:
                suffix += 1
            filename_stem = f"{filename_stem}_{suffix}"
        used_filenames.add(filename_stem)

        md_content = build_byulpyo_md(display_title, raw_content)
        md_path = out_dir / f"{filename_stem}.md"
        md_path.write_text(md_content, encoding="utf-8")

        if filename_stem.startswith("별표"):
            bp_count += 1
        elif filename_stem.startswith("별지"):
            bj_count += 1
        else:
            bj_count += 1  # count 기타 as 별지
        print(f"  -> {md_path.name}: {display_title[:60]}")

    result = {
        "name": name,
        "admrul_id": admrul_id,
        "xml_bytes": len(xml_text),
        "articles": len(articles_text),
        "byulpyo": bp_count,
        "byulji": bj_count,
        "title": metadata.get("제목", name),
        "date": metadata.get("시행일자", "?"),
    }
    print(f"  완료: 조문 {len(articles_text)}, 별표 {bp_count}, 별지 {bj_count}")
    return result


def main():
    print("=" * 60)
    print("법제처 DRF API -> Markdown 변환")
    print(f"대상: {len(REGULATIONS)}개 규정")
    print(f"저장: {BASE_DIR}")
    print("=" * 60)

    results = []
    for i, (name, admrul_id) in enumerate(REGULATIONS, 1):
        print(f"\n[{i}/{len(REGULATIONS)}]", end="")
        try:
            result = process_one(name, admrul_id)
            results.append(result)
        except Exception as e:
            print(f"\n  ERROR: {e}")
            results.append({
                "name": name,
                "admrul_id": admrul_id,
                "error": str(e),
            })

        # API 예의: 1초 대기
        if i < len(REGULATIONS):
            time.sleep(1)

    # 전체 통계
    print("\n" + "=" * 60)
    print("전체 통계")
    print("=" * 60)
    print(f"{'규정명':<30} {'조문':>5} {'별표':>5} {'별지':>5} {'시행일자':<12}")
    print("-" * 70)
    total_articles = 0
    total_bp = 0
    total_bj = 0
    errors = 0
    for r in results:
        if "error" in r:
            print(f"{r['name']:<30} ERROR: {r['error']}")
            errors += 1
        else:
            print(
                f"{r['name']:<30} {r['articles']:>5} {r['byulpyo']:>5} "
                f"{r['byulji']:>5} {r['date']:<12}"
            )
            total_articles += r["articles"]
            total_bp += r["byulpyo"]
            total_bj += r["byulji"]

    print("-" * 70)
    print(
        f"{'합계':<30} {total_articles:>5} {total_bp:>5} "
        f"{total_bj:>5}"
    )
    if errors:
        print(f"오류: {errors}건")
    print(f"\n완료! {len(results) - errors}/{len(REGULATIONS)} 성공")


if __name__ == "__main__":
    main()
