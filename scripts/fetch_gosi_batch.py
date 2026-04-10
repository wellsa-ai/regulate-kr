#!/usr/bin/env python3
"""
전체 행정규칙 목록에서 '고시' 타입만 필터하고,
처음 100개의 본문 + 별표를 수집하여 kr/ 디렉토리에 저장.

기존 10개 규정(이미 kr/에 있는)은 건드리지 않음.
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KR_DIR = BASE_DIR / "kr"

# 기존 10개 규정 (건드리지 않을 것)
EXISTING_NAMES = {
    "금융소비자보호에관한감독규정",
    "은행업감독규정",
    "보험업감독규정",
    "금융투자업규정",
    "상호저축은행업감독규정",
    "여신전문금융업감독규정",
    "신용정보업감독규정",
    "금융지주회사감독규정",
    "전자금융감독규정",
    "외국환거래규정",
}


def load_gosi_list() -> list[dict]:
    """전체 목록에서 고시만 필터."""
    all_path = DATA_DIR / "all_admrul_list.json"
    all_rules = json.loads(all_path.read_text())
    gosi_only = [r for r in all_rules if r["type"] == "고시"]
    return gosi_only


def fetch_xml(admrul_seq: str) -> str:
    """법제처 DRF API로 행정규칙 XML을 가져온다."""
    resp = httpx.get(
        "https://www.law.go.kr/DRF/lawService.do",
        params={
            "OC": "openclaw",
            "target": "admrul",
            "ID": admrul_seq,
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
    """조문내용 텍스트를 Markdown으로 변환."""
    text = text.strip()
    if not text:
        return ""

    chapter_match = re.match(
        r"^(제\d+장|제\d+편|제\d+절|제\d+관)\s*(.*)$", text
    )
    if chapter_match and len(text) < 100:
        return f"\n{text.strip()}\n"

    if text.startswith("부칙") and len(text.split("\n")[0]) < 80:
        return f"\n{text.strip()}\n"

    article_match = re.match(r"^(제\d+조(?:의\d+)?)\s*(\(.*?\))?\s*", text)
    if article_match:
        article_num = article_match.group(1)
        article_title = article_match.group(2) or ""
        if article_title:
            heading = f"##### {article_num} {article_title}"
        else:
            heading = f"##### {article_num}"
        body = text[article_match.end():].strip()
        if body:
            return f"{heading}\n\n{body}\n"
        else:
            return f"{heading}\n"

    return f"{text}\n"


def build_gosi_md(metadata: dict, articles_text: list[str]) -> str:
    """고시.md 내용을 생성."""
    title = metadata["제목"]
    source_url = f"https://www.law.go.kr/행정규칙/{title}"

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


def sanitize_dirname(name: str) -> str:
    """디렉토리명으로 사용할 수 없는 문자 제거."""
    # 파일시스템에서 문제되는 문자 제거
    name = re.sub(r'[/\\:*?"<>|]', '', name)
    # 앞뒤 공백/점 제거
    name = name.strip(". ")
    return name


def process_one(rule: dict) -> dict:
    """하나의 고시를 처리: API 호출 -> XML 파싱 -> MD 파일 생성."""
    name = rule["name"]
    seq = rule["seq"]

    # Sanitize directory name
    dirname = sanitize_dirname(name)
    if not dirname:
        return {"name": name, "seq": seq, "error": "invalid dirname"}

    out_dir = KR_DIR / dirname

    print(f"  {name} (seq={seq})...", end=" ", flush=True)

    try:
        xml_text = fetch_xml(seq)
    except Exception as e:
        print(f"FETCH ERROR: {e}")
        return {"name": name, "seq": seq, "error": f"fetch: {e}"}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"XML ERROR: {e}")
        return {"name": name, "seq": seq, "error": f"xml: {e}"}

    metadata = parse_metadata(root)
    if not metadata.get("제목"):
        print("NO METADATA")
        return {"name": name, "seq": seq, "error": "no metadata"}

    # 조문
    article_elements = root.findall("조문내용")
    articles_text = [el.text for el in article_elements if el.text]

    # 별표
    byulpyo_el = root.find("별표")
    byulpyo_units = []
    if byulpyo_el is not None:
        byulpyo_units = byulpyo_el.findall("별표단위")

    # 디렉토리 생성
    out_dir.mkdir(parents=True, exist_ok=True)

    # 고시.md 생성
    gosi_content = build_gosi_md(metadata, articles_text)
    gosi_path = out_dir / "고시.md"
    gosi_path.write_text(gosi_content, encoding="utf-8")

    # 별표 파일 생성 (간단하게 전체 텍스트만 저장)
    bp_count = 0
    for i, unit in enumerate(byulpyo_units, 1):
        content_el = unit.find("별표내용")
        if content_el is None or not content_el.text:
            continue
        bp_path = out_dir / f"별표{i}.md"
        bp_path.write_text(f"# 별표 {i}\n\n{content_el.text.strip()}\n", encoding="utf-8")
        bp_count += 1

    print(f"OK (조문:{len(articles_text)}, 별표:{bp_count})")
    return {
        "name": name,
        "seq": seq,
        "articles": len(articles_text),
        "byulpyo": bp_count,
        "dept": rule["dept"],
        "date": metadata.get("시행일자", "?"),
    }


def main():
    # 고시 목록 로드 및 필터
    gosi_list = load_gosi_list()
    print(f"전체 고시 수: {len(gosi_list)}")

    # 부처별 통계
    dept_counts = Counter(r["dept"] for r in gosi_list)
    print("\n=== 고시 부처별 통계 (상위 20) ===")
    for d, c in dept_counts.most_common(20):
        print(f"  {d}: {c}")

    # 기존 규정 제외
    new_gosi = [r for r in gosi_list if r["name"] not in EXISTING_NAMES]
    print(f"\n기존 10개 제외 후: {len(new_gosi)}개")

    # 고시 목록 JSON 저장
    gosi_list_path = DATA_DIR / "gosi_only_list.json"
    gosi_list_path.write_text(json.dumps(gosi_list, ensure_ascii=False, indent=2))
    print(f"고시 목록 저장: {gosi_list_path}")

    # 처음 100개만 본문 수집
    batch_size = 100
    batch = new_gosi[:batch_size]
    print(f"\n{'='*60}")
    print(f"본문 수집 시작: 처음 {batch_size}개")
    print(f"{'='*60}")

    results = []
    for i, rule in enumerate(batch, 1):
        print(f"[{i}/{batch_size}]", end="")
        result = process_one(rule)
        results.append(result)
        time.sleep(0.3)

    # 결과 저장
    results_path = DATA_DIR / "batch1_results.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    # 통계
    success = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    total_articles = sum(r.get("articles", 0) for r in success)
    total_bp = sum(r.get("byulpyo", 0) for r in success)

    print(f"\n{'='*60}")
    print(f"수집 완료!")
    print(f"  성공: {len(success)}/{batch_size}")
    print(f"  실패: {len(errors)}")
    print(f"  총 조문 수: {total_articles}")
    print(f"  총 별표 수: {total_bp}")
    if errors:
        print(f"\n실패 목록:")
        for r in errors:
            print(f"  - {r['name']}: {r['error']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
