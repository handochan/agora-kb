# 입력: 그린필드 스틸맨 — 백지 설계 `agora-kh`와 재시작 케이스 (2026-09-03)

> **상태: 입력 자료 · Non-normative.** [`agora-kh-design-judgement.md`](agora-kh-design-judgement.md)가 취합한 세 의견 중 하나.
> 생성: 워크플로의 그린필드 설계안 에이전트가 placeholder만 반환해, 같은 전제로 별도 opus 에이전트가 재실행한 결과. 본문 무편집.

# ARCHITECT — 백지 설계 `agora-kh` 와 재시작 스틸맨

> 방법: R1–R12만 보고 백지에서 설계 → 그다음에야 `agora-kb`를 열어 대조. 코드 주장은 전부 이 세션에서 직접 확인했고 `file:line`으로 표기했다. 실측: `src/` **27,575줄**, `tests/` **37,113줄 / `def test_` 1,753개**, Accepted ADR 27건.

---

## 1. 백지 설계 — `agora-kh`

### 1.1 첫 결정: 단위는 "저장소"가 아니라 "워크스페이스"다

R2·R3·R4가 전부 "여러 KB"를 전제한다. 그러므로 시스템의 1급 객체는 **KB 하나**가 아니라 **KB들의 레지스트리**여야 하고, KB는 **경로가 아니라 불변 ID**로 식별돼야 한다.

```yaml
# <kb-root>/.agora/kb.yaml   — init 시 1회 각인, 이후 불변
id: 01JQ8F3M7XA2K9V0            # ULID. 디렉터리명·remote URL·별칭과 독립
name: general
kind: personal | team
schema: 1
```

```yaml
# ~/.agora/registry.yaml  — 저장소 바깥. 유일한 사용자-레벨 상태
version: 1
kbs:
  - {id: 01JQ8F…, alias: general,  path: ~/knowledge,          role: owner}
  - {id: 01JR2A…, alias: rust,     path: ~/kb/rust,            role: owner}
  - {id: 01JS9C…, alias: team-eng, remote: https://kb.co/eng,
     transport: http | git-fetch, role: reader,  cache: ~/.agora/cache/01JS9C…}
defaults:
  write: general
  read_scope: [general, rust, team-eng]
```

`kb_id`가 모든 것에 붙는다 — 검색 히트, inbox 아이템, gold 팩, 그래프 노드, 그리고 **노트 프론트매터 자체**(`kb:`). 노트가 복사돼 나가도 자기 출신을 안다. 이게 R2의 "per-result KB provenance"·R3의 신뢰 경계·R4의 테넌트 경계를 전부 하나의 원시 타입으로 떠받친다.

### 1.2 KB 하나의 온디스크 포맷 — 디렉터리가 종류다

```text
<kb-root>/
  .agora/{kb.yaml, agents.yaml, adapters.yaml, policy.yaml}
  index.md · log.md · AGENTS.md
  wiki/                       ← 정본. 첫 세그먼트가 種類(kind)
    concepts/[<free>/]<slug>.md
    summaries/<doc-slug>.md          장문 한 편의 항해 노트
    notes/<yyyy>/<mm>/<slug>.md      날짜 캡처 · 무손실 바닥
    maps/<slug>.md
    entities/<slug>.md
    people/<principal>/**.md         읽기 1급 · 큐레이터 쓰기 금지
  docs/<doc_id>/{manifest.yaml, pages/<n>.md}    ← 장문 티어(R7), 정본
  assets/**
  raw/
    _blob/<ab>/<sha256>.<ext>        원본 바이트 · 불변 · 콘텐츠 주소
    _blob/<ab>/<sha256>.meta.yaml
  _kb/                        ← 파생 · gitignore · 재구축만
    inbox/<principal>/<id>.md
    processing/ processed/ failed/ cursors/
    index.duckdb
    gold/<pack>.md
    proposals/{insights,skills,ontology}/
```

프론트매터 (정본 노트):

