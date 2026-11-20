-- 봇 전용 DB. 원본 코드는 Git에 있고, 여기는 검색용 사본이다.
CREATE EXTENSION IF NOT EXISTS vector;

-- 검색 단위(메서드·함수·컴포넌트·클래스 요약) 하나가 한 행
CREATE TABLE IF NOT EXISTS code_chunk (
    id              BIGSERIAL PRIMARY KEY,
    service         TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,          -- 서비스 안에서 유일한 이름
    name            TEXT NOT NULL,          -- 짧은 이름 (RoomService#findRoom(Long))
    kind            TEXT NOT NULL,          -- method | constructor | class | function | component
    file_path       TEXT NOT NULL,
    start_line      INT  NOT NULL,
    end_line        INT  NOT NULL,
    signature       TEXT NOT NULL,
    content         TEXT NOT NULL,          -- 코드 원문 (AI에게 읽혀 주는 용도)
    hash            TEXT NOT NULL,          -- content 해시 (배포 때 바뀐 것만 갱신)
    description     TEXT,                   -- Haiku가 쓴 한국어 한 줄 설명
    embedding       VECTOR(1536),           -- 설명 + 시그니처로 만든 벡터
    search          TSVECTOR GENERATED ALWAYS AS (
                        to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(signature, '') || ' ' || coalesce(description, ''))
                    ) STORED,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service, qualified_name)
);
CREATE INDEX IF NOT EXISTS code_chunk_file_idx ON code_chunk (service, file_path);
CREATE INDEX IF NOT EXISTS code_chunk_name_idx ON code_chunk (name);
CREATE INDEX IF NOT EXISTS code_chunk_search_idx ON code_chunk USING GIN (search);

-- 콜 그래프. 한 행이 엣지 하나 (caller가 callee를 호출한다)
--   call   메서드 → 메서드
--   render 컴포넌트 → 컴포넌트
--   route  "GET /api/v1/rooms/{}" → 그 경로를 처리하는 메서드
--   http   프론트 함수 → "GET /api/v1/rooms/{}"
CREATE TABLE IF NOT EXISTS call_edge (
    service    TEXT NOT NULL,
    caller     TEXT NOT NULL,
    callee     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    file_path  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (service, caller, callee, kind)
);
CREATE INDEX IF NOT EXISTS call_edge_callee_idx ON call_edge (callee);
CREATE INDEX IF NOT EXISTS call_edge_file_idx ON call_edge (service, file_path);

-- 마지막으로 인덱싱한 커밋
CREATE TABLE IF NOT EXISTS index_state (
    service     TEXT PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 처리 기록. 해결된 것만 검색 대상
CREATE TABLE IF NOT EXISTS incident (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    service     TEXT,
    api         TEXT,
    symptom     TEXT NOT NULL,
    cause       TEXT,
    cause_type  TEXT,                       -- code | config | infra | external
    resolution  TEXT,
    methods     TEXT[] NOT NULL DEFAULT '{}',
    pr_url      TEXT,
    commit_sha  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    slack_channel TEXT,
    slack_ts    TEXT,
    embedding   VECTOR(1536)                -- 증상 + 원인
);
CREATE INDEX IF NOT EXISTS incident_created_idx ON incident (created_at DESC);

-- 작업 한 건의 상태 (Slack 스레드 하나 = 작업 하나)
CREATE TABLE IF NOT EXISTS job (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT NOT NULL,            -- report | deploy_failure
    status        TEXT NOT NULL DEFAULT 'received',
    slack_channel TEXT NOT NULL,
    slack_ts      TEXT NOT NULL,            -- 스레드의 첫 메시지
    state         JSONB NOT NULL DEFAULT '{}',
    UNIQUE (slack_channel, slack_ts)
);

-- 문서 청크 (README, 포팅 매뉴얼). ## / ### 단위로 자르고, 제목 경로를 heading 에 담는다
CREATE TABLE IF NOT EXISTS doc_chunk (
    id         BIGSERIAL PRIMARY KEY,
    path       TEXT NOT NULL,
    heading    TEXT NOT NULL,
    content    TEXT NOT NULL,
    hash       TEXT NOT NULL,
    embedding  VECTOR(1536),
    search     TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', heading || ' ' || content)) STORED,
    UNIQUE (path, heading)
);
CREATE INDEX IF NOT EXISTS doc_chunk_search_idx ON doc_chunk USING GIN (search);
