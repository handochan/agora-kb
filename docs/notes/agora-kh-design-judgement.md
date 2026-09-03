# agora-kh 설계 판정 — 재시작인가, 진화인가 (2026-09-03)

> **상태: Draft · Non-normative · 결정은 오너.** 어떤 ADR도 supersede하지 않는다. 아키텍처 SSOT는 [`docs/DESIGN.md`](../DESIGN.md), 결정 SSOT는 [`docs/adr/`](../adr/)다.
> **근거:** 25-agent 설계 워크플로(5안 · 심사 3인 · 적대 5렌즈 60건 · 2회 개정) + OpenAI Codex 독립 답변 + 백지 그린필드 스틸맨.
> 코드 주장은 이 세션에서 read-only로 재검증했고 `file:line`으로 인용한다. **스냅샷:** `docs/strategy-14-four-way-synthesis` @ `5da5d03`.
> **착지:** `docs/notes/agora-kh-design-judgement.md` (2026-09-03). 입력 2종은 같은 폴더에 있다 — [Codex 독립 답변](agora-kh-judgement-input-codex.md) · [그린필드 스틸맨](agora-kh-judgement-input-greenfield.md). 워크플로 합성본 rev.2와 적대 검증 60건 원본은 저장소 밖(`.omc/plans/`)에 남긴다.

---

## 0. 한 문장 판정 (R13)

> **진화한다. 새 저장소 `agora-kh`를 만들지 않는다. 하이브리드는 저장소 *안의* `hub` 계층 하나와
> 선택적 배포명 하나로 축소한다.**

### 0.1 세 의견 비교

| | **workflow rev.2** | **Codex (gpt-5.6-sol)** | **그린필드 스틸맨** |
|---|---|---|---|
| 판정 | 잠정 EVOLVE (W6 재판정) | **하이브리드** — custody 커널 유지 + `hub` 제어면 추가, 새 저장소 없음 | **EVOLVE** + v0.2 클린 브레이크 |
| 핵심 근거 | 몰수 자산 실측: `src/` **27,575줄** · `tests/` **37,113줄 / 1,753 함수** · Accepted ADR **27건** | 가장 어려운 자산(append-only inbox · 단일 큐레이터 · git 발행/롤백 · 리댁션 · gold)이 이미 있고, 프로세스당 1 repo 구조는 상위 라우터를 얹는 게 자연스럽다 | 재시작이 파는 물건("호환 깰 자유")을 **이미 소유**한다 — 릴리스 전, 외부 사용자 0 |
| 인월 | 미제시 (주 단위 14주 예산) | 미제시 | **재시작 13–18 vs 진화 15–18 = 노이즈 범위 동률** |
| 90일 범위 | 초보수 — R4·R7·R9 전부 90일 밖 | 6구간, 46–60일에 auth facade까지 | 순서만 고정: 정직성 4건 → 관문 A → 관문 B → 클린 브레이크 |
| 뒤집는 기존 판정 | 없음 | **STRATEGY §7 / §14.7 "Phase 4 auth+멀티테넌시 삭제"** 1건 | `#156` 도메인 물질화가 **불필요**해짐 |

### 0.2 합의된 것

셋 다 재시작을 기각하고, 다음이 세 문서에서 독립적으로 같은 값으로 나왔다 — CQRS 단일 라이터 유지 · KB는 경로가 아니라 **불변 ID**로 식별 · 크로스-KB
**원점수 병합 금지**(밴드/그룹) · 부착은 읽기 전용 기본 · 모델 합성물은 **제안 플레인 + 사람 에어락** · 에이전트는 이름이 아니라 **선언된
capability**로. 백지에서 다시 그려도 스파인이 같다는 것이 스틸맨의 결론이고, 그 스파인이 코드가 집중된 곳이다 (`curator/` + `core/` = src의 48%).

### 0.3 갈린 것과 최종 결정