```yaml
kb: 01JQ8F…            # 자기-식별
id: <ulid>
kind: concept|summary|note|map|entity   # 디렉터리의 미러(디렉터리가 권위)
title:  aliases: []  tags: []
subjects: []           # ← 옛 domain. 경로를 떠나 데이터가 됨. 초기값 [] = 아무것도 단언 안 함
summary:  description:                  # description = OKF용 summary 미러
status: draft|active|contested          confidence: low|medium|high
sources: ["raw/_blob/<sha>", "docs/<doc_id>#p12"]
related: []  children: []
provenance:
  writers: [alice@corp]   # 인증된 principal. 신뢰됨
  agents:  [codex]        # 에이전트의 자기신고. 기록하되 신뢰 안 함
  origin:  harvest:codex | web:alice | mcp:alice
okf_version: '0.2'   timestamp:   updated:
```

**핵심 분리 둘.** (a) `subjects:`가 데이터가 되면서 "경로 이동 = 도메인 변경"이라는 무복귀선이 애초에 생기지 않는다. (b) `writers`(인증됨) 와 `agents`(자기신고)를 **다른 필드**로 나눈다 — R4의 custody는 이 구분 없이는 거짓말이다.

### 1.3 쓰기 경로 — CQRS는 백지에서도 그대로 나온다

R4("한 KB에 여러 사람이 넣고 컴파일")를 만족하면서 git 히스토리를 유지하는 방법은 사실상 하나다: **N 라이터가 per-principal append-only inbox에 넣고, 1 큐레이터가 wiki를 편집한다.** 백지 설계가 여기서 기존 스파인과 완전히 수렴한다. 다만 세 곳이 다르다.

1. **네임스페이스는 인증된 principal이다** — `_kb/inbox/<principal>/`. 얼굴(face)이 아니라.
2. **`source`는 채널에서 파생되지 호출자가 주지 않는다.**
3. **락은 추상화되고 소유자를 기록한다** — `LockBackend{posix-flock, windows-msvcrt, lease-file}`, 리스 레코드 `{host, pid, boot_id, expires_at}`. 이게 그대로 크로스-호스트 fencing이 된다.

역할은 넷: `owner > editor(compile 가능) > contributor(inbox append만) > reader`.

### 1.4 공유 모델 (R3) — "read-only보다 나은 모델"

오너가 열어둔 질문에 대한 답: **`contributor` 역할**. 원격 KB의 `_kb/inbox/<my-principal>/`에만 append할 수 있고 `wiki/`는 못 건드린다. 이건 "쓰기 권한을 준다"가 아니다 — 원격의 큐레이터가 여전히 유일한 라이터이므로 **읽기전용과 거의 같은 안전성으로 기여를 받는다.** CQRS를 채택한 순간 공짜로 떨어지는 배당이고, PR 워크플로 없이 작동한다.

부착: `kh kb attach https://kb.co/eng --as team-eng --role reader`. 트랜스포트 둘 — `git-fetch`(fetch-only clone, 오프라인 가능, gold 팩은 curated commit의 순수 함수라 로컬 재조립) 또는 `http`(라이브 읽기 프록시). 기본은 fetch-only.

### 1.5 검색 · 그래프 — 파생 인덱스는 DuckDB, 점수는 순수 파이썬

R2가 요구하는 건 **N개 코퍼스에 대한 한 프로세스 안의 페더레이션**이다. 조사 결과 MIT `markdown` DuckDB 커뮤니티 익스텐션이 `read_markdown_sections()` / `md_extract_wikilinks()` / `md_extract_tags()`를 이미 준다 — 즉 Obsidian-aware 마크다운→SQL 프론트엔드가 완제품으로 존재하고, `ATTACH`로 KB N개를 한 세션에 붙일 수 있다. 라이선스는 DuckDB core MIT + 익스텐션 MIT로 T1 깨끗하다.

경계선은 명확히 긋는다: **DuckDB는 후보 집합과 term statistics만 계산하고, 점수는 순수 파이썬이 후보 행 위에서 계산한다.** 그래야 "가속기는 절대 score를 계산하지 않는다"는 결정론 계약이 유지되면서 페더레이션 성능을 산다.

