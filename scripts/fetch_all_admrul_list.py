#!/usr/bin/env python3
"""
법제처 DRF API로 대한민국 전체 행정규칙 목록을 수집.
페이징하면서 전체 리스트를 JSON으로 저장.
"""

import re
import json
import time
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

all_rules = []
page = 1

while True:
    print(f"Page {page}...", end=" ", flush=True)
    resp = httpx.get(
        "https://www.law.go.kr/DRF/lawSearch.do",
        params={
            "OC": "openclaw",
            "target": "admrul",
            "query": "",
            "type": "XML",
            "display": "100",
            "page": str(page),
        },
        timeout=30,
    )

    names = re.findall(r"<행정규칙명><!\[CDATA\[([^\]]+)\]\]>", resp.text)
    seqs = re.findall(r"<행정규칙일련번호>(\d+)</행정규칙일련번호>", resp.text)
    depts = re.findall(r"<소관부처명>([^<]+)</소관부처명>", resp.text)
    types = re.findall(r"<행정규칙종류>([^<]+)</행정규칙종류>", resp.text)

    if not names:
        print("empty -> done")
        break

    print(f"{len(names)} items")

    for n, s, d, t in zip(names, seqs, depts, types):
        all_rules.append({"name": n, "seq": s, "dept": d, "type": t})

    page += 1
    time.sleep(0.3)

# JSON으로 저장
out_path = DATA_DIR / "all_admrul_list.json"
out_path.write_text(json.dumps(all_rules, ensure_ascii=False, indent=2))
print(f"\nTotal: {len(all_rules)} rules saved to {out_path}")

# 통계 출력
from collections import Counter

type_counts = Counter(r["type"] for r in all_rules)
dept_counts = Counter(r["dept"] for r in all_rules)

print("\n=== 행정규칙종류별 통계 ===")
for t, c in type_counts.most_common():
    print(f"  {t}: {c}")

print(f"\n=== 부처별 통계 (상위 30) ===")
for d, c in dept_counts.most_common(30):
    print(f"  {d}: {c}")
