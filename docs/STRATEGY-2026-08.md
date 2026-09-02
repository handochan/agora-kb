# 전략 리뷰 2026-08 — 범위 재조정

> **상태:** 조사 완료 · 결정 대기. §7의 로드맵 판정과 §8의 실행안은 **권고**이며 오너 승인 전까지
> 실행되지 않는다. §2의 결함 4건은 승인과 무관하게 이미 수정되었다.
>
> **범위:** "옵시디언·MCP·git으로 충분한가"라는 질문에서 출발해 2026년 에이전트 메모리 시장을 다시
> 재고, 아고라의 코드를 재현 실험으로 검증했다. 기준 `main @ 0390d5d`, 2026-08-13.
>
> **§10–§13 추가 2026-08-15 · §14(4자 합성 판정) 추가 2026-09-02.** 이 문서는 append-only로
> 자란다 — 앞 절의 서술이 뒤에 반증되면 지우지 않고 §14.1처럼 정정 표로 기록한다.

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

> **정정 (2026-09-02).** 이 grep은 **OpenKB 트리에 대해서만** 참이었다. OpenViking은 같은 기능을
> `harvest`가 아니라 `ingest`로 부르며 이미 갖고 있다. 다섯 중 4자 조사에서도 유일하게 남은 것은
> **경계 레닥션 · 아웃바운드 센티널 · 후보 게이트**이고, 하베스터 자체는 더 이상 유일하지 않다 — §14.

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

| 저장소 | 별 | 라이선스 | 계층 | 반증하는 절 |
|---|---:|---|---|---|
| **Graphify** (Graphify Labs) | **109,033** | Apache-2.0 + MIT | T0 | `export wiki`/`obsidian` — 마크다운 위키 + `index.md` + 에이전트 크롤 |
| thedotmack/claude-mem | 90,520 | Apache-2.0 | — | 메모리 수확 (7개 에이전트 지원) |
| kepano/obsidian-skills | 45,079 | MIT | — | 스키마를 AGENTS.md로 배포 |
| khoj-ai/khoj | 36,463 | AGPL-3.0 | T4 | 마크다운 우선 + 로컬 |
| VectifyAI/PageIndex | 35,155 | MIT | T2 | 벡터 없는 검색 |
| **volcengine/OpenViking** | **31,496** | **AGPL-3.0** | **T4** | **하베스터 — claude_code·codex·hermes·openclaw·opencode 세션 인제스트** |
| TencentCloud/TencentDB-Agent-Memory | 20,395 | — | — | 팀 공유 메모리 + LLM-Wiki |
| AgriciDaniel/claude-obsidian | 10,788 | MIT | — | 단일 라이터 트랜잭션 엔진 |
| **VectifyAI/OpenKB** | **3,664** | **Apache-2.0** | **T0** | **거의 전부** |
| basicmachines-co/basic-memory | 3,641 | AGPL-3.0 | T4 | 마크다운 + MCP |
| nex-crm/wuphf | 1,240 | — | — | 아키텍처 쌍둥이 + 인간 검증 게이트 |

> **재발행 (2026-09-02).** 초판 표에는 **1·2위가 빠져 있었다.** Graphify와 OpenViking 두 행은
> 2026-08-21 조사에서 추가됐고 별점은 그날의 스냅샷이다 [조사] — 나머지 행은 2026-08-13 실측이다.
> **계층** 열은 ADR-0005의 T0–T4 애드덤이고, 별점이 아니라 **무엇으로 붙일 수 있는가**를 말한다.
> 판정 전문은 §14.

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
기존 Extractor Protocol에 그대로 맞고 seam 변경 0, 결정론 계약 무손상.

