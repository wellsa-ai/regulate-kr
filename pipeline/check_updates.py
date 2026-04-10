"""감독규정 업데이트 체크 + 자동 스크래핑 파이프라인.

GitHub Actions에서 주기적으로 실행하여 law.go.kr 연혁 목록을 확인하고,
새 버전이 있으면 스크래핑 → Markdown 변환 → Git commit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REGULATIONS = [
    {"name": "금융소비자보호에관한감독규정", "seq": "2100000276850", "dept": "금융위원회"},
    {"name": "은행업감독규정", "seq": "2100000276094", "dept": "금융위원회"},
    {"name": "보험업감독규정", "seq": "2100000272874", "dept": "금융위원회"},
    {"name": "금융투자업규정", "seq": "2100000275618", "dept": "금융위원회"},
    {"name": "상호저축은행업감독규정", "seq": "2100000272466", "dept": "금융위원회"},
    {"name": "여신전문금융업감독규정", "seq": "2100000272458", "dept": "금융위원회"},
    {"name": "신용정보업감독규정", "seq": "2100000254238", "dept": "금융위원회"},
    {"name": "금융지주회사감독규정", "seq": "2100000266376", "dept": "금융위원회"},
    {"name": "전자금융감독규정", "seq": "2100000274812", "dept": "금융위원회"},
    {"name": "외국환거래규정", "seq": "2100000276526", "dept": "기획재정부"},
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KR_DIR = PROJECT_ROOT / "kr"
STATE_FILE = PROJECT_ROOT / "pipeline" / "state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_latest_versions(seq: str) -> list[dict]:
    """law.go.kr 연혁 API에서 최신 버전 목록 조회."""
    resp = httpx.get(
        "https://www.law.go.kr/LSW/admRulHstListR.do",
        params={"admRulSeq": seq},
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (regulate-kr bot)"},
    )
    resp.raise_for_status()

    seqs = re.findall(r"admRulViewHst\('Y','(\d+)'\)", resp.text)
    dates = re.findall(r"\[시행\s+(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]", resp.text)
    details = re.findall(r"\[([^\]]*고시[^\]]*)\]", resp.text)

    versions = []
    for i, (vseq, (y, m, d)) in enumerate(zip(seqs, dates)):
        detail = details[i] if i < len(details) else ""
        amendment = "제정" if "제정" in detail else "일부개정"
        versions.append({
            "seq": vseq,
            "date": f"{y}-{int(m):02d}-{int(d):02d}",
            "detail": detail,
            "amendment": amendment,
        })

    return versions


def scrape_version(seq: str) -> str:
    """Playwright로 규정 본문 스크래핑."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed, trying httpx fallback")
        return _httpx_fallback(seq)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        url = f"https://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq={seq}&chrClsCd=010202"
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        text = page.evaluate(
            "() => document.querySelector('#conScroll')?.innerText || document.body.innerText"
        )
        browser.close()
        return text


def _httpx_fallback(seq: str) -> str:
    """Playwright 없을 때 httpx로 시도 (본문 불완전할 수 있음)."""
    resp = httpx.get(
        f"https://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq={seq}&chrClsCd=010202",
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    )
    return resp.text


def git_commit(reg_name: str, version: dict) -> None:
    """Git add + commit with historical date."""
    date_str = f"{version['date']}T00:00:00+09:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": date_str,
        "GIT_COMMITTER_DATE": date_str,
    }

    subprocess.run(["git", "add", f"kr/{reg_name}/"], cwd=PROJECT_ROOT, check=True)
    subprocess.run(
        [
            "git", "commit", "-m",
            f"고시: {reg_name} ({version['amendment']})\n\n"
            f"시행일자: {version['date']}\n"
            f"{version['detail']}\n"
            f"admRulSeq: {version['seq']}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )


def check_and_update() -> dict:
    """모든 규정의 업데이트 확인 + 반영."""
    state = load_state()
    stats = {"checked": 0, "updated": 0, "errors": 0}

    # lazy import
    sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))
    from convert import parse_articles, to_markdown

    for reg in REGULATIONS:
        stats["checked"] += 1
        print(f"\n[{reg['name']}]")

        try:
            versions = get_latest_versions(reg["seq"])
            if not versions:
                print("  no versions found")
                continue

            latest = versions[0]
            known_seq = state.get(reg["name"], {}).get("latest_seq")

            if latest["seq"] == known_seq:
                print(f"  up to date (seq={latest['seq']}, {latest['date']})")
                continue

            print(f"  NEW: seq={latest['seq']}, {latest['date']} ({latest['amendment']})")

            # scrape
            raw_text = scrape_version(latest["seq"])
            if "제1조" not in raw_text:
                print("  WARNING: no articles found in scraped text")
                stats["errors"] += 1
                continue

            # convert to markdown
            md = to_markdown(raw_text, reg)

            # save
            reg_dir = KR_DIR / reg["name"]
            reg_dir.mkdir(parents=True, exist_ok=True)
            (reg_dir / "고시.md").write_text(md, encoding="utf-8")

            articles = parse_articles(raw_text)
            print(f"  saved: {len(articles)}조")

            # git commit
            git_commit(reg["name"], latest)
            print(f"  committed: {latest['date']}")

            # update state
            state[reg["name"]] = {
                "latest_seq": latest["seq"],
                "latest_date": latest["date"],
                "updated_at": datetime.now().isoformat(),
            }
            save_state(state)
            stats["updated"] += 1

        except Exception as e:
            print(f"  ERROR: {e}")
            stats["errors"] += 1

        time.sleep(2)  # rate limit

    print(f"\nDone: {stats}")
    return stats


if __name__ == "__main__":
    check_and_update()
