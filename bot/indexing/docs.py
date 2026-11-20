"""문서(Markdown)를 섹션 단위로 잘라 저장한다. 비밀값은 저장하기 전에 가린다."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from bot import embedding

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
# 값이 비밀일 가능성이 큰 줄: password, secret, token, key 뒤의 값을 가린다
_SECRET_LINE = re.compile(r"(?i)^(\s*[-\w.\"']*(?:password|passwd|secret|token|api[-_]?key|private[-_]?key|client[-_]?id)[\w.\"']*\s*[:=]\s*)(\S.*)$")
_HOOK_URL = re.compile(r"https?://[^\s\"'`)]*/hooks/[^\s\"'`)]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_KEY_LIKE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|xox[abp]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")


def mask_secrets(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = _SECRET_LINE.match(line)
        if m and not m.group(2).strip().startswith(("[", "${", "<")):
            line = m.group(1) + "***"
        line = _HOOK_URL.sub("https://***/hooks/***", line)
        line = _KEY_LIKE.sub("***", line)
        line = _EMAIL.sub("***@***", line)
        out.append(line)
    return "\n".join(out)


def split_markdown(text: str, path: str, max_chars: int = 3500) -> list[dict]:
    """## / ### 단위로 자른다. 각 조각에는 '문서 > 2. 구성 > 2.1 포트' 같은 제목 경로를 붙인다."""
    chunks: list[dict] = []
    trail: list[str] = []
    buf: list[str] = []
    in_code = False

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            heading = " > ".join(trail) or Path(path).name
            for i in range(0, len(body), max_chars):
                part = body[i : i + max_chars]
                label = heading if i == 0 else f"{heading} (계속 {i // max_chars + 1})"
                chunks.append({"path": path, "heading": label, "content": part, "hash": hashlib.sha256(part.encode()).hexdigest()})
        buf.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            del trail[level - 1 :]
            trail.append(m.group(2).strip())
        else:
            buf.append(line)
    flush()
    return chunks


async def index_docs(conn, repo_root: str | Path, paths: list[str]) -> dict:
    total = 0
    for rel in paths:
        file = Path(repo_root) / rel
        if not file.exists():
            continue
        chunks = split_markdown(mask_secrets(file.read_text(encoding="utf-8", errors="replace")), rel)
        existing = {r["heading"]: r["hash"] for r in conn.execute("SELECT heading, hash FROM doc_chunk WHERE path = %s", (rel,))}
        changed = [c for c in chunks if existing.get(c["heading"]) != c["hash"]]
        vectors = await embedding.embed_many([f"{c['heading']}\n{c['content']}" for c in changed]) if changed else None
        for i, c in enumerate(changed):
            vec = None if vectors is None else "[" + ",".join(f"{x:.6f}" for x in vectors[i]) + "]"
            conn.execute(
                """
                INSERT INTO doc_chunk (path, heading, content, hash, embedding) VALUES (%s,%s,%s,%s,%s::vector)
                ON CONFLICT (path, heading) DO UPDATE SET content = EXCLUDED.content, hash = EXCLUDED.hash, embedding = EXCLUDED.embedding
                """,
                (c["path"], c["heading"], c["content"], c["hash"], vec),
            )
        gone = [h for h in existing if h not in {c["heading"] for c in chunks}]
        if gone:
            conn.execute("DELETE FROM doc_chunk WHERE path = %s AND heading = ANY(%s)", (rel, gone))
        total += len(chunks)
    conn.commit()
    return {"chunks": total}