| 쟁점 | 갈림 | **결정** | 이유 |
|---|---|---|---|
| 하이브리드의 정의 | workflow는 기각, Codex는 이것이 곧 판정 | **같은 것을 다르게 불렀다.** 새 저장소 없음 + 저장소 내부 `hub` 패키지 = Codex의 하이브리드 ≡ workflow의 EVOLVE | 신규 표면 전부가 `RepoLayout`/`Wiki`/`Inbox`를 **함수 호출로** 쓴다. 네트워크 경계가 아니므로 저장소를 쪼갤 근거가 없다 |
| R4 시점 | workflow 90일 밖 / Codex 46–60일 | **Codex 채택.** H2(주 8–13)에 ADR-0036 Accept + auth facade | R4가 제품 핵심이 된 이상 "90일 밖"은 요구의 조용한 증발이다 |
| R9 웹 | workflow Track B 전면 연기 / Codex 61–75일 | **최소 `?kb=` 스위처만 H1**, 나머지 밖 | 오너가 웹을 중시하고, URL 공간 유지 + 셀렉터는 기존 웹 테스트의 마이그레이션이 아니라 확장이다 |
| 레이아웃 이행 | workflow 도메인 내부 신규 티어(잠정) / 스틸맨 클린 브레이크 | **스틸맨 채택** (§3.2). 중간에 v1 도메인 레이아웃 아래 새 티어를 만들지 **않는다** | 도메인 내부 티어를 먼저 만들면 Stratum 비준 시 **두 번 마이그레이션**한다 |
| DuckDB | 스틸맨 코어 파생 엔진(T1) / workflow 미탑재 / Codex repo별 옵션 | **옵션 `[analytics]` extra** (§10, D7) | `docs/BOM.md`(#157)가 아직 없다. 전이 폐포 검사 전에 코어 의존은 ADR-0005 절차 위반 |
| `people/` | Codex만 불변식 2 충돌 제기 | **충돌 아님** — Stratum 노트가 이미 "읽기 1급 · 큐레이터 쓰기 금지"로 분리했다(`stratum-target-architecture.md:44-46`, §3-2) | 불변식 2는 **큐레이션된 위키**를 지배한다. 사람 소유 트리는 큐레이터가 쓰지 않으므로 단일 라이터가 깨지지 않는다. ADR이 이 문장을 명시해야 한다 |

## 1. 오너 요구 × 현재 상태 × 목표안

상태 어휘: `shipped` · `partial` · `drafted`(문서만) · `reserved`(ADR 번호만) · `absent`(코드 0).

| 요구 | 현재 | 근거 | 목표안 기전 | 언제 | ADR |
|---|---|---|---|---|---|
| **R1a** CLI compile (서버 없이) | `partial` | `agora repo init/import/curate`는 있으나 **읽기 동사가 없다** — `cli.py`의 `add_parser` 19개 중 `Wiki.query` 호출자 0 | `agora query` / `read` / `neighbors` 추가(≈250–350 src) + 데몬 0개 e2e | **H0** | 0002·0003 |
| **R1b-1** serve: 상시 큐레이터 | `shipped` | `agora watch` (`cli.py:324`) | 변경 없음 | — | 0011 |
| **R1b-2** serve: 웹 | `shipped`(단일 KB) | `faces/web/app.py`, `build_app(repo_path=)` | `?kb=` 스위처 | **H1** | 0019 |
| **R1b-3** serve: 네트워크 MCP | `absent` (의도적) | `ROADMAP.md:370` — Streamable HTTP는 **auth 항목에 결합, 먼저 출하 금지** | ADR-0036 뒤 | **90일 밖** | 0036 |
| **R1b-4** serve: 인사이트 잡 | `absent` | 제안 플레인 없음 | `_kb/proposals/` 스켈레톤 | **H2**(스켈레톤만) | 0028 |
| **R2** 다중 KB + KB-aware 검색 | `absent` | `--repo` 기본 `"."` × 19 파서, `build_app(repo_path=)`; `kb_query(question: str)`에 scope 인자 없음(`faces/mcp_server.py:1042`) | 레지스트리 + `FederatedHit` + 밴드 합성; CLI·웹·MCP가 같은 profile 소비 | **H1**(CLI·웹) · MCP `scope=`는 H1 말 | **0037** |
| **R3** 타인 KB 부착 | `absent` | 원격 반입 경로 0. `core/repo.py:27-28` "Everything here is LOCAL" | S1 = 커밋 고정 fetch-only 미러 + `MIRROR` 마커 + curate/watch/upload 거부 | **H1** | **0037** |
| **R4** 서버 멀티테넌트 | `reserved` | `src/agora_kb/auth/__init__.py` = **docstring 2줄**. ADR-0036 Status: Proposed | ADR-0036 Accept → `Principal → AuthorizedRepoHandle` → S2 contributor 인박스 | **H2** | 0036 → 0030 |
| **R5** OKF + Obsidian | `partial` | produce는 표준 마크다운 링크(ADR-0014 D3); consume은 손실 — `vault_import.py:522-535` 위키링크 재작성, `:584-593` 해소 불가 `related:` 드롭 | 생산자 적합성 검증기 + 손실 발산표. **바이트 라운드트립은 기각** | **H0**(문서) · H2(검증기) | 0014 |
| **R6** KG·LLM 친화 단일 구조 | `drafted` | `stratum-target-architecture.md` = "Draft · NOT ratified". 축이 뒤집혀 있음(경로=주제, 4값 `type:`=종류) | Stratum kind-first + `subjects:` 프론트매터 | 목표 선언 **H2 말**, 이행은 90일 밖 | #153 소유 |
| **R7** 장문 | `drafted` | 장문 노트도 "NOT ratified"; `adr/README.md` 예약 목록에 장문 항목 **없음** | `docs/<doc_id>/pages/` + `wiki/summaries/` 항해 노트 | **ADR-0040 초안만** H2 | **0040** |
| **R8** 살아있는 KB | `partial` | 하베스터·세션 커넥터·후보 게이트·리댁션·gold는 shipped. 승격/온톨로지/스킬 op **없음**(`GATE_ALLOWED_OPS` 3개) | `_kb/proposals/` 제안 플레인 + `agora insight promote` 에어락 + `agora link` 동의 | **H2**(스켈레톤·동의) | **0026·0028** |
| **R9** 웹 UX | `partial` | 단일 KB 브라우즈·검색·업로드·그래프·대시보드 shipped | `?kb=` 스위처 + 결과마다 KB 칩 | **H1**(최소) | 0019·0025 |
| **R10** 1급 에이전트 4종 | `partial` **2/4** | `core/models.py:28-30` `FIXED_SOURCES = {claude-code, codex, qwen, gemini, opencode, hermes, manual}` — **aelix·copilot 둘 다 거부됨** | capability set 선언(§8) + `doctor --agents` 프로브 | **H0**(#147) | **0029** |
| **R11** CLI-over-API · aelix 실행 플레인 | `shipped`(기전) | ADR-0016 `agora-cli-brain` 심이 이미 R11의 기전. 지배 판정 `STRATEGY-2026-08.md` §11 | 신규 API 표면 **0개**. 페더레이션은 파일·git | — | 0016 |
| **R12** 4자+Obsidian/DuckDB 흡수 | `partial` | T0–T4는 ADR-0005에 있으나 `docs/BOM.md` **미작성**(#157) | §10의 차용 지도. DuckDB는 옵션 extra | **H0**(BOM 첫 행) | 0005 애드덤 |

## 2. 목표 아키텍처 — hub over cores

```text
  CLI (로컬 프로세스 주체) ─────────┐
  MCP / Web / HTTP (인증된 주체) ──┼──►  hub  (제어면 · 쓰기 없음)
                                   │      ├─ 레지스트리 · 프로파일 · 부착
                                   │      ├─ 인가 판정 → AuthorizedRepoHandle
                                   │      └─ 페더레이션(밴드)
                                   │            ┌──────┴──────┐
                                   │            ▼             ▼
                                   │        RepoEngine A   RepoEngine B   (KB당 1개)

  RepoEngine:  capture → _kb/inbox/<writer>/ → 큐레이터 1인 → wiki/ · raw/ · git
                                             └─────────────→ _kb/{index,graph,gold}  (파생)
```

### 2.1 누가 무엇을 읽고 쓰는가

| 프로세스 | 읽는 것 | **쓰는 것** | KB 수 | 라이터 정체성 |
|---|---|---|---|---|
| `agora curate` · `watch` | `_kb/inbox/**`, `wiki/**` | `wiki/**`, `_kb/**`, git 커밋 | 1 | 큐레이터 (유일 라이터) |
| `agora serve` (MCP) | `wiki/**`, `_kb/gold/**` | `_kb/inbox/<writer>/` + `$AGORA_HOME/emit/**` | 1 → H1 이후 읽기 N | `claude-code`/`codex`/… |
| `agora web` | `wiki/**` | `_kb/inbox/web:<user>/` | 1 → H1 이후 읽기 N | `web:<user>` (#67, `app.py:28-31`) |
| `agora harvest` | 커넥터 소스 | `_kb/inbox/harvest-<agent>/` | 1 | `harvest:<agent>` |
| `agora link <agent>` | `_kb/gold/**` | **KB 밖** 에이전트 메모리 파일 + `$AGORA_HOME/emit/**` | 1 | 사람 (명시 동의) |
| **`hub`** (신규) | 등록 KB N개의 `wiki/**` | **아무것도 쓰지 않는다** | N | — |

### 2.2 연속성 — 같은 포맷, 같은 명령

개인 no-serve → serve → 멀티테넌트는 **다른 제품이 아니라 같은 라이브러리 계약의 세 실행 형태**다. KB 포맷도 명령도 셋에서 동일하고, 달라지는 것은
주체 해석 하나뿐이다 — CLI는 로컬 프로세스 주체, serve는 `trusted_header` 또는 토큰, 멀티테넌트는 ADR-0036 principal. `--repo` 단일 KB 경로는 셋
모두에서 **계속 동작해야 한다**(§13 리스크 2의 반증 조건).

### 2.3 패키지 경계 · 이름 `agora-kh`

**`src/agora_kb/hub/`를 택한다**(`src/agora_hub/`가 아니라). 두 번째 최상위 패키지는 배포·`uv.lock`
폐포·`#107` 수동 smoke 게이트를 갈라놓는 값을 내지만 사는 것은 import 금지 하나뿐이고, 그건 pytest로 같은 값에 산다. `hub/`는
`core/`·`curator/`·`faces/`와 같은 층에 놓여 기존 레이아웃 규약과 정합한다.
**강제 규칙(테스트):** `agora_kb.hub`는 `agora_kb.curator.*`를 import하지 않는다. `hub`는 읽기
fan-out과 인가만 하고, 쓰기는 언제나 각 KB의 `Inbox.write`를 통과한다.

배포명(PyPI)과 CLI 엔트리포인트만 `agora-kh`/`kh`로 바꾸는 것은 가능하고 저장소·패키지 경로는 그대로다. 채택한다면 **한 커밋에서
문서·이슈·ADR·엔트리포인트를 전면 치환**한다(스틸맨 리스크 a: 두 이름 공존은 이 감사가 이미 찾아낸 부류의 표류를 하나 더 만든다). **권고: H2 종료까지
보류** — 공개 `kb_id`·fork·scope·인용 계약이 서기 전에 별도 프로젝트로 발표하지 않는다(Codex §5).

## 3. KB 하나의 구조

### 3.1 목표 — Stratum kind-first

`wiki/`의 첫 세그먼트가 **종류**를 지고, 주제는 경로를 떠나 `subjects:` 프론트매터가 된다 (`stratum-target-architecture.md` §2·§3). 오늘은 반대다 —
경로가 주제를, 닫힌 4값 `type:` enum이 종류를 진다. 이 역전이 R6("엔티티에 노드 종류가 없다")과 R7("긴 문서에 집이 없다")의 기계적 원인이다.

```text
wiki/concepts/[<free>/]<slug>.md   ·  summaries/<doc-slug>.md   ·  notes/<yyyy>/<mm>/<slug>.md
    maps/<slug>.md  ·  entities/<slug>.md  ·  people/<person>/**.md
docs/<doc_id>/{manifest.yaml, pages/<n>.md}      raw/{<domain>/, _blob/<ab>/<sha256>.<ext>}
_kb/{inbox,index,graph,gold,proposals,cursors}/  ← 파생 · gitignore
```

`raw/`는 **옮기지 않는다** — 그 경로 문자열이 모든 노트의 `sources:`에 저장돼 있고 lint L1-7/L1-8이 그 참조를 검증하므로, 재경로화는 전 노트
provenance 사슬의 재작성이다(`stratum:66-73`).

### 3.2 이행 — v0.2 클린 브레이크 (인플레이스 마이그레이터 없음)

이행 경로는 하나다: **`agora export --format okf` → `agora repo init --schema 2` → `agora import`.** 마이그레이션·호환 몫(대략 2–3인월)이 사라지고,
도그푸드 KB는 하루~이틀의 재임포트로 재구축한다 —
**재큐레이션이 아니라 재임포트여야 한다**(`raw/`가 오너 산문과 바이트 동일이라 재생성은 오너의 글을
패러프레이즈한다, `stratum:130-133`).

**선언 시점: 관문 A(#154) 통과 후, H2 종료 시점.** 그 전에 선언하면 문제가 생겼을 때 원인이
레이아웃인지 무결성 경계인지 가릴 수 없다. **90일 안에 v1 도메인 레이아웃 아래 새 티어를 만들지 않는다** — 만들면 비준 시 두 번 마이그레이션한다.
90일을 넘기면 폴백은 `#156`(`domain:` 프론트매터 물질화)이고, 그때만 무복귀선을 닫는 값을 낸다.

### 3.3 `people/` 규칙 (Codex 지적 해소)

사람이 소유하는 트리다. **큐레이터는 절대 쓰지 않고**, 검색·그래프·web·MCP는 1급 코퍼스로 **읽는다.** 사람의 기여가 `wiki/` 나머지에 닿는 유일한 길은
`file:` 커넥터 → 후보 게이트다(`stratum:44-46`).
**불변식 2는 큐레이션된 위키를 지배하며, 사람 소유 네임스페이스는 그 바깥이다** — 레이아웃 ADR이 이
문장을 명시해야 한다. 이렇게 하면 "한 저장소, 두 네임스페이스"가 안전을 사려고 지불하던 값(사람 영역이 검색되지 않음)을 되돌려받는다.

### 3.4 OKF · Obsidian

| 방향 | 계약 | 산출물 |
|---|---|---|
| **produce** | 표준 마크다운 본문 링크(ADR-0014 D3) — Obsidian이 그대로 읽는다 | `schema/okf.py` 생산자 적합성 검증기 + `agora doctor --okf` |
| **consume** | **손실 있음** — 본문 위키링크 재작성(`vault_import.py:522-535`), 해소 불가 `related:`/`children:` 드롭(`:584-593`) | `tests/fixtures/okf/divergence.md` 손실 발산표 |
| **1일차 설정** | Obsidian → Files & Links → **"Use [[Wikilinks]]" OFF** | `GETTING-STARTED.md` + `LIMITATIONS.md` |

바이트 동일 라운드트립은 기각한다. ADR-0014가 네 곳에서 막는다(자유형 `type` vs `schema/lint.py:85`의 4값 폐쇄 · `vault_import.py:196` 타입 강제 ·
`extra=forbid` vs 미지 키 보존 · 깨진 링크 발산). 지배 원리는 **엄격 생산자 / 관용 소비자**다.

### 3.5 프론트매터 (schema 2 목표)

| 필드 | 값 | 비고 |
|---|---|---|
| `kb` | ULID | 자기-식별. 노트가 복사돼 나가도 출신을 안다 |
| `kind` | `concept\|summary\|note\|map\|entity` | 디렉터리의 미러 — **디렉터리가 권위** |
| `subjects` | `[]` | 옛 `domain`. 초기값 `[]`은 아무것도 잃지 않고 아무것도 단언하지 않는다 (ADR-0022 무손상) |
| `sources` | `["raw/_blob/<sha>", "docs/<id>#p12"]` | lint L1-7/L1-8이 해석 가능성 검증 |
| `provenance.writers` | 인증된 principal | **신뢰됨** |
| `provenance.agents` | 에이전트 자기신고 | **기록하되 신뢰 안 함** — R4 custody는 이 분리 없이는 거짓말이다 |
| `origin` | `harvest:<agent>` / `web:<user>` / `agora:<kb_id>` | 마지막 형태는 부착 반입용 신규 |
| `derived` | bool | 제안 플레인 산출물 표식. gold·MERGE 타깃에서 제외 |

## 4. 멀티-KB

### 4.1 신원

- **`kb_id` = ULID**, `agora repo init`에서 1회 각인, `_meta/kb.yaml`에 git-tracked, 노트 프론트매터
  `kb:`에도 스탬프. 닫힌 키 집합 — `kb_id` · `name` · 조언적 `declared_kind`뿐. **정책은 들어가지
  않는다**: `load_harvest_policy`가 `harvest.*`와 `kind`를 **둘 다** `_kb/repo.yaml`에서 읽는다
  (`config.py:544-565`). git-tracked `kind`는 "상류 저자의 `kind: personal`이 하류 운영자의 개인
  스코프 커넥터를 해금"하게 만들어 **로컬 안전 선언을 원격 주장으로 바꾼다.**
- **레지스트리 키 = 로컬 별칭**(alias). `kb_id`는 표시·조인용이며 원격의 **자기 주장**이다.
  `agora kb attach`는 **`(kb_id, transport+url)` 복합 지문**을 기록한다 — 같은 지문 재부착은 거부,
  `kb_id`만 겹치면 경고 + 별칭 강제(정상적 fork).
- **사칭은 닫히지 않는다.** 원격 주장이므로 구조적으로 닫을 수 없다. 완화는 provenance 배지에
  `alias · kb_id · transport`를 **항상 병기**하는 것뿐이다.

### 4.2 페더레이션 — 밴드

KB별 결정론 질의는 **손대지 않는다**(ADR-0012 무손상). 신규 필드는 `SearchHit`이 아니라 래퍼에 붙는다: `FederatedHit = {kb_id, kb_alias, kb_role,
transport, commit, fetched_at, rank_in_kb, kb_local_score, hit: SearchHit}`. 기본 병합은 **선언 순서 밴드**, 옵션 `interleave`/`concat`.
**크로스-코퍼스 원점수 비교 금지** — IDF가 레포별이라 비교 자체가 불가능하다. RRF는 기각한다(서로소 문서 집합에서 RRF ≡ interleave라 채택 이유가 없고,
채택하면 ADR-0012 §11 판정을 건드려야 한다). §13(BM25F 헌법)의 LLM 리랭크 꼬리는 **밴드 위에** 앉고, 쓰기 경로 오라클(#144)은 절대 건드리지 않는다.

### 4.3 예시 YAML

```yaml
# $AGORA_HOME/registry.yaml   (기본 ~/.agora, 절대경로 오버라이드 AGORA_HOME)
version: 1
kbs:
  general:                                  # ← 키 = 로컬 별칭
    kb_id: "01J8Z…"                         # 원격 자기 주장. 표시·조인용
    path: "/Users/me/knowledge"
    role: owner
  hana-research:
    kb_id: "01J9B…"
    transport: "git+ssh://git@forge.example/hana/kb.git"
    mirror: "/Users/me/.agora/remotes/hana-research"
    role: reader                            # 부착 = 구조적 읽기 전용

# $AGORA_HOME/profile.yaml    (DESIGN.md:395-402가 예약한 ~/.agora/profile.yaml)
version: 1
read: [general, hana-research]              # 선언 순서 = 밴드 순서
write: general                              # 기본 쓰기 대상
merge: { mode: bands, per_kb_limit: 5 }     # CLI·MCP·웹이 같은 객체를 소비

# <kb>/_meta/kb.yaml          (git-tracked · 닫힌 키 집합 · 정책 금지)
kb_id: "01J8Z…"
name: "general"
declared_kind: personal       # 조언적. 강제하는 값은 로컬 _kb/repo.yaml의 kind
```

## 5. 공유와 멀티테넌트

### 5.1 S1 — 부착 (읽기 전용). 정책이 아니라 구조로

커밋에 고정된 fetch-only 클론 + 미러 루트의 `MIRROR` 마커 + 레지스트리 `role: reader`.
**`curate`/`watch`/`requeue`/웹 업로드는 해소된 저장소 루트가 등록 미러이면 거부한다.** 이게 없으면
`config.py:173-180`(repo.yaml 부재는 오류 아님) + `_GITIGNORE = _kb/`(클론에 인박스 없음) 조합 때문에
**클론에서 curate가 그냥 성공한다.** 반입 자세: 격리 클론 · 심링크/초과크기/비-`.md` 거부 · 모든
원격 발췌에 `strip_sentinel_spans` + 리댁터 통과 · **원격 노트의 gold 진입 금지** · provenance 배지.

### 5.2 S2 — contributor (오너가 연 "읽기 전용보다 나은 모델"의 답)

원격 KB의 **`_kb/inbox/<principal>/` 네임스페이스에만 append**할 수 있는 역할. `wiki/`는 못 건드린다. 원격의 큐레이터가 여전히 유일한 라이터이므로
**읽기 전용과 거의 같은 안전성으로 기여를 받는다** — CQRS를 채택한 순간 떨어지는 배당이고 PR 워크플로가 필요 없다. 전송 2택: **(A) pull 반전** —
오너가 기여자 원격에서 fetch(양쪽 서버 불필요, **권장**) · **(B) 서버측 ref ACL** — SSH forced-command 또는 Forgejo 보호 브랜치. "서버 없이
git만으로"는 거짓이었다(`DEPLOY-TEAM.md` Footgun 4).

수신 측은 기여물을 **흡수하지 않고 전부 `Inbox.write`로 재발행**한다: `id`는 서버 배정, `writer`는
**인증 주체에서 파생하고 파일 내용에서 절대 취하지 않는다**, `kind=candidate`/`confidence=low`는
수신 측 스탬프. 오늘 `is_gated`는 **페이로드에서 계산된다**(`curator/bundle.py:268`) — 기여자가 `kind: capture, confidence: high`라고 적으면 게이트를
우회한다. 따라서 `_kb/repo.yaml`에 per-repo `inbox.trust` 맵을 두고 워커가 번들링 **전에** 비-로컬-신뢰 네임스페이스에 `is_gated=True`를 강제한다.
**`agora promote` 에어락:** 부착 KB의 지식을 자기 KB로 채택할 때는 목적지 인박스로 넣고 원본
`kb_id/revision/path/hash`를 보존한다. 위키끼리 merge하지 않는다.

### 5.3 신원 → 인가 → 경로 (순서가 계약이다)

1. principal 인증 → 2. 불투명 `kb_id`에 대한 권한 판정 → 3. `AuthorizedRepoHandle` 생성 →
4. **그 뒤에만** 파일시스템 경로 해석 → 5. RepoEngine 위임. **caller가 넘긴 path를 먼저 열고 나중에
검사하지 않는다** — ADR-0036의 핵심 경계다(`0036-authn-authz.md:161`). 오늘 `auth/__init__.py`는 docstring 2줄이므로 이 전부가 순증 작업이다.

**큐레이션 홈은 KB당 정확히 하나**(DESIGN §7 V12)이고 다른 환경은 전부 얼굴이다. 오늘 락은 host-local
단일 `fcntl.flock`(`curator/claim.py`)이므로 **writable 클론의 active-active와 양방향 sync는 금지**다. 펜싱 리스는 `refs/agora/*` 조율 네임스페이스로
두되, **리스 ref가 없는 저장소는 오늘과 정확히 동일하게 curate된다** — 개인 경로가 새 실패 모드를 얻지 않는 것이 채택 조건이다.

### 5.4 ADR-0036 OD-1..4 권고값

| OD | 권고 | 근거 |
|---|---|---|
| **OD-1** 신원 출처 | **A — Forgejo PAT 위임** | 발급 코드 0. 회전·폐기가 계정이 사는 곳에 남는다. B(자체 발급 토큰)는 git 호스트를 거부하는 실배포가 나타날 때의 문서화된 폴백 |
| **OD-2** 토큰 범위 | **A — 토큰 = 호출자 신원, 서버가 repo별 인가** | Forgejo PAT 의미론과 일치, 클라이언트 스토리가 가장 단순. C(토큰 내 스코프 클레임)는 Phase-5 OAuth 형상 |
| **OD-3** `trusted_header` 우선순위 | **A — 모드 배타** (둘 다 설정 시 `ConfigError`) | #67 자세와 동일: 오타 난 보안 키가 조용히 신뢰를 바꾸지 않는다 |
| **OD-4** 운영 표면(`/metrics`·`/dashboard`) | **A — 허브 로컬/프록시 차단 유지**, `#51` 2단계(웹에 *제어*가 붙는 순간) B로 전환 | 지금 코드 0. 전환은 그 변경 자신의 몫 |

넷 다 ADR 본문의 자기 권고와 일치한다(`0036-authn-authz.md:294-328`). **Accept는 이 넷을 고르는 행위이지 새 논증이 아니다** — H2의 첫 단위인 이유다.

## 6. 장문 (R7)

**상태: `drafted` — 코드 0.** `docs/notes/openkb-compatible-long-document-compiler.md`는 배너에
"NOT ratified"를 못박고 있고, `docs/adr/README.md` 예약 목록(0026·0028–0035)에 장문 항목이 없다.

**ADR-0040 초안 범위(H2, 초안만):** 원본 바이트 → `raw/_blob/<sha256>` 불변 저장 → 추출기가
`docs/<doc_id>/pages/<n>.md` + `manifest.yaml`(페이지 트리·헤딩·바이트 범위) 생성 → 큐레이터가 `wiki/summaries/<slug>.md` 항해 노트를 저작하고
`sources:`가 `docs/<doc_id>#p12`를 가리킨다. 인용은 `kb_id/revision/blob_sha/unit/offset`까지 해석 가능해야 한다. 컴파일러는 **순수 변환**, 발행은
큐레이터.

**해제 조건 셋 (전부 참이어야 구현 시작):** ① ADR-0040 Accepted — `raw/_pages/` 파생물이 정본을
침범하는 예외를 논증해야 한다(`stratum:130-133`이 `raw/` 이동을 "하지 않는 것"으로 판정했다) · ② Stratum `summaries/` 티어 착지(#153 비준 종속) · ③
읽기 동사 셋(H0)이 먼저 존재.
**구현은 90일 밖이다. 초안 작성만 H2에 넣는다** — 초안조차 미루면 요구가 조용히 증발한다.

## 7. 살아있는 KB (R8)

| 루프 | 기전 | 게이트 / 동의 | 루프 차단 | 선행조건 | 시점 |
|---|---|---|---|---|---|
| 세션 → KB | `session:` 커넥터 → 후보 게이트 → 인박스 | `is_gated`, `confidence=low` | 커넥터 경계 리댁션(ADR-0023) | **#147** — `session_connector.py:130`이 `reader or ClaudeCodeJsonlReader()`라 **모든** `session:<agent>`가 Claude Code JSONL로 파싱된다 | **H0** |
| 인사이트 → 제안 | `_kb/proposals/insights/` | `agora insight promote` (사람) | **제안 플레인은 검색 코퍼스 밖**·`derived: true` | 제안 디렉터리 계약 | H2 (스켈레톤) |
| 인사이트 → 스킬 | `_kb/proposals/skills/` + dry-run/staging export | 사람 승인. **에이전트 스킬 디렉터리 자동 쓰기 금지** | 같음 | **ADR-0026이 스킬 기능보다 먼저** | H2 (ADR 초안) |
| KG → 온톨로지 개념 | `wiki/concepts`/`entities`로의 제안 | harvest 후보와 동일 게이트 | 같음 | ADR-0028 | 90일 밖 |
| KB → 에이전트 메모리 | `agora link <agent>` — 센티널 펜스 구간에 refresh-only | **명시 동의 파일**, `(KB, 해소 경로, scope)` 3-튜플로 키잉 | 배출 원장 `$AGORA_HOME/emit/<kb_id>/`; 입력 프로파일 해시 변경 시 `--refresh` 거부 | 경로 계약(§7.1) | **H2** (동의·경로 계약) |

**경로 계약 (전부 테스트):** 절대 경로 또는 `~`-확장만 · realpath 해소 · **어떤 등록 KB 루트의
서브트리에도 속하지 않음** · 심링크 아님 · `layout.schema_file`·`_meta/**`·`_templates/**` 및 스키마 심링크 셋(`curator/constants.py:30-32`)과 **동일
inode 아님** · 센티널 영역 `region_sha256` 불일치 시 덮어쓰지 않음. **루프는 닫혔다고 주장하지 않는다** — ADR-0027 §8 그대로 카운트 > 0은 **에러가
아니라 루프 신호**이고 ADR-0017 §5는 NOT claimed closed다. 재서술 루프는 텔레메트리로 관측하고, 차단의 주축은 후보 게이트 · 축자 span-drop ·
`_HARVEST_SHARE_CAP`으로 남긴다.

## 8. 에이전트 (R10/R11)

### 8.1 "1급"의 정의 — capability set

`adapters.yaml`의 `agents:` 블록에 **운영자가 선언**하는 다섯 항목이다 — ① 세션 트랜스크립트 리더 (포맷 선언) ② CLI 브레인 argv 프로파일(ADR-0016) ③
`agora link` 메모리 주입 대상 경로 ④ MCP 클라이언트 검증 ⑤ CI 적합성 테스트 1행. 엔진은 **선언된 capability에만 반응하고 이름에는 절대 반응하지
않는다**(불변식 6, STRATEGY §11: 1급 채택 / 특권 기각). 미선언은 '없음'이 아니라 **'미상'**.

### 8.2 4행 상태

| 에이전트 | 세션 리더 | CLI 브레인 | 메모리 경로 | MCP | 오늘 |
|---|---|---|---|---|---|
| **claude-code** | ✔ (유일한 하드코딩 구현) | ✔ 실측 | `~/.claude/CLAUDE.md` | ✔ | **검증** — 단, 지금 특권을 받고 있는 쪽이다 |
| **codex** | 미상 (`format:` 키 없음) | ✔ 실측 | `AGENTS.md` | 미상 | **검증**(브레인) |
| **copilot** | 미조사 | 미조사 | `.github/copilot-instructions.md` | 미조사 | **미조사** — `source="copilot"`은 오늘 **검증 오류**(`FIXED_SOURCES`에 없음) |
| **aelix** | 미조사 | 미조사 | 미상 | 미조사 | **미조사** — `source="aelix"`도 거부됨 |

**"aelix를 1급으로 만드는 작업"과 "Claude Code의 특권을 해제하는 작업"은 같은 커밋이다**
(STRATEGY §11, #147). 그것이 H0에 들어가는 이유다. `doctor --agents`가 **선언 vs 프로브**를 출력해 이 표를 채우는 것이 H0의 종료 조건이다.

### 8.3 aelix = 실행 플레인 · CLI-first

플레인 법: **아고라 = 기억, aelix = 실행.** 스킬·태스크 보드·제안 실행은 aelix에 살고, `agora-bridge-aelix`는 별도 저장소다(DESIGN §10 V7). 이 설계가
요구하는 **신규 API 표면은 0개**다 — 페더레이션은 파일과 git이고, 에이전트는 argv 기반 헤드리스 CLI를 **순수 텍스트 생성기**로 쓴다.

## 9. 웹 (R9)

**H1에 최소 스위처만.** 나머지(인사이트 큐, 밴드형 전역 검색 UI, 제안 리뷰, 장문 트리, SSE 진행)는 90일 밖이다. 착지 시점에 유효한 결정 셋:

1. **URL 공간 유지 + `?kb=` 셀렉터.** 기존 웹 테스트 스위트가 마이그레이션이 아니라 확장이 된다.
2. **미들웨어 해소 규칙.** 프로파일 모드는 `$AGORA_HOME` 레벨 `web:` 정책 **하나**를 요구하고, KB별 정책이 불일치하면 **기동을 거부한다**
   (조용한 완화 금지). 오늘 미들웨어는 앱 레벨이다.
3. **`/insights`는 제안 플레인이 생긴 뒤.** 그때까지 대시보드가 보여줄 것은 **루프 텔레메트리**뿐이다 — 에코 near-dup, harvest 비율 vs
   `_HARVEST_SHARE_CAP`, 리댁션 카운트, 배출 원장 + 원클릭 unlink. 새 op가 필요 없다.

## 10. 외부에서 가져오는 것 (R12)

**T0′ 신설:** *permissive 소스를 **설계·계약·어휘·UX 형상**을 위해 읽는 것은 코드·프로세스·의존성 0.
표현의 축자 복사는 T2 벤더링 + 상류 헤더 보존 + 출처 명시.* T0는 진짜 아티팩트 피드에만 남긴다.

| 프로젝트 | 가져오는 것 | 종류 | 티어 | 안 가져오는 것과 이유 |
|---|---|---|---|---|
| **OpenKB** (Apache-2.0) [조사] | 문서 요약 층 · `sources` 티어(`full_text:` 포인터만) · 컴파일 어휘 · Workbench UX 형상 | 아티팩트 피드 + 설계 | T0 + T0′ | **동거 금지** — 롤백이 `wiki/concepts`를 디렉터리째 스냅샷 후 백업에 없는 라이브 파일을 `unlink()` (STRATEGY §10). 상호운용은 `agora export`뿐 |
| **OpenViking** (AGPL-3.0) [조사] | 설계 1건: JSONL **바이트오프셋 커서 + inode 회전 감지** — 우리 `SessionConnector` 커서 강건성에 직접 적용 | 설계만 | **T4 영구** | 엔진 전부. AGPL이고 PyPI SDK는 **라이선스 선언이 아예 없다**(`0005-fully-oss-bom.md:81`). 링크·벤더링 금지 |
| **Graphify** (Apache-2.0/MIT) [조사] | **슬러그 계약** — 비-ASCII 보존 · 링크≡디스크 파일명 · Windows MAX_PATH 예산 · 충돌 접미사. #136이 터뜨린 결함 클래스 | 계약 | T0 + T0′ | 엔진 전부. `graph.json`에 `schema_version`이 없어 안정 입력 계약이 안 된다 |
| **Obsidian** | 본문 마크다운 링크 호환 · 로컬-우선 UX 형상 · (선택) `.base` 방출 = 공짜 두 번째 UI | 포맷·형상 | T0′ | 위키링크 정본화. 소비는 손실 있음(§3.4) |
| **DuckDB (+`markdown` ext, MIT)** [조사] | 로컬 SQL 분석 · 페더레이션 후보 생성 | 옵션 read model | T1 **조건부** | **코어 의존 아님.** `[analytics]` extra, KB별 `_kb/` 파생, 재구축 가능, **보안 경계도 SSOT도 아님**. 선행조건: `docs/BOM.md`(#157) 전이 폐포 검사. 첫 페더레이션은 기존 순수 파이썬 인덱스 캐시로 낸다. **점수는 언제나 순수 파이썬** — 가속기는 score를 계산하지 않는다 |
| **PageIndex Flash** [조사] | 장문 트리 탐색 설계 | 설계 | T2 (벤더링 후보) | 릴리스가 AGPL `pymupdf`를 하드 의존 → 의존이 아니라 벤더링으로만. R7이 밖이라 **지금은 안 한다** |
| SilverBullet · Cognee/Graphiti [조사] | UX 형상 · 온톨로지 어휘 | 설계 | T0′ | 코드 0 |

**미이행 의무:** Apache-2.0 §4는 저작자표시를 요구하는데 이 저장소에 **`NOTICE`가 없다** — T0′ 차용이
늘기 전에 만든다. 임베딩(ADR-0032)도 DuckDB와 같은 자세다: 증거 게이트 미충족, 게이트 유지.

## 11. 바뀌는 것

### 11.1 뒤집는 판정 — 정확히 1건

**`STRATEGY-2026-08.md` §7 "Phase 4 인증 + 멀티테넌시(#69) 즉시 삭제 — 가장 중요한 삭제 항목"과 §14.7의 "삭제 판정 강화"를 뒤집는다.** 그 권고는 **1인·개인 제품** 전제 위에 섰고, R4가 개인 다중 KB · 다중 작성자 팀 KB · 공유 서버를 제품 핵심으로
명시하므로 전제가 사라졌다. **범위는 repo 단위 auth · KB 레지스트리 · 단일 host repo-owner까지다** — OpenFGA·OAuth·분산 멀티마스터로 확대하지 않는다.

**뒤집지 않는 것:** B′(아티팩트 위에서 합성, 런타임 위에서는 절대 안 함) · custody 포지셔닝 · T0–T4 · §11(이름이 아니라 capability) ·
§12(새 `type:` 값 없이) · §13(BM25F는 헌법, LLM이 `ok`/`not_found`를 소유) · Stratum 관문 A/B · 정직성 계약 4건(#144·#146·#147·#152) 선행.

### 11.2 추가 불변식

기존 1–6은 유지하고 둘을 더한다.

> **7. audience 또는 custody가 다르면 다른 repo다.** 크로스-KB 작업은 **read composition** 또는
> **provenance를 가진 inbox event**뿐이다.
>
> **8. 페더레이션은 identity를 지우지 않는다.** 결과·그래프·gold·캐시는 `kb_id`/revision을 보존하고,
> revoke·삭제 시 **repo 단위로 제거 가능**해야 한다.

기존 둘은 문언을 정밀화한다 — **불변식 2**는 "큐레이션된 위키"를 지배한다고 명시(§3.3) ·
**불변식 5**는 "repo = 계정"이 아니라 "repo = 보안·audience·custody 경계"로 개정하고 **한 프로세스가
KB N개를 읽는다**를 구조 조항으로 추가한다(KB별 `Repo`/`RepoLayout`/핸들러 인스턴스, 모듈 전역 가변 상태 금지, 모든 쓰기 경로가 `kb_id`를 명시
인자로).

### 11.3 ADR 계획

| ADR | 성격 | 내용 | 시점 |
|---|---|---|---|
| **0005** | 애드덤 | **T0′ 신설** + `docs/BOM.md`(#157) 최초 행. DuckDB는 BOM 통과 전 티어 배정 없음 | **H0** |
| **0006** | 개정 (AMENDED) | repo = 보안·audience·custody 경계(계정 아님); 한 프로세스가 KB N개를 읽는다 | **H1** |
| **0012** | **손대지 않는다** | 신규 필드는 `FederatedHit`으로 갔고 RRF를 기각했으므로 부칙이 불필요 | — |
| **0014** | 유지 | 라운드트립 기각 → 생산자 적합성 검증기 + 손실 발산표 | H2 |
| **0026** | **신규 저작** (예약 있음) | 아웃바운드 스킬 write-back — 옵트인, dry-run/staging만. **어떤 스킬 기능보다 먼저** | **H2 초안** |
| **0028** | **신규 저작** (예약 있음) | `DISTILL`을 인사이트·온톨로지 **제안** act로 확장. **자동 정본 반영 금지** | H2 초안 |
| **0029** | **신규 저작** (예약 있음) | 에이전트 프로파일 · 세션 포맷 · 주입 동의 · 커넥터 capability 계약 | H1 |
| **0030** | 예약 유지 | **auth 게이트** 크로스-테넌트 합성 | 90일 밖 |
| **0036** | Proposed → **Accepted** | OD-1..4를 §5.4 권고값으로 확정 | **H2 첫 단위** |
| **0037** | 신규 예약 | **로컬 다중 KB 페더레이션** — auth 무관, 읽기 전용, 밴드 | **H1** |
| **0038** | 신규 예약 | 이식 가능한 filelock(#87) — 배타 전용 · 비블로킹 · 프로세스 급사 시 OS 해제 · 네트워크 FS 비지원 선언 | H1 |
| **0039** | 신규 예약 | MCP 계약 변경: `kb_curate` 위임화 + `serve --curation-home` 겸업 예외 | H2 |
| **0040** | 신규 예약 | **장문 계약** — 초안만(§6) | **H2 초안** |
| Stratum 레이아웃 | 번호 **#153이 소유** | 이 문서가 배정하지 않는다 | H2 말 선언 |

접미사 번호(`0030a`)를 쓰지 않는다 — 이 저장소 README는 정수만 쓴다. `README.md`의 예약 주석 갱신은 H0에서 한 번에 처리한다.

## 12. 90일

세 구간은 **엄격한 순차가 아니라 진입 조건 기반이며 의도적으로 겹친다.** 총 13주 ≈ 90일.

| 구간 | 주차 | 단위 | 진입 조건 | 산출물 (완료 판정) |
|---|---|---|---|---|
| **H0** 정직성 + 읽기 | 1–5 | ① #144 쓰기 경로 랭커 심 ② #146 thin-page ③ **#147** `FIXED_SOURCES` + `session: format:` 키 + `doctor --agents` 4행 ④ #152 ⑤ **관문 A** #154 ⑥ **관문 B** #155 ⑦ `docs/BOM.md` 최초 행 ⑧ **읽기 동사** `agora query/read/neighbors` | 즉시 (선행 없음) | 데몬·서버 0개에서 init→import→curate→query 전 체인 e2e 통과; §8.2 표의 미조사 2칸이 채워짐 |
| **H1** hub v1 | 4–9 | ① `_meta/kb.yaml` 신원 ② `$AGORA_HOME` 레지스트리 ③ `profile.yaml` ④ 페더레이션 밴드 + `FederatedHit` ⑤ **S1 부착** ⑥ 웹 `?kb=` 스위처 ⑦ ADR-0037·0038·0029 | H0의 #144·#147 착지 | 3-KB 픽스처에서 밴드 순서 테스트 통과; `role: reader` KB 바이트가 어느 gold 팩에도 없음을 테스트가 단언 |
| **H2** auth + 진화 스켈레톤 | 8–13 | ① **ADR-0036 Accept** (OD-1..4 확정) ② auth facade `Principal → AuthorizedRepoHandle` ③ **S2 contributor** (pull 반전 먼저) ④ `_kb/proposals/` 플레인 스켈레톤 ⑤ `agora link` 동의 + 경로 계약 ⑥ ADR-0026·0028·0040 **초안** ⑦ OKF 생산자 검증기 | H1의 레지스트리 + 신원 착지 | 2사용자 × 2팀 × 개인 KB deny-first 매트릭스 통과; 거부 요청이 파일시스템 수준에서 **path를 열지 않음** |

**Stratum 클린 브레이크는 H2 종료 시점에, 관문 A가 통과한 경우에만 선언한다.** 90일을 넘기면 폴백은 `#156`이다.

### 12.1 90일 안에 하지 않는 것 (이름으로 남긴다)

OpenFGA · OAuth 2.1 · **네트워크 MCP (Streamable HTTP — `ROADMAP.md:370`이 auth 항목에 결합)** · 크로스-호스트 active-active 큐레이터 · **writable
원격 부착 · 양방향 sync** · 외부 프로젝트와의 런타임 융합 · 전역 vector/graph 인덱스 · **장문 구현**(ADR-0040 초안만) · 스킬/온톨로지 **자동 적용** ·
`/insights` 큐 · Stratum 실제 이행 · 임베딩(ADR-0032).

### 12.2 무복귀선 — 정확히 셋, 각각 다른 이유

| # | 항목 | 왜 되돌릴 수 없는가 | 언제 |
|---|---|---|---|
| 1 | **#144 쓰기 경로 랭커 심** | `curator/bundle.py:145`가 후보마다 `wiki.query()`를 부르고 op 어휘에 DELETE가 없어 **오병합이 provenance 도장을 달고 영구화**된다. 모델 코드가 존재하기 전에 심을 뽑아야 한다 | **H0 — 이 시점 이후 되돌릴 수 없다** |
| 2 | **Stratum 클린 브레이크 선언** | 인플레이스 마이그레이터를 쓰지 않기로 하는 것이 곧 "언제든 되돌린다"의 포기다 | H2 말. **관문 A 통과가 조건** |
| 3 | **첫 network write 수락** | 외부 주체의 바이트가 들어온 뒤에는 retention·audit 규칙을 소급 적용할 수 없다 | ADR-0036 Accept + 보호 브랜치 + retention 판정 **전에는 넘지 않는다** |

**R13 재검토 시점(H0 종료, 주 5)에 이미 되돌릴 수 없는 것은 #144 하나다.** 2와 3은 그 뒤에 오므로
재판정 시점에는 **여전히 되돌릴 수 있다.** 오너는 이 사실 위에서 재판정한다.

### 12.3 베타 컷라인 (#93 Track A)

**결정자: 오너. 결정 시한: H0 종료(주 5).** b1 태그는 이 계획의 어느 항목에도 막히지 않는다 —
막는 것은 `#107` 수동 smoke 하나이고 그건 **오너만 실행할 수 있다.** 두 갈래:

- **(A) H0 종료 전에 b1을 낸다** — hub 코드가 쓰기 경로에 닿기 전이라 `#107` 재실행이 필요 없다. **권장.**
- **(B) H1 이후로 미룬다** — 레지스트리·부착이 착지한 뒤이므로 `#107` 수동 smoke를 **반드시 재실행**한다.

Windows CI 게이트는 `ROADMAP.md`의 규칙 그대로 **한 번의 CI 런 URL**로 판정하며, `skipif`로 녹색을 만드는 것은 금지다.

## 13. 리스크와 반증 조건

| # | 리스크 | 관측 가능한 반증 조건 | 반증되면 |
|---|---|---|---|
| 1 | **페더레이션이 테넌트 벽을 깬다** | 모든 read/query/graph/gold/upload/curate 조합에서 미인가 접근 **0건**. 거부 요청이 파일시스템 수준에서도 path를 열지 않아야 하고, symlink·중복 basename·위조 `kb_id`·캐시 중 **한 건이라도** 누출되면 반증 | network 모드 중단. 게이트웨이와 repo 워커를 별도 프로세스/OS 신원으로 격리, 캐시를 repo별로 분리 |
| 2 | **hub가 얇은 제어면이 아니라 새 모놀리스가 된다** | 기존 `--repo` CLI와 stdio가 데몬·DB·Forgejo 없이 동작하고 단일-repo 골든 결과가 **바이트 동일**. H1 종료(주 9)까지 2 로컬 + 1 스냅샷을 이 조건으로 연결 못 하거나, 얼굴이 hub를 우회하면 반증 | 신규 기능 동결. 패키지 경계만 분리하고 custody 코어는 재작성하지 않는다 |
| 3 | **관문 A가 예산을 초과한다** | #154(무결성 경계 v2)가 **솔로 3주 안에** #135 TAMPER/DELETE/COVERED-DELETE 매트릭스 + #136 파생 충돌 코퍼스를 다시 만족시키지 못함 | Stratum 8인월은 하한이 아니라 낙관치다. 클린 브레이크 선언 보류 + `#156` 폴백. **단일 최대 결정 변수** |
| 4 | **다중 검색이 정직성을 낮춘다** | 페더레이션의 answer-bearing Recall@10이 "정답 KB를 미리 고른 oracle"보다 **5%p 이상** 낮거나, hard-negation/`not_found` 회귀가 생기거나, 인용이 정확한 revision/unit으로 100% 해석되지 않음 | KB별 그룹 검색을 기본으로 유지, LLM/KG는 표시된 실험 티어로 강등 |
| 5 | **제안 플레인이 값을 못 낸다** | blind 제안 **50건 이상**에서 사람 승인율 50% 미만 | 제안은 staging에만 남기고 자동 정본 쓰기 금지를 계속 유지. `/insights`를 열지 않는다 |

### 13.1 R13 재개봉 조건 (스틸맨) — 셋 중 하나라도 참이면 재개봉

1. **관문 A가 3주를 넘긴다** (리스크 3과 같은 신호). 그러면 "진화 15–18 인월"이 25+가 되고 동률이 깨진다.
2. **페더레이션 ADR 초안의 supersede 목록에 ADR-0012 §0a가 들어간다.** 크로스-KB 랭킹이 `SearchHit`/`QueryResult` 의미론 자체의 변경을
   요구하면 ADR 5건 + `tests/core/`가 한꺼번에 움직인다 — 보존할 자산의 핵심(검색 헌법)이 사라진다.
3. **R4 신원 귀속이 쓰기 경로의 온디스크 문법을 바꾼다.** per-principal 네임스페이스가 가산이 아니라 `_kb/inbox/<writer>/<id>.md` 경로 문법이나
   `InboxItem` 필수 필드의 *모양*을 바꿔야 하면, 불변식 3의 디스크 계약이 깨지고 도그푸드 코퍼스의 provenance 사슬이 무효화된다.

셋 다 **H0 안에 관측 가능**하다. **재검토 시점: H0 종료(주 5). 평가자: 오너.**

## 14. 심사 요약

**5안 · 심사 3인 순위** — 1위표는 갈렸다(`evolve-min` 2표, `hub-over-cores` 1표). `greenfield`는 세
심사 전원 **최하위**. 판정은 `evolve` 3안 / `hybrid` 2안, **restart를 낸 안은 하나도 없다.**

| 안 | 판정 | 접목된 것 |
|---|---|---|
| `evolve-min` | evolve | 스파인 무손상 원칙; R1의 결손이 "읽기 동사 부재"라는 진단 |
| `hub-over-cores` | hybrid | **hub 계층 구조 전체**; 쓰기표면 pytest; DuckDB를 코어가 import 못 하는 곳에 |
| `living-kb` | evolve | 배출 원장 + 드리프트 거부 + `region_sha256`; 제안/승격 분리 |
| `interop-compile` | evolve | Format Freeze Rule; OKF 계약의 테스트화; `RETIRE` = 타깃 자격 박탈 |
| `greenfield` | (플레이스홀더) | 전용 스틸맨으로 재실행 → **EVOLVE + v0.2 클린 브레이크** |

**5렌즈 적대 검증:** 다섯 전부 `holds_with_fixes`, high 20건 / medium 31건. **설계가 뒤집힌 5건** —
RRF → 밴드 합성 · `SearchHit` 가산 필드 → `FederatedHit` · 에코 게이트 차단 → 텔레메트리 · portalocker → stdlib filelock · OKF 바이트 라운드트립 →
생산자 적합성 검증기.

**Codex와 일치:** hub 제어면 · `kb_id` 불투명 식별자 · 인가 후 경로 해석 · 밴드/그룹 검색 · snapshot
attach + promote 에어락 · capability 기반 에이전트 · 불변식 7·8 · ADR-0036 OD 권고값.
**불일치 2건:** ① R4 시점(Codex 채택) ② `people/`이 불변식 2와 충돌한다는 진단 — Stratum 노트가 이미
읽기/쓰기를 분리해뒀으므로 **충돌이 아니라 ADR 문언의 공백**이다.

**workflow 문서의 사실 오류 2건 교정:** ① `docs/LIMITATIONS.md`는 오늘 **§1–§9**다
(`## 8. The web face has no authentication`, `## 9. A hosted CLI-agent brain sends your KB off the machine`). "§1–§7뿐"은 틀렸고 DuckDB 항목은
**§10**이 된다. ② "90일 안 웹 변경 0 / R7 ADR 초안도 밖"은 뒤집혔다 — 최소 스위처는 H1, ADR-0040 초안은 H2다.

**스틸맨 결론:** 재시작 13–18 인월 vs 진화 15–18 인월은 **노이즈 범위 안에서 동률**이고, 동률일 때는 부가 자산이 결정한다 — 살아있는 도그푸드 코퍼스, 이미 결정된 27건, 이중 유지보수 4–6개월의 부재. 셋 다 진화 편이다. 재시작의 최대 이점("하위 호환을 안
지켜도 되는 자유")은 **이미 소유하고 있다.**
