"""법제처 행정규칙 연혁 스크래핑 → Git 커밋 파이프라인.

각 규정의 과거 버전을 스크래핑하고, 시행일자를 기준으로
Git 커밋을 생성하여 연혁을 버전 관리합니다.

Usage:
    cd /Users/sammy/workspaces/regulate-kr/pipeline
    python3 scrape_history.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import sync_playwright

from convert import REGULATIONS, to_markdown, parse_articles, extract_metadata

# ─── 설정 ───
REPO_DIR = Path(__file__).parent.parent
KR_DIR = REPO_DIR / "kr"
BACKUP_DIR = REPO_DIR / "tmp" / "kr_backup"
MAX_VERSIONS_PER_REG = 5
DELAY_SECONDS = 5
HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ─── Phase 1: 연혁 목록 수집 ───

def fetch_version_list(seq: str, reg_name: str) -> list[dict]:
    """연혁 목록 페이지에서 버전 seq, 시행일자, 발령정보 추출."""
    url = "https://www.law.go.kr/LSW/admRulHstListR.do"
    resp = httpx.get(
        url,
        params={"admRulSeq": seq},
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    html = resp.text

    version_seqs = re.findall(r"admRulViewHst\('Y','(\d+)'\)", html)
    dates_raw = re.findall(
        r"\[시행\s+(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]", html
    )
    # 발령 정보 - 고시 또는 훈령
    details = re.findall(r"\[([^\]]*(?:고시|훈령|규정)[^\]]*(?:제정|개정|폐지)[^\]]*)\]", html)
    if not details:
        details = re.findall(r"\[([^\]]*고시[^\]]*)\]", html)

    versions = []
    for i, vseq in enumerate(version_seqs):
        v: dict = {"seq": vseq, "reg_name": reg_name}

        if i < len(dates_raw):
            y, m, d = (
                int(dates_raw[i][0]),
                int(dates_raw[i][1]),
                int(dates_raw[i][2]),
            )
            v["date"] = datetime(y, m, d)
            v["date_str"] = f"{y:04d}-{m:02d}-{d:02d}"
        else:
            v["date"] = None
            v["date_str"] = None

        if i < len(details):
            v["detail"] = details[i]
            kind_match = re.search(r"(제정|일부개정|전부개정|폐지)", details[i])
            v["kind"] = kind_match.group(1) if kind_match else "개정"
        else:
            v["detail"] = ""
            v["kind"] = "개정"

        versions.append(v)

    return versions


# ─── Phase 2: 스크래핑 ───

def scrape_version_content(page, seq: str) -> str:
    """Playwright로 특정 버전의 본문 스크래핑."""
    url = f"https://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq={seq}&chrClsCd=010202"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(DELAY_SECONDS)

    try:
        el = page.query_selector("#conScroll")
        if el:
            text = el.inner_text()
        else:
            text = page.inner_text("body")
    except Exception:
        text = page.inner_text("body")

    return text


# ─── Phase 3: Git 커밋 ───

def git_run(*args, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Git 명령 실행 헬퍼."""
    full_env = os.environ.copy()
    if env_extra:
        full_env.update(env_extra)
    return subprocess.run(
        ["git"] + list(args),
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        env=full_env,
    )


def git_commit_version(
    reg_name: str, md_content: str, date_str: str, kind: str, detail: str
) -> bool:
    """고시.md를 업데이트하고 백데이팅된 Git 커밋 생성."""
    reg_dir = KR_DIR / reg_name
    reg_dir.mkdir(parents=True, exist_ok=True)
    md_file = reg_dir / "고시.md"
    md_file.write_text(md_content, encoding="utf-8")

    rel_path = str(md_file.relative_to(REPO_DIR))
    git_run("add", rel_path)

    # 변경사항 확인
    diff_result = git_run("diff", "--cached", "--quiet")
    if diff_result.returncode == 0:
        print(f"    (no changes, skip)")
        return False

    # 커밋
    commit_msg = f"고시: {reg_name} ({kind})"
    if detail:
        commit_msg += f"\n\n{detail}"

    git_date = f"{date_str}T00:00:00+09:00"
    result = git_run(
        "commit", "-m", commit_msg,
        env_extra={
            "GIT_AUTHOR_DATE": git_date,
            "GIT_COMMITTER_DATE": git_date,
        },
    )
    if result.returncode != 0:
        print(f"    GIT ERROR: {result.stderr.strip()}")
        return False

    return True


# ─── 메인 ───