랭킹 (§13 실측을 그대로 따름 — `llm_then_bm25` r@1 0.583 vs bm25 단독 0.208, 부정 정답률 1.000 유지):
- KB별로 결정론 후보 top-k 생성 → 합집합에 대해 LLM이 `ok`/`not_found`와 순서를 소유 → BM25F가 그 아래를 backfill.
- **크로스-코퍼스 원점수는 절대 비교하지 않는다** (IDF·avgdl이 코퍼스별이라 비교 불가). 오프라인 폴백은 KB 내 순위 → `(registry 우선순위, 순위, path)` 고정 타이브레이크 인터리브.
- 모든 히트는 `kb`(ULID) + `alias`를 싣는다.

그래프는 `wiki/` 링크 + `related`/`children`에서 파생, DuckDB 뷰로 노출. 덤으로 Obsidian `.base` 파일을 방출하면 Obsidian이 테이블/보드 UI를 공짜로 그려준다 (R9의 두 번째 UI, 코드 0줄).

### 1.6 장문 (R7)

```
kh docs add paper.pdf --kb general
```
→ 원본 바이트를 `raw/_blob/<sha256>.pdf`에 불변 저장 → 추출기가 `docs/<doc_id>/pages/<n>.md` + `manifest.yaml`(페이지 트리·헤딩·바이트 범위) 생성 → 큐레이터가 `wiki/summaries/<slug>.md` 항해 노트를 저작하고 그 `sources:`가 `docs/<doc_id>#p12`를 가리킨다. 검색은 페이지 단위 앵커를 돌려준다. 서버 없이 CLI로 전부 된다 (R1).

무결성 경계는 **처음부터 바이트 우선**으로 설계된다: 커밋 승인은 `(path, sha256)` 매니페스트 대조이지 텍스트 동일성 비교가 아니다. 슬러거는 유니코드를 보존하고(Graphify 슬러그 계약 차용 — 비-ASCII 보존 · 링크≡디스크 파일명 · Windows MAX_PATH 예산 · 충돌 접미사 예약), **탈출 방지는 charset 정규식이 아니라 `resolve() + is_relative_to()` 봉쇄로 따로 건다.**

### 1.7 R8 — 살아있는 KB와 안전 게이트

파이프라인: `extract → gate → compile → derive → **propose** → **promote** → inject`

- **compile**: 닫힌 op 어휘 (오늘의 6개와 사실상 동일). 모델이 정본 위키에 자유롭게 못 쓴다.
- **propose**: 모델 합성물(인사이트·스킬 제안·온톨로지 개념)은 `_kb/proposals/`에 착지한다. **파생 티어이고 검색 코퍼스 밖이다.** 이게 자기-섭취 루프(모델 산출물을 소싱된 지식과 같은 층에 넣기)에 대한 구조적 방어다.
- **promote**: `kh propose promote <id>` — 사람 또는 명시적 규칙만. 승격 시 `promoted_from:` provenance가 박힌다.
- **inject**: `kh link claude-code` — 에이전트 메모리 파일의 센티널 펜스 구간에 refresh-only 쓰기. 표준 동의 필요, 조용한 쓰기 없음.
- **루프 차단은 세는 것**부터: 인바운드 사실의 content hash가 이전에 내보낸 gold span과 일치하면 **드롭하고 카운터를 올린다**. 재서술 루프는 못 막지만, 관측 가능해진다.

### 1.8 에이전트 통합 (R10/R11) — enum이 아니라 레지스트리

```yaml
# .agora/agents.yaml
agents:
  claude-code:
    session: {format: claude-jsonl, path: ~/.claude/projects/**/*.jsonl}
    memory:  {file: CLAUDE.md, fence: sentinel}
    driver:  {argv: [claude, -p], mode: text-only}
  codex:
    session: {format: codex-rollout, path: ~/.codex/sessions/**/*.jsonl}
    memory:  {file: AGENTS.md}
    driver:  {argv: [codex, exec, --skip-git-repo-check, --sandbox, read-only]}
  copilot:
    memory:  {file: .github/copilot-instructions.md}
  aelix:
    driver:  {argv: [aelix, --print, --mode, json]}
    plane:   execution        # 실행 플레인. 지식은 저장하지 않음
```

