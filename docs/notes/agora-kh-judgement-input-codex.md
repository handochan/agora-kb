# 입력: Codex 독립 답변 — agora-kh 재시작 질문 (2026-09-03)

> **상태: 입력 자료 · Non-normative.** [`agora-kh-design-judgement.md`](agora-kh-design-judgement.md)의 §0·§14가 취합한 세 의견 중 하나.
> 생성: `codex exec --sandbox read-only` (gpt-5.6-sol, reasoning ultra), 저장소 `5da5d03` 위에서 같은 질문·같은 읽기 목록으로 독립 실행. 본문은 무편집이며 절대 경로 링크만 상대 경로로 바꿨다.

## 1. 판정: 하이브리드

**기존 `agora-kb`를 폐기하거나 포크하지 않고, 단일-KB custody kernel은 유지하면서 그 위에 멀티-KB `hub` 제어 계층을 추가하는 하이브리드가 적절합니다.**

여기서 하이브리드는 “두 엔진을 병행한다”는 뜻이 아니라 다음과 같습니다.

- `agora-kb`: 하나의 KB를 안전하게 보관·큐레이션·검색하는 data plane
- 신규 `hub` 계층: KB 등록, 권한, 라우팅, 원격 부착, 검색 federation을 담당하는 control plane
- `agora-kh`: 필요하다면 향후 제품명이나 서버 배포 번들명으로 사용하되, 지금 새 저장소·새 SSOT를 만들지는 않음

근거는 세 가지입니다.

