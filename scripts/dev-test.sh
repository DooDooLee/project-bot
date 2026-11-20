#!/usr/bin/env bash
# PC의 코드를 봇 서버로 보내고 Docker 안에서 테스트를 돌린다. 사용: ./scripts_dev_test.sh [pytest 인자]
set -e
cd "$(dirname "$0")/.."
tar czf - --exclude=.git --exclude=__pycache__ --exclude=.env . | ssh pb-bot 'mkdir -p ~/dev/project-bot && tar xzf - -C ~/dev/project-bot'
ssh pb-bot "cd ~/dev/project-bot && docker run --rm --network host -v ~/dev/project-bot:/app -v ~/dev/sudaDUCK:/data/repo:ro --env-file ~/dev/bot-test.env project-bot-dev python -m pytest -q $*"
