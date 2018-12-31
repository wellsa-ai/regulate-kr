"""law.go.kr 행정규칙 스크래핑 스크립트.

miniverse scrape API를 사용하여 금융 감독규정 본문을 수집합니다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from convert import REGULATIONS

MINIVERSE_URL = "http://localhost:7749"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"


def login() -> str:
    """miniverse 로그인 → Bearer token."""
    resp = httpx.post(
        f"{MINIVERSE_URL}/api/v1/auth/login",
        json={"username": "admin", "password": "admin1234"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def scrape_regulation(token: str, seq: str) -> str:
    """admRulSeq로 행정규칙 본문 스크래핑."""
    url = f"https://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq={seq}&chrClsCd=010201"
    resp = httpx.post(
        f"{MINIVERSE_URL}/api/v1/tools/scrape",
        json={"url": url, "action": "fetch_js"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=90.0,
    )
    resp.raise_for_status()
    data = resp.json()

    result = data.get("result", "")
    if isinstance(result, str):
        try:
            inner = json.loads(result)
            return inner.get("content", "")
        except (json.JSONDecodeError, TypeError):
            return result
    return str(data)


def scrape_all() -> None:
    """모든 규정 스크래핑."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = login()
    print(f"Logged in to miniverse")

    for reg in REGULATIONS:
        out_file = OUTPUT_DIR / f"{reg['seq']}.txt"
        if out_file.exists() and out_file.stat().st_size > 1000:
            print(f"  SKIP {reg['name']} (already scraped)")
            continue

        print(f"  Scraping {reg['name']}...", end=" ", flush=True)
        try:
            content = scrape_regulation(token, reg["seq"])
            out_file.write_text(content, encoding="utf-8")
            has_article = "제1조" in content
            print(f"OK ({len(content)} chars, 제1조={'Y' if has_article else 'N'})")
        except Exception as e:
            print(f"FAIL ({e})")

        time.sleep(2)  # rate limit

    print("\nDone!")


if __name__ == "__main__":
    scrape_all()