1. 가장 어려운 자산은 이미 구현되어 있습니다. append-only inbox, writer별 namespace, repo별 단일 curator, git publication/rollback, redaction, provenance, derived index와 gold pack은 다른 프로젝트들의 기능을 안전하게 수용할 수 있는 custody kernel입니다. 이를 재작성하면 기존 검증을 모두 다시 해야 합니다. [DESIGN의 CQRS·single-writer](../../docs/DESIGN.md#L56)

2. 현재 `Repo`, `AgoraHandlers`, Web/MCP app가 프로세스당 한 repo를 잡는 구조라서, 이를 깨뜨리기보다 상위 router가 여러 독립 RepoEngine을 조정하는 편이 자연스럽습니다. [MCP의 현재 per-repo handler](../../src/agora_kb/faces/mcp_server.py#L92), [Web의 현재 per-repo app](../../src/agora_kb/faces/web/app.py#L409)

3. OpenKB·OpenViking·Graphify를 한 런타임에 합치는 것은 기존 B′와 라이선스 계층을 위반합니다. **B′—“아티팩트 위에서 합성하고 외부 런타임 위에서는 합성하지 않는다”는 판정은 유지**해야 합니다. [STRATEGY의 B′](../../docs/STRATEGY-2026-08.md#L505), [T0–T4 정의](../../docs/adr/0005-fully-oss-bom.md#L56)

뒤집어야 할 기존 판정은 하나입니다.

- `STRATEGY-2026-08`의 Phase 4 auth·multi-tenancy 삭제 권고는 “1인·개인 제품” 전제를 사용했습니다. 이번 요구는 개인 다중 KB, 다중 사용자 팀 KB, 공유 서버를 제품 핵심으로 명시하므로 그 권고를 뒤집어야 합니다. [기존 삭제 권고](../../docs/STRATEGY-2026-08.md#L546)
- 다만 이를 OpenFGA·OAuth·분산 멀티마스터까지 확대하지는 않습니다. Phase 4는 repo 단위 auth, KB registry, 단일-host repo-owner까지로 제한합니다.

`repo=tenant`도 폐기하지 않고 용어를 정밀화해야 합니다.

- 사용자 또는 조직 하나가 여러 KB repo를 소유할 수 있습니다.
- repo는 “계정”이 아니라 **보안·audience·custody 경계**입니다.
- 같은 KB에는 여러 writer가 각자의 inbox로 기여할 수 있지만 curated wiki writer는 하나입니다.
- 공유 범위가 다르면 별도 repo로 분리하고 읽을 때 합성합니다. [기존 read-time composition](../../docs/DESIGN.md#L395)

---

## 2. 목표 아키텍처: 계층·단위·경계

```text
CLI — local process principal ─────────────┐
                                           │
Web / MCP / HTTP — authentication ─────────┼── HubService
                                           │     ├─ KB Registry / profiles
                                           │     ├─ authorization + routing
                                           │     ├─ federation / jobs
                                           │     └─ AuthorizedRepoHandle
                                           │              │
                                           │       ┌──────┴──────┐
                                           │       ▼             ▼
                                           │   RepoEngine A   RepoEngine B
                                           │
RepoEngine: capture → inbox → one curator → wiki/raw/git
                                      └────→ derived index/graph/gold

외부 도구: snapshot/artifact → validator/redaction → inbox 또는 read adapter
```

### 2.1 단위

하나의 KnowledgeRepo가 다음의 최소 단위입니다.

- 하나의 보안·공유·custody 경계
- 하나의 git history
- 하나의 curation home과 동시에 활성화되는 curator 하나
- writer별 append-only inbox
- repo별 독립 index, graph, gold, DuckDB/vector 파생물
- 결과 식별자: `kb_id + revision + path + anchor/unit`

`kb_id`는 basename이나 사용자 입력 path가 아니라 hub registry가 발급한 opaque identifier여야 합니다. fork·rename·mirror 의미는 공개 계약 배포 전에 ADR로 고정해야 합니다.

### 2.2 단일 KB 구조

Stratum의 “디렉터리가 종류다” 원칙은 유력하지만 아직 비규범 초안입니다. [Stratum 상태](../../docs/notes/stratum-target-architecture.md#L3)

승인 시의 목표는 다음과 같습니다.

- `wiki/{concepts,summaries,notes,maps,entities}/`
- 주제는 경로 대신 복수 `subjects:` frontmatter
- `raw/_blob/<sha>`: 원본 evidence
- `raw/_pages/<sha>/<compiler-version>/…`: clone/offline에서도 사용할 수 있는 장문 projection
- `_kb/{index,graph,gold,staging,…}`: 재구축 가능한 파생·운영 상태
- graph는 canonical graph DB가 아니라 Markdown에서 재구축되는 read model

`people/`은 현재 초안 그대로 비준하면 안 됩니다. “curator만 `wiki/`를 쓴다”와 “curator는 `people/`을 쓰지 않는다”가 충돌합니다. 사람 작성물도 inbox/review를 거치게 하거나, curator-owned `wiki/` 밖의 명시적 human-source namespace로 분리해야 합니다.

### 2.3 실행 모드

| 모드 | 실행 경로 | 추가 기능 |
|---|---|---|
| 개인 no-serve | CLI가 local registry를 읽고 같은 프로세스에서 HubService와 RepoEngine 호출 | 동기 capture/compile/query; daemon·Forgejo·네트워크 불필요 |
| 개인 serve | 동일 registry와 RepoEngine 위에 watcher·scheduler·Web·MCP 제공 | 비동기 장문 compile, 자동 harvest, Web 진행상태 |
| 멀티테넌트 server | 인증 후 `kb_id`를 AuthorizedRepoHandle로 변환하여 repo별 worker에 위임 | 팀 ACL, 원격 접근, 다중 KB Web, 감사·운영 기능 |

no-serve와 serve는 서로 다른 제품이 아니라 같은 library contract의 두 실행 형태여야 합니다.

### 2.4 Hub registry와 보안 경계

Registry는 repo 밖의 사용자/서버 설정이며 지식 SSOT가 아닙니다. 최소 필드는 다음입니다.

- `kb_id`, display alias
- local path, pinned mirror 또는 remote locator
- `owned`, `attached-readonly`
- personal/team 종류와 owner security realm
- 허용 capability: `read`, `inbox-write`, `curate-admin`
- schema/revision, trust, refresh/freshness policy

서버 요청은 반드시 다음 순서를 지킵니다.

1. principal 인증
2. opaque `kb_id`에 대한 권한 판정
3. 권한이 부여된 handle 생성
4. 그 뒤에만 filesystem path 해석
5. RepoEngine에 위임

caller가 넘긴 path를 먼저 열고 나중에 검사해서는 안 됩니다. 이는 Proposed ADR-0036의 핵심 경계입니다. [ADR-0036 core-boundary enforcement](../../docs/adr/0036-authn-authz.md#L161)

### 2.5 원격 KB 부착과 공유

기본은 읽기 전용입니다.

- `snapshot attach`: git commit에 고정한 fetch-only clone. 개인 no-serve와 offline에 적합
- `live attach`: 향후 인증된 Agora-to-Agora read protocol. 첫 90일에는 제외
- snapshot 갱신은 새 clone 검증 후 atomic pointer 교체
- attached KB에서는 local curator를 실행하지 않음
- 타 KB 지식을 내 KB에 채택할 때는 `promote` 이벤트로 목적지 inbox에 넣고 원본 `kb_id/revision/path/hash`를 보존
- 쓰기 기여는 remote owner service의 inbox로 전송하며 wiki끼리 merge하지 않음

현재 lock은 host-local이므로 writable clone의 active-active나 양방향 sync는 금지해야 합니다.

### 2.6 검색 federation

검색은 다음 단계로 나눕니다.

1. 명시적 scope 또는 named KB collection 선택
2. 권한으로 scope 축소
3. repo별 deterministic lexical/structural query 실행
4. 각 repo의 top-k를 `kb_id/revision/local-rank`와 함께 반환
5. 기본 UX는 KB별 그룹과 명시적 scope priority
6. 선택적으로 citation-bound LLM synthesis 수행

KB별 BM25 점수는 서로 다른 corpus의 IDF를 사용하므로 raw score를 한 줄로 정렬하면 안 됩니다. 첫 버전은 전역 순위를 가장하지 말고 그룹 결과를 기본으로 하며, cross-KB fusion은 ADR-0030의 별도 benchmark를 통과해야 합니다.

검색 경로도 분리해야 합니다.

- curator merge-target 결정: deterministic lexical oracle만 사용
- 사용자 읽기: lexical baseline에 dense/KG/LLM을 strictly additive tail로 사용 가능
- DuckDB·vector·KG: repo별 shard로만 구성하며 canonical 저장소나 권한 경계로 사용하지 않음
- cross-KB gold pack: source repo에 쓰지 않고 hub cache/메모리에서 합성하며 각 section의 provenance를 유지

---

## 3. 요구사항별 충족 방식

| 요구사항 | 충족 방식 |
|---|---|
| R1 개인·no-serve compile + serve 상위기능 | CLI가 registry와 RepoEngine을 직접 호출. serve는 같은 경로 위에 watcher, scheduler, Web, remote MCP, 비동기 장문 작업만 추가 |
| R2 개인 다중 KB + KB-aware 검색 | 사용자 profile에 여러 `kb_id`와 named scope를 등록. 단일 KB·선택 집합·authorized-all 검색을 구분하고 모든 hit에 KB identity와 revision 표시 |
| R3 타인 KB 부착 | commit-pinned read-only snapshot이 기본. 자동 local curation 금지. 채택은 provenance를 가진 destination-inbox promotion |
| R4 서버 멀티테넌트 다중 작성자 | 사용자·조직은 여러 repo를 소유하고 KB별 membership 부여. writer마다 inbox namespace, repo마다 curator 하나. 초기 서버는 단일 host·단일 owner actor |
| R5 OKF+Obsidian 호환 | canonical corpus는 Markdown/frontmatter/wikilink. strict OKF export와 tolerant import를 제공. Obsidian은 읽기·편집 face이되 curator-owned wiki 직접 쓰기는 금지. OpenKB와 같은 디렉터리에 동거하지 않음 |
| R6 KG·LLM 친화 단일 KB 구조 | Stratum의 kind directories, `subjects`, summaries, stable entity IDs, typed links를 채택 후보로 삼음. graph/embedding은 Markdown에서 재구축되는 projection |
| R7 장문 | 원본 blob → deterministic unit/page tree → 계층 summary → bounded retrieval. compiler는 pure transform이고 curator만 publish. 인용은 `kb_id/revision/blob_sha/unit/offset`까지 해석 가능해야 함 |
| R8 살아있는 KB | session→redaction→inbox; insight와 ontology는 자동 사실 변경이 아니라 proposal 생성; skill은 `_kb/staging`/diff/승인 후 export; 중요 지식은 gold pack과 명시적 agent link로 pull |
| R9 Web UX | KB switcher, readable scope와 writable target의 분리, read-only badge, freshness/revision/trust, grouped search, partial-failure 표시, provenance/diff, 장문 tree, graph와 proposal review, SSE job progress |
| R10 Claude Code·Codex·aelix·Copilot | 이름별 core 분기가 아니라 `AgentProfile` capability contract 사용: session reader, CLI brain argv, capture transport, context-link/export target, hooks. 네 profile에 동일 conformance suite 적용. 현재 aelix 누락·Claude reader 기본값 문제부터 제거해야 함. [현재 전략의 agent gap](../../docs/STRATEGY-2026-08.md#L391) |
| R11 API보다 CLI | CLI workflow를 규범적 UX로 두고 Web/MCP/HTTP는 동일 HubService의 thin face로 구현. agent 인증과 모델 실행은 각 CLI가 소유하고 Agora는 argv·stdout/artifact contract만 소유 |
| R12 외부 장점 흡수 | 아래와 같이 T0–T4를 지키며 artifact/idea/capability만 흡수 |

R12의 구체적 경계는 다음입니다.

- **OpenKB — T0 artifact:** compile/watch, 계층 요약, multi-KB alias, Workbench의 업로드 진행·도구 호출 timeline 같은 UX를 참고합니다. OpenKB가 만든 Markdown/JSON을 검증된 document feed로 읽거나 `agora export --format okf`를 제공합니다. 같은 `wiki/`에서 양쪽 compiler를 실행하지 않습니다. [OpenKB 공식 저장소](https://github.com/VectifyAI/OpenKB), [Workbench·multi-KB REST 예제](https://github.com/VectifyAI/OpenKB/blob/main/examples/rest-api/README.md)

- **OpenViking — T4:** URI namespace, L0/L1/L2 progressive loading, observable retrieval trajectory, session→memory 패턴을 설계 아이디어로 사용합니다. 현재 main project는 AGPL이므로 import/vendor/query-time 의존 없이 별도 실행 결과 artifact만 수입합니다. [OpenViking 공식 저장소](https://github.com/volcengine/OpenViking)

- **Graphify — T0 artifact:** explained edge, confidence, deterministic graph export, scoped graph query/merge, agent installation profile을 참고합니다. `graph.json`은 schema/version validator를 통과한 경우에만 derived input으로 받고 엔진은 흡수하지 않습니다. [Graphify 공식 저장소](https://github.com/Graphify-Labs/graphify)

- **Obsidian + DuckDB:** Obsidian은 canonical Markdown을 보는 개인 UX입니다. DuckDB FTS/vector/graph extension은 라이선스 전이 폐포를 다시 검사한 뒤 repo별 optional read model로만 사용합니다. 특히 VSS는 공식적으로 experimental이고 삭제·메모리 운용 제약이 있으므로 SSOT나 보안 경계가 될 수 없습니다. [DuckDB extension 문서](https://duckdb.org/docs/stable/core_extensions/overview), [DuckDB VSS](https://github.com/duckdb/duckdb-vss)

새 프로젝트를 추가할 때도 “좋아 보이는 기능”이 아니라 다음 네 관문을 모두 통과시켜야 합니다.

1. T0–T4와 전이 라이선스
2. versioned input/output contract
3. provenance·redaction·tenant isolation
4. 제거해도 single-repo/no-serve가 정상인 optionality와 benchmark

---

## 4. 뒤집거나 추가해야 할 ADR/불변식

| 대상 | 판정 |
|---|---|
| ADR-0005 | T0–T4 모델 유지. 실제 BOM·전이 라이선스 CI를 추가하고 도입 시점마다 재검증 |
| ADR-0006 | 유지하되 “repo=사용자”가 아니라 “repo=KB security/audience/custody boundary”로 개정. domain ACL은 보안이 아닌 편의 필터 |
| ADR-0002·불변식 2 | single-writer 유지. Stratum의 `people/` 직접쓰기 공백을 해소하고 human contribution도 inbox/review를 거치게 함 |
| ADR-0010/0011/0012/0022/0014 | Stratum 비준 시 layout, ingest path, query corpus, domain→subjects, OKF export-only 경계를 함께 amend/supersede |
| ADR-0026 | skill 기능보다 먼저 작성. proposal/staging/diff/승인만 허용하고 agent skill directory 자동 쓰기 금지 |
| ADR-0028 | DISTILL을 insight·ontology proposal act까지 확장하되 canonical 자동 반영 금지 |
| ADR-0029 | agent profile, session format, consent, connector capability 계약 포함 |
| ADR-0030 | registry, stable KB identity, scope, snapshot attach, promotion airlock, partial failure, cross-KB result composition까지 확대 |
| ADR-0031 | 멀티사용자 write 전 retention, 삭제, derived cache purge, inbox backup 범위의 최소 판정 필요 |
| ADR-0032/0035 | DuckDB/vector/hybrid는 search harness 증거가 나온 뒤에만 활성화 |
| ADR-0036 | OD-1=A Forgejo, OD-2=A server-side repo authz, OD-3=A exclusive auth mode를 비준. OD-4는 Web control 전까지 local/proxy-only, control 도입 시 curator-admin으로 전환. [미비준 ODs](../../docs/adr/0036-authn-authz.md#L294) |
| 신규 ADR-0037 후보 | Hub topology: registry와 AuthorizedRepoHandle, repo-owner lifecycle, local/server 동일 contract |
| 신규 ADR-0038 후보 | Stratum/schema-v2와 장문 blob/unit/citation/compiler-version 계약 |
| 신규 ADR-0039 후보 | query lanes와 federation: write oracle, read synthesis, score 비호환, fusion-version 계약 |

불변식 1–6은 폐기하지 않고 다음처럼 정밀화합니다. [현재 불변식](../../CLAUDE.md#L21)

- 불변식 1: “curated knowledge의 SSOT는 Markdown+git”으로 명시하고 원본 binary evidence와 git-tracked portable projection을 구분
- 불변식 2: 모든 canonical publish는 curator만 수행; human·compiler·hub도 예외 없음
- 불변식 5: RepoEngine은 한 repo만 열 수 있음. Hub는 권한이 확인된 opaque handle만 조정할 수 있음
- 불변식 6: 네 agent를 first-class로 시험하되 core에서 이름 기반 특권을 만들지 않음

다음 두 불변식을 추가하는 것이 좋습니다.

7. **Audience 또는 custody가 다르면 다른 repo다.** Cross-KB 작업은 read composition이나 provenance를 가진 inbox event뿐이다.  
8. **Federation은 identity를 지우지 않는다.** 결과·graph·gold·cache는 `kb_id/revision`을 유지하고 revoke/delete 시 repo 단위로 제거 가능해야 한다.

---

## 5. 이행 순서: 첫 90일과 무복귀선

| 기간 | 목표와 완료 조건 |
|---|---|
| 1–14일 | GitHub Project·ROADMAP·현재 코드의 기준선 재조정; ADR-0030/0036 결정; single-repo golden fixture 고정; #144/#146/#147/#152 정직성 문제 선행; 라이선스 BOM; Stratum integrity/search gate 설계 |
| 15–30일 | repo 외부 local registry, stable `kb_id`, `kb add/list/use/detach`, `query --scope`, `compile --kb`; 기존 `--repo`와 stdio는 완전 호환 |
| 31–45일 | 두 local KB와 하나의 pinned read-only snapshot federation; KB별 grouped results, revision provenance, partial failure; raw-score global merge 없음 |
| 46–60일 | ADR-0036 기반 auth facade; `Principal → AuthorizedRepoHandle`; Forgejo repo permission; 두 사용자·두 팀·개인 KB deny-first matrix; 한 host에서 repo별 owner actor |
| 61–75일 | Web KB switcher·scope·write target·read-only badge·provenance; PDF 한 종류의 장문 vertical slice; local 동기와 serve 비동기가 같은 compiler contract 사용 |
| 76–90일 | Claude/Codex/aelix/Copilot profile conformance; session→inbox→gold 한 경로; skill·ontology는 dry-run proposal만; tenant leakage, revocation, symlink, duplicate alias, rollback, retrieval benchmark 후 private pilot |

90일에 하지 않을 것은 명확히 해야 합니다.

- OpenFGA/OAuth
- cross-host active-active curator
- writable remote attach와 양방향 sync
- 외부 프로젝트 query-time runtime fusion
- 전역 vector/graph index
- Stratum 전체 migration
- skill·ontology 자동 적용

### 무복귀선

진짜 데이터 무복귀선은 `wiki/<domain>/…`을 평탄화해 Stratum으로 전환하는 순간입니다. 다음 조건 전에는 넘지 않습니다.

- 모든 기존 note의 domain/subjects 물질화
- reversible migrator와 역변환 검증
- integrity boundary v2
- n=24 이상 검색 harness
- clone/rollback/OKF export 검증
- 최소 한 compatibility release

Stratum 초안도 domain flattening을 유일한 실제 무복귀선으로 봅니다. [Stratum migration gate](../../docs/notes/stratum-target-architecture.md#L77)

운영상 추가로 넘지 말아야 할 선은 다음입니다.

- ADR-0036·protected branch·retention/audit 전에 첫 network write를 받지 않음
- fencing/lease 검증 전에 두 번째 writable owner를 허용하지 않음
- public `kb_id`, fork, scope, citation 계약 전에는 `agora-kh`를 별도 incompatible project로 발표하지 않음

---

## 6. 가장 큰 리스크 3개와 반증 조건

| 리스크 | 가정과 반증 조건 | 반증되면 |
|---|---|---|
| 1. Federation이 tenant wall을 깨뜨림 | 모든 read/write/query/graph/gold/upload/curate 조합에서 unauthorized 접근 0건이어야 함. 거부 요청이 filesystem 수준에서도 path를 열지 않아야 하고, token revoke가 TTL 안에 적용되어야 함. symlink·중복 basename·forged `kb_id`·cache에서도 한 건이라도 누출되면 설계가 반증됨 | network beta 중단. gateway와 repo worker를 별도 process/OS identity로 격리하고 cache를 repo별로 분리 |
| 2. Hub가 얇은 control plane이 아니라 새 monolith가 됨 | 기존 `--repo` CLI와 stdio가 daemon·DB·Forgejo 없이 동작하고 single-repo golden 결과가 동일해야 함. 45일까지 2 local + 1 snapshot을 이 조건으로 연결할 수 없거나 faces가 HubService를 우회하면 “thin hub” 가정이 반증됨 | 신규 기능 동결. package/process 경계를 분리하되 custody core는 재작성하지 않음 |
| 3. 다중 검색·자기진화가 KB의 정직성을 낮춤 | federation의 answer-bearing Recall@10이 “정답 KB를 미리 선택한 oracle”보다 5%p 이상 낮아지거나, hard-negation/not-found 회귀가 생기거나, citation이 정확한 revision/unit으로 100% 해석되지 않으면 전역 검색 가정이 반증됨. 50건 이상의 blind skill/ontology proposal에서 승인율이 50% 미만이면 living automation의 유용성도 반증됨 | KB별 grouped search를 기본으로 유지하고 LLM/KG는 표시된 실험 tier로 강등. proposal은 staging에만 남기고 자동 canonical write는 계속 금지 |

결론적으로 Agora가 지향할 것은 “모든 지식 도구를 한 프로세스에 넣은 최고 기능 집합”이 아닙니다. **여러 도구와 에이전트가 만든 지식을 안전하게 받아, KB별 custody와 history를 보존하고, 여러 KB 위에서 출처가 남는 합성을 제공하는 knowledge hub**여야 합니다.

읽기 전용으로 검토했으며 코드와 문서는 수정하지 않았습니다.
