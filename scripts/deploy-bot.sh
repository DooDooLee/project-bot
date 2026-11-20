#!/usr/bin/env bash
# PC의 코드를 봇 서버에 올리고 다시 띄운다. 사용: ./scripts/deploy-bot.sh
set -e
cd "$(dirname "$0")/.."
tar czf - --exclude=.git --exclude=__pycache__ --exclude=.env --exclude=.pytest_cache . | ssh pb-bot 'tar xzf - -C ~/stack && tar xzf - -C ~/dev/project-bot 2>/dev/null || true'
tar czf - --exclude=.git --exclude=__pycache__ --exclude=.env --exclude=.pytest_cache . | ssh pb-bot 'tar xzf - -C ~/dev/project-bot'
ssh pb-bot 'cd ~/stack && docker compose exec -T postgres psql -U bot -d bot -q -f - < db/schema.sql && docker compose build -q && docker compose up -d 2>&1 | grep -v "Running" | tail -5; sleep 6; docker compose ps --format "{{.Name}} {{.Status}}" | grep -E "api|worker"'
