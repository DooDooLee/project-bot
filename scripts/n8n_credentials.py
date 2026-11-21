"""n8n에 넣을 자격 정보 파일을 만든다. 봇 서버에서 .env 를 읽어 표준 출력으로 JSON을 내보낸다.

n8n 워크플로가 쓰는 자격 정보는 두 개다.
  slackBot01      Slack에 글을 올릴 때 쓰는 Bot Token
  internalHook01  '봇의 결과' 문으로 들어온 요청이 봇이 보낸 것인지 확인하는 값 (x-webhook-secret)

사용 (봇 서버에서):
  python3 scripts/n8n_credentials.py .env | docker compose exec -T n8n sh -c 'cat > /tmp/c.json && n8n import:credentials --input=/tmp/c.json; rm -f /tmp/c.json'

값은 n8n이 자기 암호화 키(N8N_ENCRYPTION_KEY)로 암호화해 보관한다. 이 스크립트는 값을 파일로 남기지 않는다.
"""
import json
import sys


def read_env(path: str) -> dict:
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def build(env: dict) -> list:
    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "WEBHOOK_SECRET") if not env.get(k)]
    if missing:
        raise SystemExit(f".env 에 값이 없음: {', '.join(missing)}")
    return [
        {"id": "slackBot01", "name": "Slack 봇", "type": "slackApi",
         "data": {"accessToken": env["SLACK_BOT_TOKEN"], "signatureSecret": env["SLACK_SIGNING_SECRET"]}},
        {"id": "internalHook01", "name": "봇 내부 요청 확인", "type": "httpHeaderAuth",
         "data": {"name": "x-webhook-secret", "value": env["WEBHOOK_SECRET"]}},
    ]


if __name__ == "__main__":
    print(json.dumps(build(read_env(sys.argv[1] if len(sys.argv) > 1 else ".env"))))
