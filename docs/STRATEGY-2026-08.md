# 전략 리뷰 2026-08 — 범위 재조정

> **상태:** 조사 완료 · 결정 대기. §7의 로드맵 판정과 §8의 실행안은 **권고**이며 오너 승인 전까지
> 실행되지 않는다. §2의 결함 4건은 승인과 무관하게 이미 수정되었다.
>
> **범위:** "옵시디언·MCP·git으로 충분한가"라는 질문에서 출발해 2026년 에이전트 메모리 시장을 다시
> 재고, 아고라의 코드를 재현 실험으로 검증했다. 기준 `main @ 0390d5d`, 2026-08-13.

이 문서는 이슈 [#34](https://github.com/handochan/agora-kb/issues/34)에 대한 답이다 — 2026-06-26에
*"obsidian이나 일반적 지식그래프와 agora에서 구현된 것은 동일 개념인 것인가"* 라는 제목으로,
**본문이 빈 채로** 열려 47일간 답이 없던 질문.

## 근거 등급

이 문서의 모든 주장은 셋 중 하나로 표시된다. 판단이 갈릴 때 등급이 낮은 주장을 먼저 의심하라.

| 등급 | 뜻 |
|---|---|
| **[재현]** | 이 저장소에서 명령을 실행해 출력을 확인함 |
| **[코드]** | 소스를 직접 읽어 검증함 |
| **[조사]** | 병렬 조사 에이전트의 실측. 재현하지 않음 |

---

## 1. 평결

> 아고라는 위키 컴파일러가 아니라, 여러 에이전트의 메모리와 세션을 레닥션·게이팅·출처 스탬핑해
> append-only 인박스로 받아내고 결정적으로 되읽어주는 **캡처·거버넌스 사이드카**다.

중복도 차별화도 아니다. **좁히면 산다.**

`src/` 27,309줄 중 경쟁 제품 코드에 대응물이 없는 것은 약 4,900줄이고, 나머지는 이미 유통되고 있는
패키지를 다시 구현한 것이다 [조사]. 살아남는 자산은 다섯 개 — **에이전트 캡처(inbox·`kb_remember`) ·
하베스터 · 커넥터 경계 레닥션 · 아웃바운드 센티널 루프브레이크 · 모델 없는 BM25F**. 이 다섯은 가장
가까운 경쟁 트리에 문자 그대로 존재하지 않는다 (`grep -rniE 'harvest|redact|MEMORY\.md|\.jsonl'` → 0
hit) [조사].

카테고리가 검증되었다는 것은 해자가 생겼다는 뜻이 아니다. **범용 레이어가 이제 소비할 상품이
되었다**는 뜻이다.

---

## 2. 검증된 결함 — 전부 수정됨

전략 방향과 무관하게 유효한 결함들이다. 네 건 모두 재현 → 수정 → 회귀 테스트(수정 없이는 실패함을
확인) 순으로 처리했다.

### 2.1 PASS-2 무결성 게이트 우회 — [#135](https://github.com/handochan/agora-kb/issues/135) [재현]

간판 차별점("결정론적 FINAL-DIFF 게이트가 신뢰할 수 없는 모델로부터 위키를 지킨다")이 PASS-2에서
성립하지 않았다.

```
[TAMPER]          status='published'  failure=None
                  victim body tampered / status flipped active→deprecated: True
[DELETE]          status='failed'   ← LINT L1-2 broken link 가 우연히 잡음. 무결성 게이트 아님
[COVERED-DELETE]  status='published'  victim still exists: False   ← 노트 영구 소실
```

근인은 두 곳이었다. changed set이 `needs_prose`에서 파생되어 `sentinels`의 **부분집합**이었으므로
`apply.py`의 *"path not in sentinels → 거부"* 분기가 프로덕션에서 도달 불가능했고, 최종 게이트는
`ALLOWLIST_DIR_PREFIXES = ("wiki/", "assets/")`라 `wiki/` 아래를 전부 통과시켰다.

`validate_author_diff`는 **이미 올바른 거부 로직을 갖고 있었고 경로만 못 받고 있었다.** APPLY 직후
`git write-tree`로 스코프 기준선을 뜨고 실제 diff를 넘기도록 고쳤다. 복구 경로도 함께 고쳤다 —
`_degrade_prose`는 `needs_prose`만 순회하므로 게이트가 잡아도 범위 밖 오염은 그대로 발행됐다.

**§4.2는 degrade, §4.0은 fail이라는 기존 분리를 유지했다.** `_restore_out_of_scope`는 allowlist 밖
경로(`_templates/`, `raw/` 위조)를 일부러 청소하지 않아 최종 게이트가 종전대로 런을 FAIL시킨다.

> **왜 응급이었나.** 악의적 시나리오가 아니다. 가장 현실적인 케이스는 엉뚱한 노트를 건드리는 로컬 8B
> 모델이고, ADR-0016이 1급으로 지원하는 `claude -p` / `codex exec` 에이전틱 브레인(파일 도구 보유)이
> 정확히 이 구성이다. 그리고 다른 모든 것을 정직하게 공개하는 `docs/LIMITATIONS.md`에 이 항목이
> 없었다.

### 2.2 `agora import` 노트 덮어쓰기 — [#136](https://github.com/handochan/agora-kb/issues/136) [재현]

5개 소스 vault → **2개 파일**, 출력은 `lint: clean`. 노트 3개가 조용히 파괴됐다.

순수 비-ASCII 파일명이 `_slugify`에서 `""`가 되어 리터럴 `"note"`를 취했고, 별개로 서로 다른 stem이
같은 슬러그가 되는 일반 충돌도 막히지 않았다. 패배한 노트는 존재하지 않게 되므로 **중복 basename이
남지 않아 lint L1-1이 볼 게 없다** — 덮어쓰기가 자기 증거를 지운다.

**큐레이터 경로는 이미 이 버그를 고쳤었다** — [#57](https://github.com/handochan/agora-kb/issues/57)이
`_hash_fallback_basename`에 결정론적 `note-<sha8>`를 넣었다. 임포터만 그 수정을 못 받았다. 같은 규약을
재사용하고, 목적지 충돌 감지 패스를 추가했다.

### 2.3 커넥터 `max_files` 하드코딩 — [#137](https://github.com/handochan/agora-kb/issues/137) [코드]

`FileConnector.max_files`가 생성자 기본값 64인데 `adapters.yaml`에 올릴 키가 없었다. 5인
`notes/<사람>/**` 레이아웃에서 즉시 활성화된다 — 90개 노트 실측에서 저자별 30·30·4로 잘렸다 [조사].

절단은 지연이 아니라 **도달 불가**다. 잘린 파일은 whole-source `content_sha256` 다이제스트에 들어가지
않으므로 그 파일을 수정해도 재스캔이 트리거되지 않는다.

`ConnectorSpec.max_files: int | None`을 추가하고 fail-loud로 파싱한다(오타가 조용히 기본값으로
돌아가면 오퍼레이터는 올린 줄 안다). 미설정 시 기존 동작과 바이트 동일.

### 2.4 손편집 노트가 큐레이터를 죽임 — [#138](https://github.com/handochan/agora-kb/issues/138) [재현]

`wiki/`에 프론트매터 펜스 없는 노트가 하나 있으면 strict `parse_all_notes`가 처리 안 된 트레이스백으로
탈출했다(`run()`이 잡는 예외는 `LockHeld` 하나뿐). 클레임된 배치가 `_kb/processing/`에 갇히는데
`agora status`는 `failed_events: 0`, `agora doctor`는 `status: healthy`를 출력하고, `agora watch`는
매 틱 복구→재클레임→재크래시하는 **영구 라이브락**이 된다.

저장소를 옵시디언에서 열고 노트 하나를 저장하면 도달한다. `strict=True`는 유지하고(불완전한 레지스트리는
모델의 플랜을 오채점한다) raise를 계약이 이미 약속한 FAILED 런으로 바꿨다.

> **열린 결정.** 현재 수정은 표준 재시도 예산을 쓰므로 손편집이 3런 방치되면 무관한 캡처가
> `_kb/failed/`로 종료 격리된다 — `agora requeue`로 복구되지만 그 디렉터리는 `agora sync` 백업 범위
> 밖이다. CAS 충돌처럼 예산 면제로 두는 편이 안전하나, CAS 경로는 에러 레코드도 `last_failure`도 쓰지
> 않아 재사용할 수 없고 §5.1 예산은 ADR-0011 명세다. #138에 남겼다.

### 2.5 검색 결함 2건 — 미수정, ADR 개정 필요

**`raw/`가 검색 코퍼스에 없다** ([#139](https://github.com/handochan/agora-kb/issues/139), [코드]).

```
$ rg -n 'raw_dir' src/agora_kb/ | grep -v test
src/agora_kb/core/layout.py:87:    def raw_dir(self) -> Path:   ← 정의 1줄. 읽기 경로 소비자 0개
```

`Wiki._iter_note_files`는 `index.md` + `wiki/**/*.md`만 훑는다. 불변 원본 소스 — 메달리온 bronze 티어
전체 — 가 검색 밖이라, `raw/`에 문자 그대로 들어 있는 문구를 질의해도 `not_found`가 돌아온다 [조사].
**답이 git 안에 있는데 없다고 답한다.**

**어간 정규화가 없다** ([#140](https://github.com/handochan/agora-kb/issues/140), [재현]).

```
_tokenize('curation') ∩ _tokenize('curator')  →  set()
FLOOR = 0.18
WEIGHTS: title 3.0 / aliases 3.0 / tags 2.5 / headings 2.0 / summary 2.0 / body 1.0
```

`FLOOR = 0.18`이 이 0 오버랩을 **"근거 없음"이라는 권위 있는 `not_found` 선언**으로 바꾼다. 그리고
`summary` 가중치 2.0은 검색 품질을 **8B 로컬 모델의 산문 품질에 직결**시킨다.

절차적 선례는 완비돼 있다: ADR-0012 §1/§3은 frozen이지만 **이미 한 번 append-only addendum으로
개정됐고**(#56 CJK bigram), 그때의 문제 서술이 *"The system counted Korean but could not search it"* 로
지금과 동일한 결함 클래스다.

---

## 3. 시장 — 차별화 문단이 절마다 반증된다

`docs/DESIGN.md`의 *"no mature OSS project combines markdown wiki × queue + sleep-time consolidation +
MCP × local-LLM × multi-tenant teams + memory harvesting"* 는 유지할 수 없다. 별점을 의심해 `gh api`로
전수 확인했고 **수치는 전부 실측과 일치했다** [재현, 2026-08-13].

| 저장소 | 별 | 라이선스 | 반증하는 절 |
|---|---:|---|---|
| thedotmack/claude-mem | 90,520 | Apache-2.0 | 메모리 수확 (7개 에이전트 지원) |
| kepano/obsidian-skills | 45,079 | MIT | 스키마를 AGENTS.md로 배포 |
| khoj-ai/khoj | 36,463 | AGPL-3.0 | 마크다운 우선 + 로컬 |
| VectifyAI/PageIndex | 35,155 | MIT | 벡터 없는 검색 |
| TencentCloud/TencentDB-Agent-Memory | 20,395 | — | 팀 공유 메모리 + LLM-Wiki |
| AgriciDaniel/claude-obsidian | 10,788 | MIT | 단일 라이터 트랜잭션 엔진 |
| **VectifyAI/OpenKB** | **3,664** | **Apache-2.0** | **거의 전부** |
| basicmachines-co/basic-memory | 3,641 | AGPL-3.0 | 마크다운 + MCP |
| nex-crm/wuphf | 1,240 | — | 아키텍처 쌍둥이 + 인간 검증 게이트 |

유일하게 안 반증되는 절이 **"multi-tenant teams"** 인데, 그건 정확히 출하하지 않은 절이다 —
`src/agora_kb/auth/__init__.py`는 2줄 docstring이다 [코드].

`claude-obsidian`이 특히 아프다. 2026-04 생성, 4,680줄 트랜잭션 엔진, `fcntl.flock`, journal/rollback,
op별 경로 권한 — 아고라의 핵심을 독립적으로 재발명했다 [조사].

---

## 4. 옵시디언 + git — 읽기는 오늘 되고, 쓰기는 안 된다

ADR-0014의 *"빌드 없이 하나의 저장소 = git + 옵시디언 vault + OKF 번들"* 은 **읽기 경로에서 사실**이다.
프론트매터 없고 파일명이 한글이고 CRLF인 생 옵시디언 노트를 `wiki/`에 두기만 해도 BM25F가 정상
랭킹하고, 웹 페이스·`/api/graph`·MCP 7종이 그 위에서 동작했다 [조사].

쓰기는 §2.4가 그대로 걸린다. 그래서 작동하는 구조는 **한 저장소, 두 네임스페이스**다.

```
/srv/team-kb   ← 정본 git 저장소 = 옵시디언 vault
  wiki/ index.md log.md raw/   ← 큐레이터 소유. 사람은 옵시디언에서 읽기만
  notes/<사람>/**              ← 사람 소유. 아고라가 못 봄 = 못 망가뜨림
  _kb/  (gitignored)           ← agora sync 백업 범위 밖. 별도 파일 백업 필수
```

둘 사이는 `file:` 하베스터 커넥터가 잇는다. 실제 Ollama 브레인으로 끝까지 검증됐다 —
`harvest → found=1 written=1 → curate → published → kb_query`가 큐레이터가 다시 쓴 문장을 반환 [조사].

**첫날에 정해야 하는 것 둘.** 본문 `[[위키링크]]`는 그래프 엣지가 0개다(`schema/notes.py`는
`[text](target.md)`만 매칭) — 옵시디언 링크 형식을 첫날에 바꿔야 한다. 마이그레이션 경로가 없다.
그리고 `max_candidates_per_run` 기본 32 × 야간 cron 1회 = 밤당 32개 상한인데 5인 유입은 무제한이다.

---

## 5. PageIndex — 경쟁자가 아니라 부품

**한 개 문서 내부**의 목차 항해자다. 자기 문서가 인정한다 — *"reasoning-based RAG within a single
document by default"*, 그리고 여러 문서 중 무엇을 볼지 고르는 방법으로는 **"청크로 쪼개고 임베딩해서
벡터 DB에 넣으라"** 고 권한다 [조사]. 간판 문구 "No Vector DB, No Chunking"은 문서 내부에서만 성립한다.

코퍼스 레벨 "File System"은 Enterprise 전용이고, OSS 리포에는 **리트리버 구현 자체가 없다**
(*"More details will be released soon"*). OSS 22,103줄 중 17,059줄(77%)이 Flash이고 그 안에 LLM 호출
0 hit [조사].

**건질 것 하나.** PageIndex Flash(`summary=False`)는 LLM이 전혀 없고 결정론적이고 오프라인이다
(222p→264노드 5.5초, 두 번 돌려 SHA-256 동일) [조사]. 현재 `ingest/extractors/pdf.py`는 pdfminer 평면
텍스트라 200페이지 PDF의 헤딩을 전부 파괴한다. Flash를 얹으면 `headings` 필드(가중치 2.0)가 살아난다 —
기존 Extractor Protocol에 그대로 맞고 **seam 변경 0, MIT, invariant-4 clean, 결정론 계약 무손상**.

**리트리버로는 안 된다.** ADR-0012 §0a는 가속기가 후보집합을 over-approximate할 때만 허용하는데 트리
탐색은 가지치기가 존재 이유라 부분집합을 낸다. 더 깊은 파탄은 `not_found`가 사라지는 것 — 트리 탐색기는
어떤 코퍼스에서든 최선의 가지를 반드시 내놓으므로 **아고라의 가장 방어 가능한 속성이 환각 표면으로
바뀐다.**

> **벤치마크 교훈.** 공개된 FinanceBench 98.7%는 공개 라벨에서 재계산하면 엄격 일치 **136/150 =
> 90.67%** 이고, 나머지 8pp는 벤더가 자기 불일치를 스스로 사면한 것이다 [조사]. **숫자를 내라. 그리고
> 엄격 수치와 사면 원장을 분리해 공개하라** — 안 그러면 같은 신뢰도 문제를 물려받는다.

---

## 6. OpenKB — 같은 제품, 4개월, 3,664★

같은 회사(VectifyAI)의 OpenKB는 같은 계보(Karpathy LLM wiki), 같은 Apache-2.0, 같은
`raw/`+`wiki/`+index+log 레이아웃, 같은 OKF 베팅, 같은 Obsidian 호환, **`wiki/AGENTS.md`는 파일명까지
동일**하다 [조사].

**정정이 필요한 대목이 하나 있다.** "저쪽엔 쓰기 중재가 없다"는 **거짓이다** [코드]. `openkb/locks.py`의
`kb_lock`이 KB 전역 배타 락을 LLM 컴파일 전 구간 유지하고, `openkb/mutation.py`가 WAL 저널 + 하드링크
스냅샷 + 자동 롤백을 한다. portalocker라 **Windows에서도 동작**한다 — 아고라의 무조건 `import fcntl`
보다 이식성이 높다. 모델은 JSON만 반환하고 코드가 페이지를 쓴다. ADR 없이 4개월 만에 독립적으로
도달했다. **README나 리뷰에 이 문장을 쓰면 확인당한다.**

그래도 OpenKB에 **없는 것**이 남은 해자다 [코드].

| 아고라에만 있는 것 | 부재 확인 | 누구에게 중요한가 |
|---|---|---|
| 에이전트 캡처 `kb_remember` | 사실 단위 엔드포인트 0 | **최상** — 에이전트 3개+ 굴리는 개발자 |
| 하베스터 | `grep harvest` → 0 hit | **최상** — 콜드스타트 해법 |
| 후보 게이트 | 대응물 없음 | 상 — 캡처와 승격의 분리 |
| 경계 레닥션 · 센티널 | `grep redact` → 0 hit | 하베스터 종속 |
| git 히스토리 · SSOT | git 사용처 = `user.name` 읽기 1곳 | 상 — "모델이 뭘 바꿨나" |
| 모델 없는 BM25F | `MAX_TURNS=50` LLM 루프 | 중상 — *하루면 복제 가능* |

그쪽의 구조적 약점: **읽기 에이전트가 쓰기 툴(`write_kb_file`)을 들고 있어** 질문 답변과 위키 수정이
같은 신뢰 도메인이고, 위키 콘텐츠에 히스토리·diff·revert가 없으며, `recompile` 문서는 *"manual edits
are overwritten"* 이라고 그냥 적어놨다 [코드].

**단, 그들의 로드맵(#151 병렬 인제스트, #172 lock-free prepare)은 동시 쓰기로 직진하고 있다. "멀티
에이전트 캡처"에서 아고라의 선행 구간은 분기 단위지 연 단위가 아니다** [조사].

---

## 7. 로드맵 판정 — 결정 대기

삭제가 추가보다 중요하다. 아래는 **권고이며 승인 전까지 실행되지 않는다.**

### 즉시 삭제 후보

| 항목 | 근거 |
|---|---|
| **Phase 4 인증 + 멀티테넌시 (#69)** | 1인 프로젝트로 2년짜리다. 팀 인증은 SSO 리버스 프록시가 오늘 해결한다. **가장 중요한 삭제 항목** |
| `ingest/extractors/` 확장 | markitdown + PageIndex가 더 넓고 멀티모달까지 있다 |
| 웹 페이스·대시보드·그래프 시각화 추가 개발 | 유지보수만. 동등물이 존재 |
| `schema/` 확장 · deck/skill-factory류 | 양쪽 다 OKF-ready. 차별화 0 |
| Phase 5 OpenFGA 도메인별 ACL | 2026-07 자체 리뷰가 이미 "10개 채널로 샌다"고 결론 |

### 보류 — 오너 결정 필요

**Windows 에픽 #85 중 `curator/` 부분.** 큐레이터를 좁혀내면 `fcntl` 의존도 사라지고, 하베스터·인박스·
core는 오늘 이미 Windows-clean이다 [조사]. **단 이 권고는 오너의 기록된 결정("네이티브 Windows 무조건
지원, WSL2는 임시")과 정면 충돌한다.** 뒤집을지는 오너가 정한다.

### 즉시 추가 — 전부 모델 불필요

1. ADR-0012 §1/§3 형태소 정규화 addendum (#56과 동일 형식) → [#140](https://github.com/handochan/agora-kb/issues/140)
2. `summary` 가중치 하향 또는 lint 게이트 → [#140](https://github.com/handochan/agora-kb/issues/140)
3. PageIndex Flash를 결정론적 구조보존 PDF 추출기로 채택 + 기본 off의 비계약 `kb_explore` →
   [#139](https://github.com/handochan/agora-kb/issues/139)

### 재정렬

**ADR-0032(임베딩)를 다음 순번에서 내린다.** PageIndex의 35k★는 "구조+추론이 평면 벡터를 이긴다"는
가장 강력한 공개 증거라 임베딩 티어의 근거를 *약화*시킨다. 증거 게이트(#28 기업 볼륨)는 미충족이고,
재현된 검색 실패는 전부 모델 없이 고쳐진다.

`ROADMAP.md`의 *"deliberately absent; the contract, not a gap"* 은 폐기한다. **결정론(랭킹 함수의
성질)과 리콜(시스템의 성질)을 혼동한 문장이다** — §2.5의 `raw/` 커버리지 0은 결정론이 100% 보존된 채
리콜이 0인 사례다. `docs/notes/retrieval-vs-vectordb.md` §1이 공개 ROADMAP보다 정직하다(의미 질의의
결정론적 `not_found`를 *"permanently unwinnable"* 로 적어놨다).

---

## 8. 실행 권고 — 승인 대기

**Day 2가 게이트다.** Day 1과 Day 4–5는 되돌리기 어려운 방향 전환이므로 Day 2의 반증 실험 결과를 보고
판단한다.

| | 내용 |
|---|---|
| **Day 1** | 팀 허브를 오늘 세운다 — 아고라는 인증/PyPI/Windows가 없으므로 팀 문제는 로드맵이 아니라 오늘 해결한다 |
| **Day 2** | **반증 실험.** 하베스터를 dry-run으로 돌려 게이트 통과 사실을 뽑고 외부 KB에 넣어본다. 판정 셋: (i) `origin: harvest:<agent>` provenance가 살아남는가 (ii) 사실 200건에서 엔티티 재작성 비용이 폭발하는가 (iii) 저신뢰 후보가 독립 페이지를 만드는가. **(i)/(iii)이 깨지면 사이드카안은 기각이고 큐레이터는 유지된다.** 로컬 Ollama나 합성 데이터로만 |
| **Day 3** | ~~PASS-2 수정~~ — **완료** (§2.1, #135) |
| **Day 4–5** | `agora-harvest` 분리 출시 — `harvester/` + `core/{redact,sentinel,inbox,hashing,layout,gold,wiki}` + MCP 페이스. `fcntl` 없음, Windows clean, **v0.1.0 태그, PyPI 게시** |
| **Day 6** | 상류 제안 — 세션·메모리 커넥터 + 경계 레닥션 + 센티널 루프브레이크 |
| **Day 7** | ADR 하나 — 범위 축소와 그 가격표(`curator/` 8,273줄 + `adapters/` 1,627줄 등 약 14,000줄)를 기록 |

> 태그 0개 / `pip install agora-kb` 404는 기술 문제가 아니라 **이 프로젝트가 아직 존재하지 않는다**는
> 뜻이다 [코드].

---

## 9. 문서 부채 정산

이슈 [#55](https://github.com/handochan/agora-kb/issues/55)(전략 리뷰 2026-07, **미결정 10건**)가 근거
전문으로 `docs/STRATEGY-2026-07.md`를 두 번 가리키는데, 그 파일은 워킹트리에도 **git 전체 히스토리에도
존재한 적이 없다** [재현].

```
$ git log --all --oneline -- 'docs/STRATEGY-2026-07.md'
(없음)
$ git log --all --diff-filter=A --name-only --pretty=format: | grep -i strateg
(없음)
```

9-에이전트 분석의 결론 요약만 이슈 본문에 남고 근거는 커밋되지 않았다. 그 결과 #55의 미결정 10건 —
동의어 위치, 승격 표기법, 벡터 트리거 명문화, gold 세탁 2홉 경로 — 은 검증 불가능한 상태로 매달려
있다. 그중 **벡터 트리거 명문화는 이 문서 §7이 답한다**(ADR-0032를 다음 순번에서 내리고 게이트를
#139/#140의 모델 없는 수정 뒤로 옮긴다).

**이 문서를 커밋하는 이유가 그것이다.** 결론만 휘발성 위치에 남기는 실패를 반복하지 않기 위해, 근거
등급과 재현 명령을 본문에 함께 남긴다. #55는 이 문서를 참조하도록 갱신하거나 닫아야 한다.

## 참조

- 결함 이슈: [#135](https://github.com/handochan/agora-kb/issues/135) ·
  [#136](https://github.com/handochan/agora-kb/issues/136) ·
  [#137](https://github.com/handochan/agora-kb/issues/137) ·
  [#138](https://github.com/handochan/agora-kb/issues/138) ·
  [#139](https://github.com/handochan/agora-kb/issues/139) ·
  [#140](https://github.com/handochan/agora-kb/issues/140)
- 답하는 질문: [#34](https://github.com/handochan/agora-kb/issues/34)
- 정산하는 부채: [#55](https://github.com/handochan/agora-kb/issues/55)
- 관련 설계: [ADR-0014](adr/0014-okf-obsidian-interoperability.md) (Obsidian/OKF 상호운용) ·
  [ADR-0012](adr/0012-deterministic-query-ranking.md) (결정론적 랭킹) ·
  [ADR-0011](adr/0011-curator-ingest-contract.md) (§4.0/§4.2 게이트, §5.1 재시도 예산) ·
  [`notes/retrieval-vs-vectordb.md`](notes/retrieval-vs-vectordb.md)