엔진은 **선언된 capability에만 반응하고 이름에는 절대 반응하지 않는다.** `source`는 등록된 agent id면 뭐든 받는다. R10(aelix·Copilot 1급)이 코드가 아니라 데이터로 풀린다. R11대로 드라이버는 전부 argv 기반 헤드리스 CLI이고, 에이전트는 순수 텍스트 생성기로 쓰인다(스크래치 cwd, 파일 툴 미사용).

### 1.9 no-serve vs serve (R1) · 웹 (R9)

| | 서버 없이 (CLI) | `kh serve` / `kh web` 추가분 |
|---|---|---|
| 쓰기 | `kh remember`, `kh docs add`, `kh harvest` | 네트워크 MCP · 웹 업로드 · 다중 사용자 |
| 컴파일 | `kh compile` (1회 실행) | `kh watch` 상시 큐레이터 · 크론 · 인사이트 잡 |
| 읽기 | **`kh query` / `kh read` / `kh context`** | `/search` UI · `/graph` · 대시보드 |
| 페더레이션 | `--scope a,b,c` (로컬 + 캐시된 원격) | 라이브 원격 프록시 |

**`kh query`가 1일차부터 존재한다.** 이게 백지 설계에서 가장 사소해 보이지만 R1의 절반을 결정한다.

웹: 레지스트리 위의 **한 프로세스**, 상단 KB 스위처, `read_scope` 전역 검색(결과마다 KB 칩), 모든 페이지의 "inject" 입력창(선택된 KB의 inbox로), 인사이트 패널 = 제안 플레인 큐(promote/reject 버튼).

### 1.10 R12 라이선스 배치

| | 티어 | 하는 일 |
|---|---|---|
| DuckDB + `markdown` ext | T1 (MIT) | 파생 인덱스 엔진 · 페더레이션 |
| Obsidian `.base` 방출 | T0 (포맷) | 공짜 두 번째 UI |
| OKF 0.2 | T0 (스펙) | `kh export --format okf` |
| Graphify 슬러그 계약 | **T0 (계약, 코드 아님)** | 유니코드 슬러거 규칙 |
| OpenKB | T0 (문서 피드 only, **동거 금지**) | `kh export`로만 |
| OpenViking | **T4 영구** | 링크 금지. PyPI SDK는 라이선스 선언 자체가 없음 |
| PageIndex | T2 (벤더링) | 필요시 장문 트리만 |

---

## 2. 백지 설계 × 기존 스파인 — 수렴/발산 표

