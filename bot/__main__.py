"""명령줄 진입점. 사용: python -m bot index [--dry-run]"""
from __future__ import annotations

import argparse
import json
import subprocess

from bot.config import Settings, load_project
from bot.indexing.indexer import index_project, link_report


def _commit(repo: str) -> str:
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return ""


def cmd_index(args: argparse.Namespace) -> None:
    cfg = load_project(args.config)
    repo = args.repo or Settings().repo_dir
    indexes = index_project(repo, cfg)
    for idx in indexes:
        print(f"[{idx.service}] {json.dumps(idx.stats, ensure_ascii=False)}")
    report = link_report(indexes)
    print(f"[프론트→백엔드 API 연결] 호출 {report['called']}건 중 {report['linked']}건 연결")
    for key in report["unlinked"]:
        print(f"  연결 안 됨: {key}")
    if args.dry_run:
        return
    from bot.db import store

    sha = _commit(repo)
    with store.connect() as conn:
        for idx in indexes:
            r = store.sync_service(conn, idx, sha)
            print(
                f"[{r['service']}] 추가 {r['inserted']} / 변경 {r['updated']} / 삭제 {r['deleted']} "
                f"/ 그대로 {r['unchanged']} / 엣지 {r['edges']}"
            )


def cmd_enrich(args: argparse.Namespace) -> None:
    import asyncio

    from bot.db import store
    from bot.indexing import enrich

    with store.connect() as conn:
        r = asyncio.run(enrich.enrich(conn, args.limit))
    print(f"설명 {r['described']}개, 벡터 {r['embedded']}개 생성 / 토큰 {json.dumps(r['usage']['by_model'], ensure_ascii=False)}")


def cmd_docs(args: argparse.Namespace) -> None:
    import asyncio

    from bot.db import store
    from bot.indexing import docs

    cfg = load_project(args.config)
    with store.connect() as conn:
        r = asyncio.run(docs.index_docs(conn, args.repo or Settings().repo_dir, cfg.docs))
    print(f"문서 청크 {r['chunks']}개")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("enrich", help="청크마다 한국어 설명과 벡터를 만든다 (없는 것만)")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_enrich)
    p = sub.add_parser("docs", help="문서를 섹션 단위로 잘라 저장한다")
    p.add_argument("--config", default=None)
    p.add_argument("--repo", default=None)
    p.set_defaults(func=cmd_docs)
    p = sub.add_parser("index", help="저장소 코드를 읽어 청크와 콜 그래프를 만든다")
    p.add_argument("--config", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 통계만 출력")
    p.set_defaults(func=cmd_index)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