> **라이선스 정정 (2026-08-15) [재현].** 이 문서의 초판은 이를 무조건 "invariant-4 clean"이라고
> 적었다. **조건부로만 맞다.** Flash를 담은 릴리스 `pageindex==0.3.0.dev3`는 `pymupdf>=1.26.0`을
> **하드 의존으로** 끌어오고, PyPI가 보고하는 pymupdf 라이선스는 *"Dual Licensed - GNU AFFERO GPL
> 3.0 or [commercial]"* 이다 — README와 DESIGN §8이 *"pymupdf are AGPL … avoid in redistributable
> core (we use pdfminer.six instead of pymupdf)"* 로 명시적으로 배제한 바로 그 패키지다. 따라서
> **`pip install pageindex`는 ADR-0005 위반이다.**
>
> 다만 `pymupdf`/`fitz`를 실제로 쓰는 것은 *classic* LLM 경로(`pageindex/utils.py`,
> `pageindex/tree_optimize.py`)뿐이고, **`pageindex/flash/`는 쓰지 않는다** — 그 서브패키지의
> 서드파티 임포트는 `pypdfium2`(BSD-3-Clause/Apache-2.0) · `PyPDF2`(BSD) · `regex`(Apache-2.0) ·
> `sortedcontainers`(Apache-2.0) + 표준 라이브러리가 전부다.
>
> 즉 채택 경로는 **의존이 아니라 벤더링**이다 — ADR-0021이 이미 세운 선례(Node/CDN을 들이지 않고
> MIT force-graph를 벤더링) 그대로. 의존으로 넣으면 AGPL이 코어에 들어온다.

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
3. `raw/` 검색 티어 → [#139](https://github.com/handochan/agora-kb/issues/139). 세 갈래 중 결정 필요
   (§5의 라이선스 정정 참조 — PageIndex Flash는 **의존이 아니라 벤더링**으로만 채택 가능하다)

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

## 10. OpenKB 정렬 판정 — 수정 채택 (2026-08-15)

**질문:** "위키 구조·정리 패턴·검색 로직을 OpenKB와 완전 호환으로 맞추고, 아고라는 상시 serving·자율
분석·크론·멀티테넌트로 차별화한다."

**판정: 목표는 옳고 메커니즘만 뒤집는다.** "직접 만들지 말고 얻어라"(직접 구현 8~14 person-month,
그중 PageIndex·멀티모달은 `pymupdf`(AGPL) 때문에 ADR-0005 하에서 **법적으로 불가**)와 "차별화는
위층"은 맞다. 틀린 건 **얻는 방법**이다 — OpenKB의 역량은 디렉터리 모양이 아니라 컴파일러 코드에
있어서, 모양을 맞춰도 역량은 한 줄도 안 따라온다.

**결정적 발견 [코드]:** `openkb add`는 평범한 `.md`를 받는다 (`openkb/cli.py:231`
`SUPPORTED_EXTENSIONS`, `converter.py:225`). 그러므로 필요한 건 위키 모양 흉내가 아니라 **문서를
내보내는 것**이다.

| | (A) 포맷 호환 | (B) 기능 패리티 | **(C) 문서 피드** |
|---|---|---|---|
| src LOC | ~3,100 | ~26,000 | **~300** |
| ADR | 3 폐기 + 7 개정 | 불변식 9회 재개방 | **0** |
| 기간 | 다주 | 8~14 pm | **~2주** |

**한 저장소 공존은 파괴적이다 [재현].** OpenKB 산출물 5종을 `wiki/`에 놓고 `agora curate`를 돌리면
**5/5 전부** 실패시킨다(3종은 `LINT L1-11 unknown type`, 2종은 `LIVE-TREE unparseable note`).
반대로 `openkb add`의 롤백(`openkb/mutation.py:282-290`)은 `wiki/concepts`·`wiki/entities`를
디렉터리째 스냅샷한 뒤 백업에 없는 라이브 파일을 `unlink()` 한다. **오늘 아고라가 살아남는 유일한
이유가 `wiki/<domain>/` 파티션이 그 폭발 반경 밖이라는 것**이고, 포맷 정렬은 정확히 그걸 안으로
옮긴다.

**무복귀선: 도메인 파티션 평탄화.** 나머지는 기계적으로 복구되지만(`_NOTE_TYPES` 참조는 3곳)
도메인은 경로 세그먼트 외 어디에도 기록되지 않는다.

## 11. aelix 1급 판정 — 채택(1급) · 기각(특권) (2026-08-15)

구분 기준: **1급** = 에이전트가 *선언한 능력*에 따라 엔진이 더 요구 · **특권** = 에이전트 *이름*에
따라 엔진이 채점을 바꿈. 불변식 6(`CLAUDE.md:30`)과 ADR-0027:152는 후자만 금지한다.

**오늘 특권을 받고 있는 건 aelix가 아니라 Claude Code다 [코드]** — `core/models.py:28-30`
`FIXED_SOURCES`에 aelix가 없어 `source="aelix"`가 거부되고, `harvester/session_connector.py:130`이
`reader or ClaudeCodeJsonlReader()`인데 `ConnectorSpec`에 `format:` 키가 없어 **모든**
`session:<agent>` 커넥터가 Claude Code JSONL로 파싱된다. 이슈 [#147].

→ **"aelix를 1급으로 만드는 작업"과 "Claude Code의 특권을 해제하는 작업"은 같은 커밋이다.**

공동 설계로만 되는 것 1순위는 **재서술 루프 차단**이다 — ADR-0017:64/0023:176이 스스로 "NOT
eliminated"로 적어둔 미해결 위험이고, 지금 그 텔레메트리는 관측 대상에서 0을 센다([#148]).

## 12. 스키마 강화 판정 — 한다, 단 새 `type:` 값 없이 (2026-08-15)

**질문:** "OpenKB의 좋은 구조 아이디어를 아고라 스키마에 흡수하되 호환도 유지하고 거버넌스는 지킨다."

**결정적 사실 [코드]: BM25F 랭커는 `type`을 보지 못한다.** `_FIELDS = (title, aliases, tags,
headings, summary, body)` (`core/wiki.py:72`)이고 `_Note`에 `type` 필드가 없다. 그러므로 #139의 검색
격차를 메우는 것은 *타입 토큰*이 아니라 **풍부한 필드를 가진 노트가 코퍼스에 존재하는 것**이다.
그리고 `core/wiki.py:802`의 `wiki_dir.rglob("*.md")`는 제한이 없어 **새 하위 폴더가 자동 색인된다.**

→ 문서 층은 **`type: theme` + 가산 프론트매터 키**로 착지시킨다. 새 op 불필요, `schema_version` 범프
불필요, **#63이 선행조건이 아니게 된다**, lint L1-7/L1-8의 출처 강제를 공짜로 상속, 완전히 되돌릴 수
있음.

**6개 아이디어 판정:** 문서 요약 층 **TAKE**(1순위, #139와 같은 작업) · `sources` 티어
**TAKE-MODIFIED**(`full_text:` 포인터만; `raw/`를 `wiki/` 밑으로 옮기는 건 거부) · 엔티티
**TAKE-MODIFIED**(태그 패싯으로, 스키마 변경 0) · explorations **SKIP**(모델 합성물을 소싱된 지식과
같은 층에 넣으면 자기-섭취 루프) · 얇은 프론트매터 **SKIP**(채점 필드 6개 중 4개가 프론트매터
저작이라 능동적으로 파괴적) · 타입 파티셔닝 **SKIP**(`wiki/<domain>/{themes,daily}/`가 이미 중첩 구조).

**도메인: 유지하되 soft화.** 실측([#146])에서 4개 질의 중 3개에서 빈 MOC 스텁이 정답을 이겼고,
한 번은 정직한 `not_found`가 스텁 3개로 바뀌었다. 가장 싼 고가치 구매는 **워커가 `domain:`을
프론트매터에 물질화하는 것** — 무복귀선을 닫아 이후 모든 도메인 결정을 되돌릴 수 있게 만든다.

## 13. 검색 아키텍처 판정 — LLM이 주(主), 결정론 티어가 헌법 (2026-08-15)

전문은 이슈 [#150]. 요지:

**전제 교정 [조사]:** 카파시 LLM-wiki 원안은 렉시컬 검색을 요구하지 않는다 — 그가 권하는 검색
레이어 QMD는 BM25 + 벡터 + LLM 리랭킹을 RRF로 융합하며 **전부 로컬 GGUF**다. `DESIGN.md:22-23`이
카파시 계보 인용 옆에 "not vector search"를 병치해 **"no vectors"가 계보에서 온 것처럼 읽히는데,
아고라가 스스로 추가한 별개 제약이다.** 그리고 `summary` 가중치 2.0이므로 **오늘도 이미 모델이 검색에
들어와 있다** — "모델 프리"가 아니라 모델이 쓴 텍스트 위에서 산술이 재현 가능할 뿐이다.

**실측 [조사]** (로컬 `qwen3.6:35b-a3b`, 1콜, 0.7초, $0 / 수작성 비순환 패러프레이즈 24 + 부정 10):
bm25 단독 r@1 **0.208** · RRF 융합 0.500이지만 **부정 정답률 1.000 → 0.100 붕괴** ·
**llm_then_bm25 r@1 0.583 / r@5 0.750 / 부정 1.000** (McNemar p = 0.0117).

**결정:** LLM이 `ok`/`not_found`와 순서를 소유하고 BM25F가 모델 픽 아래를 backfill 한다. RRF 융합
기각(기권 붕괴), BM25-as-gate 기각(0.458 < 0.583, 그리고 거짓 not_found 33%가 모델 호출을 막는다).
**BM25F는 보조가 아니라 헌법이다** — 오프라인 폴백 · 쓰기 경로 오라클([#144]) · 양성 단락 · 회귀
게이트를 소유한다.

**정정:** `FLOOR = 0.18`은 사실상 죽은 코드다 — 0.0으로 몽키패치해도 `not_found` 10건 중 0건이
뒤집히지 않는다. 실제로는 `_passes_gate`(`core/wiki.py:1380`)가 전부 만든다. #140의 서술을 이에 맞게
정정했다.

**최우선 선행 작업 [코드]:** `curator/bundle.py:145`가 후보마다 `wiki.query()`를 부른다 — 랭커는
읽기 전용 관심사가 **아니고**, 플래닝 브레인의 `MERGE_INTO_THEME` 타깃을 정한다. 오병합은 provenance
도장을 달고 영구화된다(닫힌 어휘에 DELETE 없음). **모델 코드가 존재하기 전에** `Wiki.query_lexical()`
심을 뽑아 고정해야 한다([#144]).

## 14. 4자 합성 판정 — B′: 아티팩트 위에서 합성한다 (2026-08-21/22 조사, 기록 2026-09-02)

이 문서의 §1–§13은 **OpenKB 하나**를 가장 가까운 경쟁 트리로 놓고 쓰였다. 2026-08-21에 두 건이
추가로 조사됐고 — ByteDance Volcengine의 **OpenViking**(31.5k★, AGPL-3.0)과 **Graphify**(109k★,
Apache-2.0+MIT) — 그 결과 §1·§3·§6의 전제 일부가 반증됐다. **이 절의 외부 사실은 전부 [조사]이며
2026-08-21 스냅샷이다.** 아고라 코드에 대한 주장만 [코드]로 재확인했다.

### 14.1 정정 다섯

| 앞서 이 문서가/내가 한 말 | 실제 |
|---|---|
| §1 *"다섯은 가장 가까운 경쟁 트리에 문자 그대로 존재하지 않는다"* | **하베스터는 아니다.** OpenViking은 같은 것을 `harvest`가 아니라 `ingest`로 부른다 — `openviking/ingest/sources/`에 claude_code·codex·hermes·openclaw·opencode 5종 리더, JSONL 바이트오프셋 커서 + inode 회전 감지. 그 PR은 **18.5시간·3,258줄**이었다 |
| "OpenViking은 git을 안 쓴다" | **틀렸다.** git을 Rust로 직접 구현했다(`crates/ragfs/src/git/service.rs`, gitoxide, 기본 enabled, `ov snapshot {commit,restore,diff,log}`). 앞선 grep이 파이썬 라이브러리만 봤다 |
| "파일이 SSOT가 아니다" | **틀렸다.** 평문 파일이 SSOT이고 벡터 스토어는 파생이다(`reindex`로 재구축) |
| "provenance는 아고라만" | **부분적으로 틀렸다.** `source_extraction_id(s)` · `last_update_trace_id`가 파일 트레일러에 지속되고 커밋마다 `memory_diff.json`을 쓴다 |
| "OpenViking SDK는 Apache-2.0이라 붙이는 경로가 깨끗하다" | **파이썬은 아니다.** PyPI `openviking-sdk`는 라이선스 선언이 **아예 없다** — ADR-0005 기준으로 선언된 AGPL보다 나쁘다. TS/npm SDK만 Apache-2.0이 맞다 |

살아남은 좁은 진실 하나: OpenViking의 자동 커밋은 `/memories/experiences/`에만 걸리고
preferences·entities·events에는 자동 히스토리가 없다. 그러므로 방어 가능한 문장은 *"그들은 git을 안
쓴다"* 가 아니라 **"모든 큐레이션이 손으로 clone하고 diff할 수 있는 커밋이다"** 뿐이다. 공개 문서에
전자를 쓰면 grep 한 번에 반증당한다.

### 14.2 4자 지도 — 각자가 **거부하는 것**이 아고라의 자리다

| | 무게중심 | 거부하는 것 | 계층 |
|---|---|---|---|
| **OpenKB** 3.7k★ | 마크다운 위키 **컴파일러** | 사실 단위 캡처 · 히스토리/revert · 레닥션 · 에이전트 출처 | T0 (문서 피드, **동거 금지**) |
| **OpenViking** 31.5k★ | 파일시스템 패러다임 **벡터 컨텍스트 DB** | 경계 레닥션 · 비파괴 dedup · 로컬 sparse/BM25 | **T4 (별도 서비스 only)** |
| **Graphify** 109k★ | **프롬프트 배포**(그래프는 페이로드) | 임베딩 · 산문 지식 · 오프라인 비코드 · 테넌트 경계 | T0 (아티팩트만) |
| **아고라** | **캡처 · 거버넌스 · 보관(custody)** | — | — |

### 14.3 여섯 갭 — G2가 가장 날카롭고 **4자 중 유일**하다

G1 다중 에이전트 사실 캡처 · **G2 아웃바운드 인젝션 방어** · G3 지식에 대한 감사 추적 ·
G4 정직한 `not_found` · G5 하드 테넌트 경계 · G6 결정론 규율.

**G2.** Graphify는 인바운드는 잘한다 — `<|im_start|>`·`<<SYS>>`·`[INST]`·`### system:`을 제로폭
공백으로 defang하고 sha256 `<untrusted_source>` 블록으로 감싼다. 그런데 그렇게 만든 그래프가 호스트
에이전트로 **돌아가는** 경로는 `sanitize_label`(제어문자 제거 + 256자 컷)이 전부다. *"Ignore previous
instructions and…"* 로 시작하는 256자 함수명은 그대로 통과한다. 자기 SECURITY.md가 그 행을 *"Prompt
injection via node labels"* 라고 제목 붙여놓고 코드에 없는 방어를 주장한다. 아고라 쪽 대응물은
ADR-0027 §8 센티널/루프브레이크다.

**G6은 역전이다.** Graphify는 결정론을 **광고하고 실패하고**(`wiki.py`가 *"Community labels are
LLM-generated and non-deterministic"* 라고 자기 파일에 적어놨다), 아고라는 **갖고 있으면서 광고하지
않는다** — ADR-0010 D5 고정 문법, gold 팩 바이트 동일(#37), ADR-0012 구성상 결정론. 어느 것도 외부인이
확인할 수 있는 형태로 공개돼 있지 않다.

### 14.4 판정 — **B′: 아티팩트 위에서 합성한다, 런타임 위에서는 절대 안 한다**

- **A(흡수)는 산술적으로 종결.** OpenKB 26k LOC + Graphify 62,201 LOC + 문법 25종, OpenViking은 AGPL
  이라 어떤 가격에도 복사 불가. 아고라 `src/` 전체가 27.5k줄이다.
- **순진한 합성도 기각.** Graphify가 그 대가를 몸으로 보여준다 — 설치 타깃 23개, 호스트 툴을 DENY하는
  훅까지 갖고도 **내구성 아티팩트를 소유하지 못했다**: `graph.json`에 `schema_version` 없음, 최상위 키
  집합이 라이터 버전에 따라 다름, README·skill·`serve.py`가 서로 다른 MCP 툴 집합 3종을 나열.
- **아고라의 기반은 그 반대다.** 마크다운+git은 벤더의 포맷이 아니라 넷 다 읽을 수 있고 아무도 폐기할
  수 없는 것이다. **inbox 경계에서 합성하고 쓰기 경로를 소유하는 것은 플러그인이 아니다.**

**검색은 위임하지 않는다.** ADR-0012 §0a가 범주적으로 금지한다(*"No accelerator ever computes `lex`,
`struct`, `fm`, `score`, `match_reason`"*) — 외부 검색 서비스는 정의상 score를 계산한다. 그리고 치명적인
건 읽기가 아니라 **쓰기**다: `curator/bundle.py:145`가 후보마다 `wiki.query()`를 불러
`MERGE_INTO_THEME` 타깃을 정하는데 op 어휘에 **DELETE가 없다**(`curator/plan.py:53-77`) [코드].
비결정적 랭커 = 되돌릴 수 없는 오병합에 provenance 도장. 이슈 [#144]가 그 심(seam)이다.
덧붙여 OpenViking의 **완전 로컬 구성은 dense-only이고 sparse/BM25 폴백이 없다** — §13이 "BM25F는 보조가
아니라 헌법"이라고 못박은 바로 그 바닥이 없다.

> **정직한 유보.** B′의 진짜 위험은 플러그인이 되는 게 아니라 **건너뛸 수 있게 되는 것**이다.
> Graphify + OpenKB를 쓰는 사용자는 아고라를 아예 설치 안 할 수 있다. 완화책은 기능이 아니라
> 경계다 — 셋 모두 쓰기 경로를 명시적으로 포기하거나 파괴적으로 다룬다(OpenKB는 손편집을 덮어쓰고,
> OpenViking의 dedup은 삭제하고, Graphify는 지식을 아예 안 쓴다). 그래서 `curator/`는 **잘라내면 안
> 되는 유일한 대형 모듈**이다. §7의 삭제 목록은 이 문장과 충돌하지 않는다 — §7이 지우려는 것은
> 큐레이터가 아니라 그 주변의 중복 표면이다.

### 14.5 라이선스 계층 T0–T4 → ADR-0005 애드덤

ADR-0005에 이미 3계층이 암묵적으로 있었는데 **셋이 빠져 있었고 각각이 이미 결정 하나씩을 틀리게 했다**
— (a) 전이 폐포(§5의 2026-08-15 `pageindex`→AGPL `pymupdf` 정정), (b) 벤더링 계층(ADR-0021이 이미
실행했는데 문서에 없었다), (c) **아티팩트 계층**(B′를 비용 0으로 합법화하는 계층인데 한 번도 적힌 적이
없다). 다섯 계층의 정의와 현재 배정은 [ADR-0005 애드덤](adr/0005-fully-oss-bom.md)에 있다.

### 14.6 안 가져가는 것과 그 이유

| | 이유 |
|---|---|
| OpenViking 엔진 | AGPL → **T4 영구**. C++ 벡터 인덱스 + Rust ragfs, PyPI SDK는 라이선스 선언 자체가 없음 |
| Graphify 엔진 | 62,201줄 + 안정 스키마 부재. 그리고 **아고라 코퍼스에서 0개 노드** — `wiki/`는 100% 산문이라 DOC으로 분류돼 semantic 티어로 가고, API 키 없으면 하드 실패, `--code-only`면 모든 `.md`가 조용히 드롭된다 |
| OpenKB 동거 | 롤백이 `wiki/concepts`·`wiki/entities`를 디렉터리째 스냅샷한 뒤 백업에 없는 라이브 파일을 `unlink()` (§10) + 도메인 파티션 무복귀선 |
| **빌리는 것 (코드 아님, 계약)** — [#158] | Graphify `wiki.py`의 **슬러그 계약** — 비-ASCII 보존 · 링크≡디스크 파일명 · Windows MAX_PATH 예산 · 충돌 접미사 예약. 이슈 #136(이 문서 §2.2)이 터뜨린 바로 그 결함 클래스를 저쪽은 주석으로 방어해뒀다. **라이선스 무관, ADR 0개** |

### 14.7 §7 로드맵 재판정 — 뒤집힌 항목은 없고, 둘이 강화됐다

- **Phase 4 인증 삭제 판정 강화** — Graphify는 **인증 0으로 109k★에 도달했다.**
- **`ingest/extractors/` 확장 삭제 판정 강화** — "직접 만들지 마라"의 근거가 늘었다(T0 문서 피드).
- **ADR-0032(임베딩) 재개는 여전히 게이트 미충족** — §13의 실측이 그대로 유효하다.
- **§8 Day 4–5(`agora-harvest` 분리 출시)는 취소가 아니라 재범위화** — 하베스터 자체는 더 이상 유일하지
  않으므로(§14.1), 분리 출시의 축은 **레닥션 + 센티널**이다.

### 14.8 위키 구조 — 목표안 Stratum (**미비준 Draft**)

이 조사가 스키마에 남긴 진단 한 줄: 아고라의 천장은 **축이 뒤집혀 있다**는 사실이 정한다 — 경로가
*주제*를 지고(`wiki/<domain>/`), 닫힌 4값 `type:` enum이 *종류*를 진다. OpenKB에서 가져오는 것은
디렉터리 모양이 아니라 **원리 하나 — 디렉터리가 종류다**.

목표 레이아웃(요약): `wiki/{concepts,summaries,notes,maps,entities,people}/` + 도메인은 경로를 떠나
`subjects:` 프론트매터로 · `raw/`는 **옮기지 않는다** · 원본 바이트는 `raw/_blob/<ab>/<sha256>` 콘텐츠
주소로. 무복귀선은 **도메인 파티션 평탄화 하나뿐**이고, 그건 뒤집기 *전에* 워커가 `domain:`을
프론트매터에 물질화해서 닫는다.

**상태: 초안, 승인 전.** 판정문 자체가 관문 둘을 선행조건으로 건다 — **A** 무결성 경계 v2(바이트 우선
캡처가 `curator/worker.py:1591` `_is_engine_written_raw` 재작성을 강제하고, 유니코드 슬러거가
`curator/plan.py:92` `_SAFE_TOKEN_RE_PATTERN`(ASCII 전용 **보안** 통제)에 막힌다) [코드] · **B** n=24
하네스 재실행. 전문은 [`notes/stratum-target-architecture.md`](notes/stratum-target-architecture.md).

비준은 이슈 [#153]이 소유한다 — 관문 A [#154] · 관문 B [#155] · 무복귀선 닫기 [#156]. 셋 다
[#144]·[#146]·[#147]·[#152](레이아웃 독립 정직성 계약) 뒤에 온다.

### 14.9 미결 · 반증 조건 · 남은 부채

**반증 조건(하나라도 참이면 이 절을 재계산한다):** (i) OpenViking이 경계 레닥션을 넣으면 G2가 좁아진다 ·
(ii) Graphify가 `graph.json`에 `schema_version`과 안정 키 집합을 넣으면 T0→T1 재검토 대상이다 ·
(iii) OpenKB가 위키 콘텐츠에 히스토리/revert를 넣으면 §6의 해자 표를 다시 계산해야 한다.

**남은 문서 부채:** `docs/BOM.md`(현 의존성 전부의 전이 폐포 근거) **미작성** — [#157]. —
`docs/DESIGN.md` §9의 *"no mature OSS project combines…"* 차별화 문단은 §3 재발행으로 완전히 반증됐고,
이 절과 같은 커밋에서 **철회(RETRACTED)** 됐다 — 지우지 않고 취소선 + 철회 사유 + 대체 주장(custody)을
남겼다.

**포지셔닝 한 문장** — 이 절이 실제로 판 것:

> 아고라는 당신의 에이전트들이 배운 것에 대한 **system of record**다. 모든 사실은 append-only ·
> 레닥션 · 출처 도장이 찍힌 인박스로 들어오고, 위키를 편집하는 라이터는 언제나 하나뿐이며, 모든
> 변경은 diff하고 revert할 수 있는 git 커밋이다 — 당신이 어떤 컴파일러 · 그래프 · 인덱스를 가져다
> 붙이든.

이 문장이 **주장하지 않는 것**: "no vectors"(§13이 빌려온 정체성으로 이미 폐기) · "multi-tenant
teams"(`auth/__init__.py`는 2줄 docstring) · "최고의 검색". **custody(보관 책임)** 를 주장한다 —
넷 중 누구도 다투지 않는 유일한 절이다.

## 참조

- 2026-08-15 신규 이슈: [#144](https://github.com/handochan/agora-kb/issues/144) (쓰기 경로 랭커 결합) ·
  [#145](https://github.com/handochan/agora-kb/issues/145) (gold spec_hash) ·
  [#146](https://github.com/handochan/agora-kb/issues/146) (domain_focus) ·
  [#147](https://github.com/handochan/agora-kb/issues/147) (불변식 6 위반 2건) ·
  [#148](https://github.com/handochan/agora-kb/issues/148) (§8 루프 텔레메트리) ·
  [#149](https://github.com/handochan/agora-kb/issues/149) (읽기 면 미구현 계약 3건) ·
  [#150](https://github.com/handochan/agora-kb/issues/150) (검색 아키텍처 결정)
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
  [`notes/retrieval-vs-vectordb.md`](notes/retrieval-vs-vectordb.md) ·
  [`notes/openkb-compatible-long-document-compiler.md`](notes/openkb-compatible-long-document-compiler.md)
  (**Draft · non-normative**, PDF 호환 + PPTX 확장) ·
  [`notes/stratum-target-architecture.md`](notes/stratum-target-architecture.md)
  (**Draft · non-normative**, §14.8 목표 위키 구조) ·
  [ADR-0005 애드덤](adr/0005-fully-oss-bom.md) (라이선스 계층 T0–T4, §14.5)
- §14 조사 원본: 2026-08-21/22 세션 워크플로(OpenViking 16 에이전트 · Graphify 8 에이전트 + 적대적
  반증). 이 문서가 그 결론의 **저장소 측 정본**이다 — §9가 정산한 실패(결론만 휘발성 위치에 남기기)를
  반복하지 않기 위해.

[#139]: https://github.com/handochan/agora-kb/issues/139
[#140]: https://github.com/handochan/agora-kb/issues/140
[#144]: https://github.com/handochan/agora-kb/issues/144
[#145]: https://github.com/handochan/agora-kb/issues/145
[#146]: https://github.com/handochan/agora-kb/issues/146
[#147]: https://github.com/handochan/agora-kb/issues/147
[#148]: https://github.com/handochan/agora-kb/issues/148
[#149]: https://github.com/handochan/agora-kb/issues/149
[#150]: https://github.com/handochan/agora-kb/issues/150
[#152]: https://github.com/handochan/agora-kb/issues/152
[#153]: https://github.com/handochan/agora-kb/issues/153
[#154]: https://github.com/handochan/agora-kb/issues/154
[#155]: https://github.com/handochan/agora-kb/issues/155
[#156]: https://github.com/handochan/agora-kb/issues/156
[#157]: https://github.com/handochan/agora-kb/issues/157
[#158]: https://github.com/handochan/agora-kb/issues/158