| 축 | `agora-kb` 현재 (증거) | 백지 `agora-kh` | 판정 |
|---|---|---|---|
| 쓰기 모델 | append-only inbox → 단일 라이터 큐레이터 | 동일 | **완전 수렴** |
| SSOT | markdown + git | 동일 | **완전 수렴** |
| 파생 티어 | `_kb/` 재구축 가능 | 동일 | **완전 수렴** |
| 결정론 점수 계약 | ADR-0012, 가속기는 score 계산 금지 | 동일 (DuckDB는 후보만) | **수렴** |
| 정직한 `not_found` | 있음 | 동일 | **수렴** |
| 경계 레닥션 + 아웃바운드 센티널 | `core/redact.py` · `core/sentinel.py` | 동일 | **수렴** |
| 샌드박스 큐레이터 + FINAL-DIFF | 있음 | 동일 | **수렴** |
| 브레인 = 헤드리스 CLI 텍스트 생성기 | `adapters/cli_agent_brain.py:55-70` | 동일 | **수렴** |
| **KB 식별자** | `self.repo = layout.root.name` (`core/wiki.py:657`) — 디렉터리명 | 불변 ULID `.agora/kb.yaml` | **발산** |
| **다중 KB** | 없음. `--repo default="."` × 19개 파서, `build_app(repo_path=)` (`faces/web/app.py:409`) | `~/.agora/registry.yaml` + `--scope` | **발산** |
| **레이아웃 축** | 경로=도메인 / 닫힌 4값 `type:` (`schema/lint.py:85`), 경로 하드와이어 `wiki/<domain>/themes/…` (`curator/plan.py:201-209`) | 경로=종류 / `subjects:`=데이터 | **발산** |
| **도메인 저장** | 프론트매터에 `domain:` 키 없음. `lint.py:203-208`과 `mcp_server.py:942-952`가 경로에서 **각각 독립 파생** | 프론트매터 필드 | **발산 (무복귀선)** |
| **파생 인덱스 엔진** | 전체 파일 재작성 JSON (`core/index_cache.py:81-107`) | DuckDB (ATTACH로 페더레이션) | **발산** |
| **락** | `import fcntl` 무조건 모듈 최상단 (`curator/claim.py:31`), 소유자 기록 없음 | `LockBackend` + 리스 레코드 | **발산** |
| **에이전트 명부** | `FIXED_SOURCES` frozenset 7개 (`core/models.py:28-30`) — `source="copilot"` 은 오늘 검증 오류. `session:` 리더는 Claude Code 하드코드 | `agents.yaml` 레지스트리 | **발산** |
| **신원 귀속** | `kb_remember`의 `source`가 호출자 인자 (`faces/mcp_server.py:1027-1039`); 웹은 전원이 `_kb/inbox/web/` 공유 | principal 인증 + agent 자기신고 분리 | **발산** |
| **장문** | `ExtractedDoc` = 평평한 마크다운 1덩어리, 청킹 0 | `docs/<doc_id>/pages/` + `summaries/` | **발산 (양쪽 다 신규)** |
| **R8 생성 절반** | op 6개 닫힘, 인사이트/스킬/온톨로지 op 없음 | `_kb/proposals/` 제안 플레인 + promote 게이트 | **발산 (양쪽 다 신규)** |
| **CLI 읽기 동사** | **없음.** 19개 서브커맨드 중 `Wiki.query` 호출자 0 | `kh query` 1일차 | 발산 (≈50줄) |

**요약: 스파인은 완전히 수렴하고, 둘레가 발산한다.** 그리고 스파인이 코드가 집중된 곳이다 — `curator/` 8,439 + `core/` 4,876 = src의 **48%**, `tests/curator/` 11,734줄 = 테스트의 **32%**.

---

## 3. 재시작이 사는 것 (진짜 이득만)

