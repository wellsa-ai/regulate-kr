#!/bin/bash
# regulate-kr 일일 업데이트 체크 (매일 06:00 KST)
# cron: 0 6 * * * /Users/sammy/workspaces/regulate-kr/pipeline/cron_update.sh

set -e

cd /Users/sammy/workspaces/regulate-kr
LOG="/Users/sammy/workspaces/regulate-kr/logs/update.log"
mkdir -p "$(dirname "$LOG")"

echo "=== $(date) ===" >> "$LOG"

# 업데이트 체크 + 자동 커밋
python3 pipeline/check_updates.py >> "$LOG" 2>&1

# 새 커밋이 있으면 push
AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
if [ "$AHEAD" -gt 0 ]; then
    git push origin main >> "$LOG" 2>&1
    echo "pushed $AHEAD commits" >> "$LOG"

    # 미니한테 보고
    ~/bin/mini-ask -t "[regulate-kr] 감독규정 $AHEAD건 업데이트 감지 → GitHub 푸시 완료" >> /dev/null 2>&1 || true
else
    echo "no updates" >> "$LOG"
fi