def main():
    total_scraped = 0
    total_committed = 0
    errors: list[tuple[str, str, str]] = []

    # ━━━ Phase 1: 연혁 목록 수집 ━━━
    print("=" * 60)
    print("Phase 1: 연혁 목록 수집")
    print("=" * 60)

    all_tasks: list[tuple[dict, dict]] = []

    for reg in REGULATIONS:
        name = reg["name"]
        print(f"\n{name} (seq={reg['seq']})...")
        try:
            versions = fetch_version_list(reg["seq"], name)
            print(f"  총 {len(versions)}개 버전")

            # 현행(첫 번째) 포함하여 최근 MAX_VERSIONS_PER_REG+1 개 수집
            # 현행도 다시 커밋해야 하므로 (히스토리 재구축)
            selected = versions[:MAX_VERSIONS_PER_REG + 1]
            for v in selected:
                print(f"    {v['date_str']} | seq={v['seq']} | {v.get('kind', '?')}")
                all_tasks.append((reg, v))

            time.sleep(1)
        except Exception as e:
            print(f"  ERROR: {e}")
            errors.append((name, "version_list", str(e)))

    # 오래된 순으로 정렬
    all_tasks.sort(key=lambda x: x[1].get("date") or datetime.min)

    print(f"\n총 {len(all_tasks)}개 버전 처리 예정")
    print()

    # ━━━ Phase 1.5: Git 히스토리 리셋 ━━━
    print("=" * 60)
    print("Phase 1.5: Git 히스토리 준비")
    print("=" * 60)

    # 현행 kr/ 백업
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    if KR_DIR.exists():
        shutil.copytree(KR_DIR, BACKUP_DIR)
        print(f"  현행 kr/ 백업 → {BACKUP_DIR}")

    # 기존 커밋 해제 (soft reset) — kr/ 파일만 제거
    # README.md, LICENSE, pipeline/ 등은 유지
    # 현재 커밋을 orphan branch로 재시작
    git_run("checkout", "--orphan", "history-rebuild")

    # kr/ 디렉토리 삭제
    if KR_DIR.exists():
        shutil.rmtree(KR_DIR)
    KR_DIR.mkdir(parents=True, exist_ok=True)

    # unstage all
    git_run("reset", "HEAD")

    # 기본 파일들만 커밋 (README, LICENSE, pipeline)
    git_run("add", "README.md", "LICENSE", "pipeline/")
    first_date = all_tasks[0][1]["date_str"] if all_tasks else "2024-01-01"
    result = git_run(
        "commit", "-m", "init: 금융 감독규정 Git 저장소 초기화",
        env_extra={
            "GIT_AUTHOR_DATE": f"{first_date}T00:00:00+09:00",
            "GIT_COMMITTER_DATE": f"{first_date}T00:00:00+09:00",
        },
    )
    if result.returncode == 0:
        print(f"  초기 커밋 OK")
    else:
        print(f"  초기 커밋: {result.stderr.strip()}")

    print()

    # ━━━ Phase 2: 스크래핑 + 커밋 ━━━
    print("=" * 60)
    print("Phase 2: 본문 스크래핑 + Git 커밋")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        for i, (reg, ver) in enumerate(all_tasks):
            date_str = ver.get("date_str", "unknown")
            name = reg["name"]
            print(
                f"\n[{i + 1}/{len(all_tasks)}] {name} "
                f"({date_str}, {ver['kind']})..."
            )

            try:
                content = scrape_version_content(page, ver["seq"])
                total_scraped += 1

                if "제1조" not in content:
                    print(
                        f"  WARNING: 제1조 없음 ({len(content)} chars), skip"
                    )
                    errors.append((name, date_str, "no 제1조 in content"))
                    continue

                # Markdown 변환
                reg_copy = dict(reg)
                reg_copy["seq"] = ver["seq"]
                md = to_markdown(content, reg_copy)
                articles = parse_articles(content)
                print(f"  스크래핑 OK: {len(content):,} chars, {len(articles)}조")

                # Git 커밋
                committed = git_commit_version(
                    name, md, date_str, ver["kind"], ver.get("detail", ""),
                )
                if committed:
                    total_committed += 1
                    print(f"  커밋 OK")

            except Exception as e:
                print(f"  ERROR: {e}")
                errors.append((name, date_str, str(e)))

        browser.close()

    # ━━━ Phase 3: 현행 버전 별표 파일 복원 ━━━
    print()
    print("=" * 60)
    print("Phase 3: 별표 파일 복원")
    print("=" * 60)

    # 백업에서 별표 파일 복원
    if BACKUP_DIR.exists():
        for reg_dir in BACKUP_DIR.iterdir():
            if not reg_dir.is_dir():
                continue
            target_dir = KR_DIR / reg_dir.name
            target_dir.mkdir(parents=True, exist_ok=True)
            for f in reg_dir.iterdir():
                if f.name != "고시.md":  # 고시.md는 이미 최신 커밋에 있음
                    shutil.copy2(f, target_dir / f.name)
                    print(f"  복원: {reg_dir.name}/{f.name}")

        # 별표 파일 커밋
        git_run("add", "kr/")
        diff_result = git_run("diff", "--cached", "--quiet")
        if diff_result.returncode != 0:
            git_run("commit", "-m", "feat: 별표/서식 파일 추가")
            print("  별표 파일 커밋 OK")

    # main 브랜치 교체
    git_run("branch", "-D", "main")
    git_run("branch", "-m", "main")
    print("  main 브랜치 교체 OK")

    # 백업 정리
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)

    # ━━━ 결과 ━━━
    print()
    print("=" * 60)
    print("결과 요약")
    print("=" * 60)
    print(f"  스크래핑: {total_scraped}건")
    print(f"  커밋: {total_committed}건")
    print(f"  에러: {len(errors)}건")
    if errors:
        for name, date_info, err in errors:
            print(f"    - {name} [{date_info}]: {err}")

    # git log
    print()
    log_result = git_run("log", "--oneline")
    if log_result.returncode == 0:
        lines = log_result.stdout.strip().split("\n")
        print(f"Git log ({len(lines)} commits):")
        for line in lines[:30]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