| # | 이득 | 진화 경로로는 왜 못 얻는가 |
|---|---|---|
| **G1** | **레이아웃 축 뒤집기를 마이그레이션 없이.** `subjects:`가 처음부터 데이터라 무복귀선이 생기지 않는다 | 진화는 897줄의 레이아웃 어휘 편집 + 관문 A + 관문 B + `domain:` 물질화(#156)를 먼저 사야 한다. 그리고 `raw/` 경로 문자열이 **모든 노트의 `sources:`에 저장돼 있고** lint L1-7/L1-8이 그걸 검증하므로 재경로화는 전 노트의 provenance 사슬 재작성이다 |
| **G2** | **무결성 경계가 바이트 우선으로 태어난다.** 바이너리 blob이 1일차부터 정본에 들어간다 | `curator/worker.py:1591` `_is_engine_written_raw`가 utf-8 `read_text` 완전 일치로만 승인한다. 이건 #135가 planting 공격에 대해 굳힌 함수라, 재작성은 TAMPER/DELETE/COVERED-DELETE 매트릭스를 다시 만족시켜야 한다 |
| **G3** | **유니코드 슬러그와 경로 봉쇄가 분리된 채 태어난다** | `curator/plan.py:92` `_SAFE_TOKEN_RE_PATTERN = \A[A-Za-z0-9][A-Za-z0-9._-]*\Z` 은 포맷 규칙이 아니라 **탈출 방지 보안 통제**다(`plan.py:88-91` 주석이 그렇게 명시). 살아있는 코퍼스 위에서 이걸 건드리려면 봉쇄 속성 보존 증명이 필요하다 |
| **G4** | **결정 부채 0에서 시작.** ADR 27건이 만드는 "생각을 바꾸는 비용"이 사라진다 | Stratum 초안 자체의 ADR 트리아지가 **KEEP 11 · AMEND 12 · SUPERSEDE 5** 다. 구조 변경 1건당 ADR ~17건이 움직인다. 설계가 아직 움직이는 중이라면(§14는 2026-09-02 기록) 이 오버헤드는 복리로 붙는다 |
| **G5** | **표류 리셋.** 문서와 코드의 벌어진 틈이 한 번에 닫힌다 | 오늘 실측되는 표류: `agora link`가 DESIGN §10에 있고 코드에 없음 · 도메인 파생 2중 구현 · `repo.yaml`의 `kind:`를 파서 둘이 따로 읽음 · `RepoConfig` docstring이 "not yet consumed"라고 거짓 진술 · CLI에 읽기 동사 없음. 진화는 이걸 항목별로 갚아야 한다 |

**그리고 사지 *못하는* 것 — 스틸맨의 가장 큰 구멍.** 재시작의 최대 구조적 이점은 보통 "하위 호환을 안 지켜도 되는 자유"인데, **`agora-kb`는 이미 그걸 갖고 있다.** 릴리스된 버전이 없고, 외부 사용자가 없고, 호환 약속이 없다. Stratum 노트 §6이 그 자유가 이미 2.4 인월을 아꼈다고 명시적으로 계산해뒀다. 재시작이 팔려는 물건을 피고인이 이미 소유하고 있다.

---

## 4. 재시작이 다시 벌어야 하는 것

### 4.1 포팅 가능성

| 자산 | 규모 | 이식성 | 근거 |
|---|---|---|---|
| `adapters/` (ollama_brain 1,437 · cli_agent_brain 189) | 1,627 | **거의 그대로** | 레이아웃 무관. 순수 텍스트 생성 파이프라인 |
| `core/redact.py` · `sentinel.py` | 464 | **그대로** | 경계 계약. 4자 중 아고라만 가진 것 |
| `core/gold.py` | 836 | 그대로에 가까움 | curated commit의 순수 함수 |
| `curator/` 샌드박스·`subprocess_backend`·`backends` | ~1,000 | **그대로** | OS 격리는 스키마와 무관 |
| `ingest/extractors/` + `vault_import.py` | 2,178 | **그대로** | 순수 변환 |
| `harvester/` | 1,834 | 리더 레지스트리만 추가 | `SessionReader` Protocol이 이미 있음 (`session_sources.py:10-11`); `build_connectors`가 리더를 안 넘길 뿐 |
| `core/wiki.py` 랭커 | 1,495 | ~70% (kb_id + 종류-디렉터리 + DuckDB 심) | `_FIELDS`/BM25F 수학은 그대로 |
| `curator/worker.py` · `apply.py` · `plan.py` | 4,058 | **중편집** | 경로 하드와이어(`plan.py:201-209`) + 바이트 우선 무결성 경계 |
| `schema/lint.py` · `notes.py` · `emit.py` | 1,459 | **~50% 재작성** | `_NOTE_TYPES` 4값 + 도메인 파생이 여기 산다 |
| `cli.py` · `config.py` | 4,145 | **재작성** | 명령 표면 자체가 바뀜 (레지스트리·scope) |
| `faces/` | 2,850 | 중편집 | 단일 repo 가정이 생성자에 박힘 |
| **테스트 37,113줄 / 1,753개** | — | **행동 수준 이식 가능, 픽스처 수준 중편집** | 픽스처가 `wiki/<domain>/themes/` 를 인코딩 |
| **ADR 27건** | — | 문헌으로 이식 가능, **권위는 소멸** | 새 저장소에서 재비준 필요 |
| **도그푸드 KB** (`~/knowledge-agora-dogfood`) | — | **export/import로만** | 오너의 실사용 메모리. 이게 멈추면 도구가 아니라 습관이 죽는다 |

### 4.2 이중 유지보수 기간

`agora-kh`가 R1+R5+R6 패리티(컴파일 + 질의 + lint + 스키마)에 도달할 때까지 `agora-kb`는 **계속 돌아야 한다** — 오너의 실제 기억이 거기 들어 있다. 현실적으로는 오너가 `agora-kb` 유지보수를 포기하고 얼어붙은 도구로 산다. 재시작이 착륙하면 무해하고, **정체되면 치명적이다** (기억이 얼어붙은 채로 남는다). 이 기간을 **4–6개월**로 본다.

### 4.3 인월 추정 (솔로 메인테이너, 에이전트 보조)

기준선: 이 저장소는 Phase 1(2026-06-20 실증) → Phase 3.6(2026-07-25)까지 **약 3–4개월**에 27.5k src + 37k 테스트 + 27 ADR을 쌓았다. 즉 이 오너의 실측 처리량은 매우 높고, 재구축은 발견이 이미 끝났으므로 최초 구축보다 빠르다. 이 점은 재시작에 **유리한** 사실이라 정직하게 계상한다.

| 단위 | 재시작 | 진화 |
|---|---|---|
| 단일-KB 패리티 (컴파일·질의·lint·스키마) | 2–3 | 0 (있음) |
| 큐레이터 + 샌드박스 + 브레인 (주로 포팅) | 1.5–2 | 0 |
| 레이아웃 축 (Stratum) | 0 (설계에 내장) | **8** (§6 정정치, 관문 A·B 포함) |
| 장문 티어 R7 | 1.5–2 | 1.5–2 |
| 레지스트리 + 페더레이션 + 웹/MCP over N (R2/R3/R9) | 2–3 | 2–3 |
| 멀티테넌트 신원 + fencing (R4) | 2–3 | 2–3 |
| R8 제안 플레인 + link 주입 | 1.5–2 | 1.5–2 |
| 테스트 ~1,700개 재획득 | 2–3 | 0 |
| **합계** | **13–18 인월** | **15–18 인월** |

**결론이 여기 있다: 두 경로가 노이즈 범위 안에서 같다.** 그리고 동률일 때는 부가 자산이 결정한다 — 살아있는 도그푸드, 27건의 이미 결정된 질문, 이중 유지보수 부재. 셋 다 진화 편이다.

---

## 5. R13 판정

# **EVOLVE** — 단, §6의 클린-브레이크 변형과 함께

스틸맨을 성실히 시도한 결과, 재시작의 **가장 강한 논거 셋이 측정하면 전부 반대편으로 넘어간다**:

1. "호환을 깰 자유를 산다" → **이미 갖고 있다.** 릴리스 전이다.
2. "단일-repo 가정이 뼈에 박혀 있다" → 실측하면 19개 파서의 **기본 인자 하나**와 생성자 kwarg 둘, 그리고 리졸버 한 함수(`core/repo.py:128-130`)다. `Workspace`를 얹는 건 가산적이다. 진짜 어려운 부분(크로스-코퍼스 점수 비교 불가)은 **양쪽 경로에서 값이 같은 설계 문제**다.
3. "레이아웃이 8 인월이다" → 그 숫자는 이미 15에서 8로 **자기 정정된** 값이고, 정정의 절반(6.5)이 측정 오류였다. 897줄이라는 실측이 그 아래 있다.

남는 진짜 이득은 G4(결정 부채)와 G5(표류) 뿐인데, 둘 다 **프로세스 문제이지 코드 문제가 아니다.** 저장소를 새로 파면 증상은 리셋되고 프로세스는 따라온다. 그리고 ADR 27건은 부채인 동시에 **자산**이다 — §14의 4자 판정이 살아남은 유일한 이유가 그걸 정본 위치에 적어두는 습관이었고, 재시작은 그 습관의 산출물을 버리는 것으로 시작한다.

### 판정을 뒤집을 관측 가능한 조건 셋

1. **관문 A(#154)가 예산을 초과한다.** 무결성 경계 v2(바이트 우선 캡처 + 유니코드 슬러거)가 #135 TAMPER/DELETE/COVERED-DELETE 매트릭스 + #136 파생 충돌 코퍼스를 **솔로 3주 안에** 다시 만족시키지 못하면, 결합은 897줄보다 깊고 Stratum 8인월은 하한이 아니라 낙관치다. → 그러면 위 표의 "진화 15–18"이 "25+"가 되고 판정이 뒤집힌다. **이게 단일 최대 결정 변수다.**
2. **페더레이션 ADR 초안의 supersede 목록에 ADR-0012 §0a가 들어간다.** 크로스-KB 랭킹이 `SearchHit`/`QueryResult` 의미론 자체의 변경을 요구한다면, ADR 5건 + `tests/core/` 4,848줄이 한꺼번에 움직인다 — 검색 헌법이 무너지면 보존할 자산의 핵심이 사라진다.
3. **R4 신원 귀속이 쓰기 경로의 온디스크 문법을 바꾼다.** per-principal 네임스페이스 + attested `source`가 `_kb/inbox/<writer>/<id>.md` 경로 문법이나 `InboxItem` 필수 필드의 *모양*을 바꿔야 한다면(가산이 아니라), 불변식 3의 디스크 계약이 깨지고 도그푸드 코퍼스의 provenance 사슬이 무효화된다. **보존할 코퍼스가 없어지면 진화의 마지막 우위가 사라진다.**

세 조건 모두 **6주 안에 관측 가능**하다. 관문 A는 이미 그 목적으로 발행돼 있다.

---

## 6. 하이브리드 변형 — "v0.2 Stratum 클린 브레이크"

새 패키지도 새 저장소도 아니고, **마이그레이터를 안 쓰는 것**이 변형의 핵심이다.

- **유지**: 저장소, git 히스토리, ADR 27건, `src/agora_kb/`, 테스트 1,753개, 커밋 규율.
- **깨기**: 온디스크 포맷을 v0.2 Stratum으로 **클린 브레이크**. 인플레이스 마이그레이터를 **쓰지 않는다.** 이행 경로는 `agora export --format okf` → `agora repo init --schema 2` → `agora import` 단 하나.
- **선택적 배포명**: PyPI 배포명과 CLI 엔트리포인트만 `agora-kh` / `kh`로 바꿀 수 있다(패키지 경로는 그대로). 포지셔닝(custody + knowledge hub)을 이름에 싣고 싶을 때만.
- **순서 고정**: 레이아웃 독립 정직성 계약 넷(#144 쓰기경로 랭커 심 · #146 thin-page · #147 불변식 6 · #152) → 관문 A(#154) → 관문 B(#155) → 클린 브레이크. `#156`(`domain:` 물질화)은 **불필요해진다** — 무복귀선을 닫는 대신 건너뛴다.

**비용**: Stratum 8인월 중 마이그레이션·호환 몫(대략 2–3인월)이 사라지고, 대신 도그푸드 KB를 export/import로 한 번 재구축한다(하루~이틀, 다만 `raw/`가 오너의 산문과 바이트 동일이라 **재큐레이션이 아니라 재임포트**여야 한다 — 재생성은 오너의 글을 패러프레이즈한다). 순 절감 ≈ 2–3인월.

**리스크 둘.** (a) 배포명을 바꾸면 문서·이슈·ADR에 두 이름이 공존하고, 이 감사가 이미 찾아낸 종류의 표류가 하나 더 생긴다 — 바꾼다면 같은 커밋에서 전면 치환해야 한다. (b) 클린 브레이크는 "언제든 되돌린다"를 포기하는 것이므로, **관문 A가 통과한 뒤에만** 선언해야 한다. 관문 A 전에 선언하면 문제가 생겼을 때 원인이 레이아웃인지 무결성 경계인지 가릴 수 없다.
