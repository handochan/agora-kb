# 설계 노트: OpenKB 호환 장문 serving 계약 — PDF 호환 + PPTX 확장

> **상태: Draft · Exploratory · Non-normative · NOT ratified.**
> **작성:** OpenAI Codex · 2026-08-17 · Agora 저장소 오너의 요청으로 작성
>
> 이 문서는 설계 초안이다. 어떤 결정을 확정하거나 기존 ADR을 supersede하지 않으며,
> 구현 승인도 의미하지 않는다. 아키텍처 SSOT는 [`DESIGN.md`](../DESIGN.md)이다. 구현 전
> load-bearing 결정은 별도 ADR로 승격해야 한다. 코드와 업스트림 인용은 작성 시점의 snapshot이며
> ADR 승격 전에 다시 검증한다.
> 아래에서 `MUST`에 준하는 표현으로 적은 계약도 모두 **채택 후보인 proposed contract**이며, Accepted ADR과
> 구현이 생기기 전에는 현재 Agora의 규범적 동작을 바꾸지 않는다.
>
> **제약/영향 관계(결정 아님):** [ADR-0001](../adr/0001-markdown-git-source-of-truth.md),
> [0002](../adr/0002-cqrs-single-writer-curator.md), [0005](../adr/0005-fully-oss-bom.md),
> [0006](../adr/0006-repo-as-tenant-boundary.md)의 제약을 받는다. 채택된다면
> [0004](../adr/0004-pluggable-adapters.md), [0010](../adr/0010-kb-wiki-schema.md),
> [0020](../adr/0020-web-upload-write-path.md)을 확장하고, query 측에서
> [0009](../adr/0009-deterministic-query-contract.md)/[0012](../adr/0012-deterministic-query-ranking.md),
> 운영 측에서 [0008](../adr/0008-transactional-sandboxed-curation.md),
> [0013](../adr/0013-curator-sandbox-mechanism.md),
> [0024](../adr/0024-bulk-processing-horizontal-curator-scale.md),
> [0025](../adr/0025-web-config-multiupload-extensions.md),
> [0027](../adr/0027-gold-context-packs.md)에 layers on 한다.

---

## TL;DR

Agora의 장문 처리는 기존 `Extractor`에 PageIndex를 끼워 넣는 기능이 아니라 다음의 독립 파이프라인이어야
한다.

```text
원본 capture
  → append-only binary inbox attachment
  → curator가 Git의 raw/에 원본 publish
  → 비동기 LongDocumentCompiler
  → 결정론적 artifact validator
  → curator가 PDF OpenKB-conformant / PPTX compatible-extension artifact를 publish
  → corpus 선택 → tree 탐색 → 제한된 page/slide 조회 → 근거 검증
```

이 Draft의 잠정 방향은 다음과 같다.

- **PDF:** OpenKB의 장문 artifact와 query 계약을 호환 목표로 삼는다.
- **PPTX:** 동일한 tree-navigation 철학과 artifact 경로를 따르는 Agora 확장이다. 현재 PageIndex가
  PPTX를 직접 지원한다고 주장하지 않는다.
- **원본:** 크기 제한 안에서는 Git의 `raw/`에 보존한다. PageIndex DB와 렌더링 중간물은 파생 cache다.
- **실행 목표:** audited permissive adapter/fork 또는 stable release를 확보한 뒤 local adapter를
  기본으로 삼는다. 외부 LLM/Cloud OCR은 KB별 명시적 opt-in이다.
- **쓰기:** compiler는 canonical repo를 직접 수정하지 않는다. repo별 단일 curator만 `wiki/`를
  publish한다.
- **검색:** PageIndex는 여러 문서 중 하나를 고르는 corpus retriever를 대체하지 않는다. 선택된 한 문서
  내부의 관련 page/slide를 찾는 두 번째 단계다.
- **Base/Domain:** repo가 hard tenant boundary다. Base는 OpenKB-compatible root, Domain은 같은 repo 안의
  선택적 namespace다. Domain query의 Base fallback은 명시적 ranking policy로만 허용하고 Base query는
  Domain을 자동으로 읽지 않는다.

---

## 0. 문서의 범위와 확인된 사실

### 0.1 이 Draft가 존재하는 이유

Agora를 OpenKB-compatible knowledge hub로 재정렬할 때 OpenKB의 강한 장문 탐색 경험을 유지하면서도
Agora의 다음 속성을 잃지 않기 위한 설계 초안이다.

- append-only, per-writer inbox
- CQRS + repo별 single-writer curator
- Markdown/Git 중심의 재구축 가능한 지식과 provenance
- hard tenant isolation
- local OSS path
- 정직한 `not_found`

이 문서는 PDF뿐 아니라 긴 PPTX deck도 page-like hierarchy로 탐색하고 slide 단위로 인용하려는 요구를
포함한다. `.ppt`, DOCX, XLSX, 영상, 오디오는 이 Draft의 초기 범위가 아니다.

### 0.2 현재 Agora의 gap

현재 [`ExtractedDoc`](../../src/agora_kb/ingest/extractors/base.py)은 Markdown 한 덩어리와 최소 provenance만
표현한다. PDF는 [`pdfminer` 평면 텍스트](../../src/agora_kb/ingest/extractors/pdf.py)로 바뀌며, PPTX도
[`MarkItDown` 평면 Markdown](../../src/agora_kb/ingest/extractors/office.py)으로 바뀐다. 원본 bytes,
page/slide 경계, tree, image artifact를 전달하는 계약은 없다.

원본 binary를 보존하지 않는 현재 동작은 [ADR-0020](../adr/0020-web-upload-write-path.md)과
[`LIMITATIONS.md` §6](../LIMITATIONS.md#6-an-uploads-original-bytes-are-not-kept)에 명시되어 있다.
따라서 기존 업로드에 대해 원본 없이 PageIndex를 사후 실행하는 것은 불가능하다.

현재 schema parser/linter와 query reader는 `wiki/**/*.md` 전체를 Agora v1 note로 보고, root `index.md`와
MOC child/domain 구조에도 현재 Agora 계약을 적용한다. OpenKB의 `wiki/summaries/*.md`와 `type: Summary`뿐
아니라 OpenKB식 concept/entity/index를 그대로 추가해도 현재 네 note type과 graph/lint 계약이 충돌한다.
따라서 이 설계의 완전한 OpenKB-native wiki projection은 **Schema v2 선행 결정**이다. Summary는 path-aware
전용 parser로 라우팅하되 corpus 검색에는 포함해야 하며, index/concept/entity shape도 함께 정의해야 한다.
또한 현재 curator만 `raw/`를 쓰는 [ADR-0010](../adr/0010-kb-wiki-schema.md)의 writer 계약도 binary
materialization을 허용하는 새 ADR에서 명시적으로 바뀌어야 한다.

현재 canonical raw 경로는 [`DATA-MODEL.md` §2](../DATA-MODEL.md)의
`raw/<domain>/<date>-<slug>.<ext>` 형태다. OpenKB의 평탄한 `raw/<doc_name>.<ext>`와 동일하지 않다.
현재 no-loss catch-all도 [ADR-0022](../adr/0022-curator-taxonomy-governance.md)에 따라 `domains[0]`으로
라우팅되며 flat Base scope는 아직 없다.
이 Draft의 **호환 범위는 long-document serving artifact와 read contract**이며 raw filename이나 `.openkb`
내부 DB까지 동일하게 만드는 것은 아니다. 향후 Base/Domain Schema v2가 raw 경로를 바꾸면 별도 migration
ADR이 필요하다.

### 0.3 OpenKB와 PageIndex에서 확인된 동작

OpenKB 고정 commit `ff54396`은 PDF page count가 configurable threshold(기본 20) 이상이면 원본을
`raw/`에 복사한 뒤 PageIndex 장문 경로로 보낸다
([converter.py](https://github.com/VectifyAI/OpenKB/blob/ff54396e575ee6feb0113b631a34caa082b441cc/openkb/converter.py#L135-L215)).
결과는 다음 두 serving artifact로 materialize된다
([indexer.py](https://github.com/VectifyAI/OpenKB/blob/ff54396e575ee6feb0113b631a34caa082b441cc/openkb/indexer.py#L125-L153)).

```text
wiki/sources/<doc_name>.json
wiki/summaries/<doc_name>.md
```

질의 시에는 PageIndex DB를 다시 검색하지 않는다. agent가 `index.md`, summary tree, concept/entity를 보고
문서를 고른 뒤 `get_page_content`로 제한된 page range만 local JSON에서 읽는다
([query.py](https://github.com/VectifyAI/OpenKB/blob/ff54396e575ee6feb0113b631a34caa082b441cc/openkb/agent/query.py#L22-L84)).
이 materialized tree + bounded source-read 계약이 Agora가 재사용할 핵심이다.

현재 PageIndex 공식 SDK는 **PDF만 입력으로 받는다**고 명시한다
([Document Processing SDK](https://docs.pageindex.ai/sdk/documents),
[current client](https://github.com/VectifyAI/PageIndex/blob/ae2a5b49b5411903633faa299201d6ba1769fd2f/pageindex/client.py#L107-L110)).
OpenKB도 장문 분기를 PDF에만 적용하고 PPTX는 MarkItDown을 통한 일반 Markdown source로 처리한다
([converter.py](https://github.com/VectifyAI/OpenKB/blob/ff54396e575ee6feb0113b631a34caa082b441cc/openkb/converter.py#L198-L247)).

현재 Agora의 `uv.lock`이 resolve한 MarkItDown 0.1.6의 PPTX converter는 slide marker, 제목/본문, 표,
지원되는 chart, image alt text, speaker notes를 단일 Markdown에 직렬화할 수 있다
([pinned source](https://github.com/microsoft/markitdown/blob/e144e0a2be95b34df17433bac904e635f2c5e551/packages/markitdown/src/markitdown/converters/_pptx_converter.py#L81-L200)).
image bytes는 `keep_data_uris=True`일 때만 포함되는데 현재 Agora 호출은 이를 설정하지 않는다. 따라서
현재 pipeline은 PPTX asset을 보존하지 않으며, 결과도 단일 Markdown 문자열이므로 slide-native artifact
계약이 추가로 필요하다.

---

## 1. 목표와 비목표

### 1.1 목표

1. 긴 PDF와 PPTX에서 관련 page/slide를 tree reasoning으로 찾는다.
2. 최종 답변이 원문 page/slide와 Git commit을 인용한다.
3. PDF에서는 OpenKB의 summary/source artifact와 read contract를 호환한다.
4. PPTX에서는 가능한 범위에서 slide, speaker notes, 표, chart, image를 format-native serving
   projection에 보존하고 unsupported/lossy region을 명시적으로 기록한다. raw PPTX만 lossless source
   evidence다.
5. PageIndex/renderer/model을 교체해도 원본에서 재색인할 수 있다.
6. compiler 실패가 일반 inbox와 curator를 장시간 막지 않게 한다.
7. PageIndex DB가 없어도 clone한 Git repo만으로 이미 publish된 장문을 질의할 수 있게 한다.
8. repo tenant 간 source/cache/credential/query 결과를 격리하고, 같은 repo의 Base/Domain scope도 명시적으로
   라우팅한다.
9. local OSS 경로를 항상 제공하고 cloud 전송을 명시적 정책으로 만든다.

### 1.2 비목표

- PageIndex를 Agora 전체 corpus 검색기로 교체하지 않는다.
- PageIndex SQLite/DocStore, rendered PDF, OCR scratch를 canonical knowledge로 만들지 않는다.
- OpenKB의 `.openkb` DB나 내부 hash registry와 byte-for-byte 호환하지 않는다.
- PPTX가 upstream PageIndex의 native input이라고 표현하지 않는다.
- query-time cache miss에서 즉석 재색인하거나 외부 LLM을 호출하지 않는다.
- compiler 또는 query navigator에 shell, Git write, wiki write, network credential 도구를 제공하지 않는다.
- 이 Draft에서 team PII의 hard erasure를 보장하지 않는다. Git history/remote/backup 삭제는 별도 retention
  ADR의 영역이다.

---

## 2. Draft 전제와 아직 확정되지 않은 부분

### 2.1 저장소 오너가 2026-08-17 검토에서 잠정 동의한 전제

아래 항목은 설계 진행을 위한 working assumption이며 ratify된 결정은 아니다.

1. **호환 범위:** Base PDF는 OpenKB의 Git-tracked serving artifact/page-read 계약, Domain과 PPTX는 그
   저수준 read shape를 재사용하는 Agora extension. `.openkb` 내부는 제외한다.
2. **원본 저장:** 초기에는 configurable size cap 아래 PDF/PPTX를 Git의 `raw/`에 보존한다.
3. **실행 기본값:** audited permissive adapter/fork 또는 stable release가 확보되는 것을 전제로 한 local
   adapter. 외부 LLM과 cloud OCR/vision은 KB별 opt-in이다.
4. **역할:** PageIndex는 한 문서 내부 탐색기다. corpus selection과 `not_found`는 Agora가 소유한다.
5. **형식 범위:** PDF와 PPTX를 초기 장문 형식으로 다룬다.

### 2.2 Draft일 뿐인 추천

- PDF는 OpenKB의 기본값과 맞춰 **20 physical pages**를 initial threshold로 삼되 user override와
  extracted-token/visual-complexity trigger를 함께 둔다.
- PPTX는 **20 slides를 실험 시작점**으로 삼되 slide 수만으로 장문 여부를 결정하지 않는다.
- PPTX 기본 profile은 source-native slide extraction으로 tree를 만드는 `pptx-native`다.
- 실제 PageIndex를 쓰는 `pptx-rendered-pageindex`는 검증된 `slide N ↔ rendered PDF page N` 매핑이 있을
  때만 활성화하는 실험 profile이다.
- speaker notes와 hidden slide의 기본 검색 정책은 §20의 열린 결정으로 남긴다.

---

## 3. 논리 아키텍처

```text
Web / MCP / CLI face
        │
        │ core binary-inbox API only
        ▼
append-only per-writer inbox attachment + event
        │
        ▼
repo owner / curator (Commit A)
  raw original + sidecar + source state
        │
        ├── ordinary short-document path
        │
        └── long-document job
                │
                ▼
      tenant-isolated compiler worker
      PDF PageIndex | PPTX native | PPTX rendered PageIndex
                │
                │ normalized artifact bundle only
                ▼
        deterministic validator
                │
                ▼
repo owner / curator (Commit B, CAS)
  source JSON + summary tree + generation manifest
  + Schema v2 ratification 이후에만 index/concepts/entities
                │
                ▼
authorized query
  corpus selector → tree navigator → bounded unit reader → evidence verifier
```

규칙은 다음과 같다.

- face는 `raw/`, `wiki/`, job directory를 임의로 쓰지 않는다.
- binary attachment도 core의 append-only API를 통해서만 생성한다.
- compiler는 canonical working tree를 직접 수정하지 않는다.
- compiler의 input은 read-only source snapshot이고 output은 isolated scratch의 artifact bundle이다.
- 여러 compiler worker가 서로 다른 repo/document를 처리할 수 있지만, 한 repo를 publish하는 주체는 항상
  하나다.
- publish 전에 source version과 tombstone epoch를 다시 확인해 늦게 끝난 worker가 삭제/업데이트된
  문서를 되살리지 못하게 한다.
- PR review deployment에서는 Commit A와 Commit B가 각각 review/merge된 뒤에만 다음 단계와 serving이
  진행된다. compiler와 query는 미병합 branch가 아니라 curated ref만 읽는다.

---

## 4. Identity와 format-neutral locator

### 4.1 Source identity, build request, 산출물을 분리한다

```text
source_id          논리 문서의 안정적인 Agora 식별자
source_version     원본 bytes의 SHA-256
build_request_fingerprint
                   source_version + compiler/profile/model/config/render-policy digest
attempt_id         같은 build request의 개별 실행 식별자
artifact_digest    validator가 승인한 normalized artifact bundle의 content digest
index_generation   publish된 artifact를 가리키는 안정적인 generation 식별자
```

PageIndex가 반환하는 `doc_id`는 adapter 내부 식별자일 뿐 Agora public/canonical identity가 아니다.
`source_id`는 `repo_uuid + knowledge_scope`에 묶는다. 동일 bytes라도 다른 repo 또는 Base/Domain scope라면
identity와 access decision을 자동 공유하지 않는다. tenant 간 cache 공유는 금지하고, 같은 repo scope 간
content reuse는 별도 dedup/provenance 정책이 있을 때만 허용한다.

LLM/renderer/compiler가 비결정적일 수 있으므로 같은 `build_request_fingerprint`가 항상 같은
`artifact_digest`를 만든다고 가정하지 않는다. 재시도는 새 `attempt_id`를 갖고, publish idempotency와
승인된 결과 선택은 `artifact_digest`를 기준으로 한다. job request에는 최소한 다음이 들어간다.

```text
repo_uuid
+ knowledge_scope
+ source_id
+ source_version
+ compiler name/version
+ profile/config/model digest
+ build_request_fingerprint
+ attempt_id
```

validator 승인 후 publisher가 `artifact_digest`에 결합된 `index_generation`을 발급하고 같은 값으로
summary/source/image/manifest를 묶는다. 따라서 attempt 번호나 build request만으로 generation을 가장하지 않는다.

### 4.2 내부 모델은 `page`가 아니라 typed locator를 사용한다

```yaml
locator:
  kind: page | slide
  start: 12
  end: 15
```

- `start`/`end`는 1-based, inclusive다.
- PDF는 파일 내 physical page ordinal을 사용한다.
- PPTX는 presentation의 실제 slide order ordinal을 사용한다. `slide17.xml` 같은 package filename은
  address가 아니다.
- 인쇄된 page label이나 slide label은 표시용일 뿐 citation address가 아니다.
- 한 문서/세대 안에서 locator kind를 섞지 않는다.
- 모든 tree node의 range는 `1..unit_count` 안에 있어야 하고 child range는 parent range를 벗어나지 않는다.

최소 citation은 다음을 포함한다.

```text
repo_id · knowledge_scope · source_id · source_version · git_commit
locator(kind/start/end) · node_id · excerpt 또는 asset_id
```

speaker note, chart, image에서 얻은 근거는 visible slide body와 구분되는 evidence region을 함께 표시한다.

---

## 5. Canonical, serving projection, derived state

### 5.1 Git에 추적되는 근거와 serving artifact

```text
Base scope (OpenKB-compatible artifact root):
  raw/<date>-<safe-slug>-<source-sha8>.pdf|pptx
  raw/<date>-<safe-slug>-<source-sha8>.meta.yaml
  wiki/sources/<doc_name>.json
  wiki/sources/images/<doc_name>/<index_generation>/*
  wiki/summaries/<doc_name>.md
  wiki/sources/_manifests/<source_id>.yaml

Optional Domain scope (Agora namespace extension):
  raw/<domain>/<date>-<safe-slug>-<source-sha8>.pdf|pptx
  raw/<domain>/<date>-<safe-slug>-<source-sha8>.meta.yaml
  wiki/<domain>/sources/<doc_name>.json
  wiki/<domain>/sources/images/<doc_name>/<index_generation>/*
  wiki/<domain>/summaries/<doc_name>.md
  wiki/<domain>/sources/_manifests/<source_id>.yaml
```

- `raw/` 원본과 sidecar가 재처리·감사의 source evidence다.
- `wiki/sources/*.json`은 특정 generation의 page/slide별 read projection이다.
- `wiki/summaries/*.md`는 tree navigation projection이다.
- 두 projection은 Git에 commit되고 clone/offline query가 가능하지만 원본을 대체하는 별도의 진실은 아니다.
- 변경된 bytes는 기존 raw file을 덮어쓰지 않고 content-addressed/versioned path에 새로 쓴다. Git history만
  보존 계약의 대체물로 보지 않는다.
- immutable raw sidecar는 그 capture의 provenance를 기록한다. active serving과 pending build request처럼
  변하는 durable 상태는 Git-tracked source manifest에 둔다. clone 시 사라질 수 있는 `_kb/`에만 두지 않는다.
- image는 generation별 path에 publish한다. manifest가 새 artifact set으로 전환되는 commit에서 current
  summary/source/image set을 함께 바꾸며, 이전 commit은 이전 세대를 계속 재현한다.

source manifest의 최소 shape:

```yaml
source_id: src_...
doc_name: example-a1b2c3d4
knowledge_scope: base
source_state: active
active:
  source_version: "<sha256>"
  raw: raw/2026-08-17-example-deadbeef.pptx
  build_request_fingerprint: "<sha256>"
  generation: gen_...
  artifact_digest: "<sha256>"
  artifacts:
    summary: wiki/summaries/example-a1b2c3d4.md
    units: wiki/sources/example-a1b2c3d4.json
    images: wiki/sources/images/example-a1b2c3d4/gen_...
  published_projection_checksums: {}
pending:
  source_version: "<sha256>"
  raw: raw/2026-08-18-example-feedface.pptx
  build_request_fingerprint: "<sha256>"
  request_state: requested
  retry:
    attempts_used: 0
    max_attempts: 3 # illustrative; profile policy가 결정
    last_failure: null
    next_retry_at: null
tombstone_epoch: 0
```

`active`는 현재 curated commit에서 serving되는 한 세트를, `pending`은 다음 build의 desired state와 immutable
raw를 가리킨다. `pending.request_state`는 `requested | needs_operator | terminal_failed` 같은 durable
disposition과 bounded retry receipt를 기록한다. `last_failure`에는 비밀/원문 provider response가 아닌
분류된 reason code와 attempt id만 둔다. transient `compiling`/`validating` 상태는 매번 Git에 commit하지
않고 `_kb/longdoc/jobs`에 두며, 손실되면 retry receipt의 남은 budget과 `next_retry_at`을 지켜 `requested`에서
다시 `queued` job을 만든다. `stale`은 active와
pending의 `build_request_fingerprint`를 비교해 계산한다. 서로 다른 타입인 build fingerprint와 artifact
digest를 비교하지 않는다.

`forget` 후에도 늦은 worker를 차단하고 삭제 사실을 재구축할 수 있도록 최소 tombstone manifest는 남긴다.
`doc_name`은 표시 filename이 아니라 safe slug + `source_id` hash suffix로 만들며 Unicode normalization,
case-folding, reserved-name 정책을 고정한다. 같은 filename/title 또는 대소문자만 다른 문서도 충돌하면 안 된다.

위 Base flat raw path는 현재 `raw/<domain>/...` 계약을 바꾸는 **제안**이므로 Schema/migration ADR 없이는
사용하지 않는다. `<artifact_root>`를 Base는 `wiki/`, Domain은 `wiki/<domain>/`으로 resolve하고 그 아래의
`sources`/`summaries` 계약을 동일하게 적용한다. OpenKB conformance claim은 Base artifact root에만 적용하며
Domain nesting은 Agora extension이다.

### 5.2 삭제 후 재구축할 수 있는 operational state

```text
_kb/longdoc/jobs/*
_kb/longdoc/artifacts/<generation>/*
_kb/pageindex/*
<isolated scratch>/rendered.pdf
<isolated scratch>/ocr/*
```

PageIndex DB/DocStore, generated PDF, OCR page image, retry state는 canonical이 아니다. `_kb/`가 사라져도
Git의 raw/sidecar/source manifest/summary를 스캔해 ready state를 복원하고 필요하면 명시적으로 reindex할 수
있어야 한다.

---

## 6. OpenKB read-compatible serving contract — PDF conformance, PPTX extension

Base PDF는 OpenKB artifact-level conformance 목표이며 canonical summary의 `doc_type`은 `pageindex`다.
Domain PDF는 같은 shape의 nested Agora extension이다. PPTX는 Agora canonical summary에서
`doc_type: slideindex`를 사용하고, OpenKB `get_wiki_page_content`의
`page`/`content`/`images` 저수준 read shape를 재사용하는 Agora extension이다. unmodified OpenKB의
end-to-end query prompt, lint, recompile, remove, slide citation semantics까지 호환한다고 주장하지 않는다.

### 6.1 Summary Markdown

PDF는 OpenKB의 필수 frontmatter를 유지하고 Agora metadata를 additive field로 붙인다. 아래는 PPTX canonical
extension 예시다.

```yaml
---
type: Summary
description: 문서 전체의 짧은 설명
doc_type: slideindex
full_text: sources/example-a1b2c3d4.json
source_format: pptx
locator_kind: slide
raw_ref: raw/2026-08-17-example-deadbeef.pptx
source_sha256: "..."
index_generation: "..."
compiler_profile: pptx-native
---
```

PDF summary body:

```markdown
# Architecture (pages 12–27)

Summary: ...
```

PPTX summary body:

```markdown
# Architecture (slides 12–27)

Summary: ...
```

PPTX의 `slideindex`, `source_format`/`locator_kind`, `slides` label은 Agora 확장이다. `full_text`와 source
JSON의 numeric `page` alias 덕분에 legacy 저수준 page reader는 사용할 수 있지만, unmodified OpenKB의
`doc_type: pageindex` 분기까지 그대로 통과한다는 뜻은 아니다. 필요하면 별도 **wire-compat export view**가
PPTX를 `doc_type: pageindex`와 `pages` label로 투영할 수 있으나, 이는 slide/notes 의미를 낮춘 비canonical
export이며 Agora query/lint/recompile 계약이 아니다.

`full_text`는 항상 해당 `<artifact_root>` 기준 `sources/<doc_name>.json`을 가리킨다. 따라서 Base summary는
OpenKB root에 놓이고, Domain summary는 Agora reader가 `wiki/<domain>/` root 안에서 resolve한다.

### 6.2 Source JSON

OpenKB reader 호환을 위해 각 unit에 `page` integer와 `content`/`images`를 유지한다. PPTX에서는 `page`가
slide ordinal의 compatibility alias이며 Agora는 typed locator를 우선 사용한다.

```json
[
  {
    "page": 17,
    "content": "# Architecture\n...",
    "images": [
      {
        "path": "sources/images/example-a1b2c3d4/gen_abc123/slide-17-chart-1.png"
      }
    ],
    "locator": {
      "kind": "slide",
      "number": 17
    },
    "title": "Architecture",
    "hidden": false,
    "regions": {
      "body": "...",
      "speaker_notes": "..."
    }
  }
]
```

OpenKB 호환 tool은 `page`/`content`/`images`만 사용하고 추가 field를 무시할 수 있다. speaker notes는
기본적으로 visible body와 분리한다. policy가 **명시적으로 serving을 허용한 notes만** export view의
`content`에 합칠 수 있으며, 제외 정책인 notes를 legacy reader 노출을 위해 합치면 안 된다. 정책에 따라
`regions`에도 넣지 않고 serving JSON에서 완전히 생략할 수 있다. Agora reader는 허용된 `locator`,
`regions`, asset metadata를 사용해 더 정확한 citation을 만든다.

### 6.3 Schema v2 parser/linter 요구

현재 `wiki/**/*.md`를 모두 v1 note로 보는 parser를 그대로 두면 `type: Summary`가 lint를 깨뜨리고,
OpenKB식 root index/concept/entity도 현재 Agora graph/schema와 충돌한다. Schema v2는 path-aware dispatch뿐
아니라 OpenKB-native wiki artifact 전체의 type/path/link 규칙을 추가해야 한다.

```text
wiki/summaries/**/*.md + wiki/<domain>/summaries/**/*.md
  → LongDocumentSummary parser + dedicated lint
wiki/sources/**/*.json + wiki/<domain>/sources/**/*.json
  → LongDocumentSource parser + dedicated lint
wiki/sources/_manifests/*.yaml + wiki/<domain>/sources/_manifests/*.yaml
  → LongDocumentSourceManifest parser + dedicated lint
wiki/concepts/**/*.md + optional domain-scoped equivalent
  → Schema-v2 concept parser (ratified 이후)
wiki/entities/**/*.md + optional domain-scoped equivalent
  → Schema-v2 entity parser (ratified 이후)
index.md
  → Schema-v2 root index parser (ratified 이후)
나머지 wiki/**/*.md
  → ordinary Agora note parser
```

summary는 일반 MOC/theme required-field 규칙이나 basename graph 규칙을 받지 않되 corpus query index에는
포함한다. dedicated lint는 §10의 range, path, generation, checksum 규칙을 검증한다.

최소 `LongDocument Artifact Schema`가 ratify되어 Summary/source/image/manifest dispatch와 lint가 생기기
전에는 **어떤 Commit B artifact도 current v1 repo에 publish하지 않는다.** 이후 full OpenKB-native Schema v2가
root `index.md`, concept/entity shape와 migration을 ratify한 경우에만 그 파일들을 함께 갱신한다. 두 gate를
하나의 구현 단계로 승인할 수도 있지만, minimal artifact schema 없이 먼저 산출물을 쓰는 경로는 없다.

---

## 7. Binary inbox와 두 단계 publish

### 7.1 Binary attachment가 먼저다

초기 write contract의 개념적 형태:

```yaml
kind: asset-capture
asset_ref: <per-writer append-only attachment ref>
asset_sha256: "..."
filename: example.pptx
mime: application/vnd.openxmlformats-officedocument.presentationml.presentation
bytes: 1234567
repo_uuid: <server-resolved tenant repo>
knowledge_scope: base | domain:<normalized-domain-id>
```

core는 완성된 content-addressed blob을 atomic하게 쓴 뒤 그 ref를 가진 inbox event를 공개한다.
사용자는 upload 전에 tenant repo와 Base 또는 특정 Domain scope를 선택한다. face는 authn/authz를 통과한
tenant를 서버측 `repo_uuid`로 resolve하고, domain id를 그 repo의 taxonomy/ACL에 대해 검증한 뒤 normalized
`knowledge_scope`로 고정한다. inbox event 안에서 다른 repo로 재라우팅하거나 임의 domain string을 다시
해석하지 않는다.

- blob만 남고 event 공개 전에 crash: 안전한 orphan이며 보존 후 GC 대상이다.
- event가 보이면 attachment는 완성되어 있어야 한다.
- attachment namespace는 writer와 repo에 묶이며 tenant 간 hash dedup을 하지 않는다.

이 과정은 [ADR-0020](../adr/0020-web-upload-write-path.md)의 reserved binary staging을 승격하는 별도
결정이 필요하다.

### 7.2 Commit A — source acceptance

curator의 deterministic engine이 다음을 한 commit으로 publish한다.

- 원본을 `raw/`에 materialize
- source sidecar에 filename/MIME/source SHA/provenance 기록
- logical source를 active로 기록
- source manifest에 새 pending build request와 raw pointer를 `requested`로 기록

brain은 `raw/`를 쓸 수 없다. 현재 allowlist의 안전 속성을 유지하면서 deterministic orchestrator만 binary를
publish해야 한다.

direct-publish mode에서 Commit A가 성공하면 원본 capture inbox event는 finalize한다. 이후 처리는 Git
manifest에서 재생성 가능한 별도의 durable long-document job이 담당한다. 긴 compile이 끝날 때까지 원본
event를 미처리 상태로 붙잡지 않는다.

### 7.3 외부 indexing

compiler worker는 read-only raw snapshot과 isolated scratch만 사용한다. 완료되면 normalized artifact bundle과
manifest/checksum을 제출한다. 장기 LLM/OCR 작업 중 repo lock을 잡지 않는다.

### 7.4 Commit B — indexed publication

최소 LongDocument Artifact Schema ratification이 선행조건이다. curator는 source version/tombstone을 다시
확인하고 artifact를 검증한 뒤 다음을 한 CAS commit으로 publish한다.

- `wiki/sources/<doc>.json`
- `wiki/sources/images/<doc>/<generation>/...`
- `wiki/summaries/<doc>.md`
- `wiki/sources/_manifests/<source_id>.yaml`
- full OpenKB-native Schema v2가 ratify된 경우에만 document entry와 concept/entity 업데이트
- manifest의 `active.generation`/`active.artifact_digest` 전환과 `pending` 정리

중간 상태를 query가 읽지 않는다. pending build/candidate를 준비하는 동안 기존 ready generation이 있으면 계속
serving한다.

PR review mode에서는 Commit A proposal을 만들 때 `_kb/longdoc/awaiting_review/<proposal-id>`에 review
ledger를 남기고 immutable inbox event/attachment는 보존한다. event는 processing queue에서는 분리하지만
merge 전 finalize하지 않는다. ledger 손실 시 unfinalized event와 open PR을 대조해 복구한다.

- raw/source PR merge: Commit A가 curated ref에 보인 뒤 event finalize + build enqueue
- PR close/reject: `preserved_for_requeue` 또는 terminal `rejected` disposition을 기록하고, attachment GC는
  retention 이후에만 수행
- artifact PR: validated candidate를 review하며 merge commit에서만 active generation 발급/전환

compiler는 Commit A merge 뒤에만 실행한다. PR branch나 미병합 artifact를 preview가 아닌 일반 query에
노출하지 않는다. source proposal의 `awaiting_source_review`와 candidate artifact의 `awaiting_review`는 서로
다른 상태다.

---

## 8. 상태 모델과 recovery

source proposal, logical source, build job, candidate artifact, published generation의 상태를 분리한다.

```text
Source proposal (PR mode, operational):
  awaiting_source_review → merged | rejected | preserved_for_requeue

Logical source (Git manifest):
  active → tombstoned

Build job (operational, rebuildable from pending request):
  queued → compiling → validating → validated
            ├→ needs_ocr
            ├→ failed
            └→ cancelled

Candidate artifact:
  validated → awaiting_review → published | rejected

Published generation:
  ready → superseded
```

`awaiting_source_review`과 `awaiting_review`은 PR mode에서만 사용하며 direct-publish mode에서는 각각 Commit A,
validation 뒤에 바로 CAS publish한다. `index_generation`은 publish 때 처음 발급되므로 queued/compiling 상태를
generation 상태라고 부르지 않는다. 부분 성공은 별도 `degraded` state가 아니라 `ready` + structured
warnings다. `indexing`, `stale`, `not_ready`는 저장 상태가 아니라 active/pending request와 job state에서
계산하는 UI/API view다.

복구 규칙:

- **attachment 후 event 전 crash:** orphan inventory에서 보존/GC한다.
- **source PR 대기 중 state loss:** unfinalized inbox event/attachment와 open/closed proposal을 대조해
  `awaiting_source_review` ledger 및 disposition을 복원한다.
- **Commit A 후 enqueue 전 crash:** Git-tracked source state를 스캔해 missing job을 재생성한다.
- **compiler 완료 후 publish 전 crash:** 동일 `artifact_digest`로 검증된 staging bundle을 재사용한다. 같은 build
  request를 다시 실행했다는 이유만으로 다른 digest의 결과를 자동 선택하지 않는다.
- **CAS conflict:** 비싼 PageIndex를 다시 실행하지 않고 새 branch tip에서 source/tombstone 검증과 publish만
  재시도한다.
- **동일 source update:** 새 source version/build request를 만들고 publish 전까지 이전 ready generation을 유지한다.
- **처리 중 delete:** tombstone epoch 불일치로 늦은 worker 결과를 폐기한다.
- **operational state loss:** Git의 raw/sidecar/source manifest/summary에서 ready inventory를 복원한다.

동일 bytes + 동일 `build_request_fingerprint`에 이미 **ready로 선택된 artifact**가 있을 때만 compile을 no-op하고
새 provenance를 union할 수 있다. ready artifact가 없으면 새 `attempt_id`로 실행하며 비결정적 결과를 기존
generation과 같다고 간주하지 않는다. filename만으로 동일 문서를 판정하지 않는다. 내용이 바뀌면
Base는 `raw/<date>-<safe-slug>-<sha8>.<ext>`, Domain은
`raw/<domain>/<date>-<safe-slug>-<sha8>.<ext>` 같은 새 content-addressed/versioned path를 사용한다. Git
history만으로 덮어쓰기를 정당화하지 않는다.

retry는 failure class별 bounded policy다. transient parser/model/provider failure만 configured max attempt와
exponential backoff + jitter 안에서 새 `attempt_id`로 재시도한다. build fingerprint는 입력/config가 같으면
유지한다. `needs_ocr`은 operator가 OCR profile/consent를 선택하기 전 terminal wait이고, schema/security/
resource-limit failure는 source 또는 config가 바뀌기 전 자동 재시도하지 않는다. CAS publish 재시도는 검증된
동일 artifact를 재사용하므로 compile attempt budget을 소비하지 않는다. max attempts를 소진하면 reason과
last attempt를 durable terminal receipt/manifest disposition에 남기고 `failed`가 된다. `_kb` 손실이 attempt
budget을 초기화해서는 안 된다.

---

## 9. Format pipeline

### 9.1 PDF — OpenKB/PageIndex profile

```text
raw PDF
  → sandboxed preflight(page count/encryption/blank/scanned/resource estimate)
  → PageIndex adapter(tree + summaries)
  → permissive page text/image extractor
  → normalized page units
  → tree/page range validator
  → OpenKB-compatible summary + sources JSON
```

초기 routing은 다음 중 하나라도 만족하면 long-document candidate로 본다.

- `force_long_document=true`
- physical page count가 configured threshold 이상(초기 compatibility default 20)
- direct-curation token budget 초과
- 표/이미지/heading density 등 complexity policy 초과

scanned/image-only PDF를 text-empty success로 처리하지 않는다. local OCR profile이 없으면 `needs_ocr`로
표시하며 cloud/VLM으로 조용히 전환하지 않는다.

### 9.2 PPTX — `pptx-native` profile

PPTX는 slide 자체가 이미 자연스러운 source unit이다. 기본 추천 pipeline은 OOXML/native extraction이다.

```text
raw PPTX
  → zip/XML/resource preflight
  → presentation order의 slide units 추출
  → title/body/table/chart/image/notes/hidden metadata 추출
  → extract 가능한 section metadata + titles + adjacency로 hierarchy compile
  → slide-range tree validator
  → OpenKB-path-compatible summary + sources JSON
```

이 hierarchy compiler는 Agora가 새로 제안하는 component다. MarkItDown/python-pptx만으로 section-aware
tree가 제공되는 것은 아니다. compiler의 목표는 PageIndex와 같다: vector chunk가 아니라 문서의 논리적
hierarchy를 만들어 reasoning navigator가 section을 고르게 한다. engine name/profile은 `pptx-native`로
기록하여 upstream PageIndex를 실행한 것처럼 보이게 하지 않는다.

각 slide unit에는 최소한 다음을 보존한다.

```yaml
index: 17
label: "17"
hidden: false
title: Architecture
body_markdown: "..."
speaker_notes_markdown: "..." # policy에 따라 serving 포함/제외
assets:
  - asset_id: chart-1
    role: chart
    path: sources/images/example-a1b2c3d4/gen_abc123/slide-17-chart-1.png
    sha256: "..."
    mime: image/png
```

native extraction이 놓치거나 저하시킬 수 있는 항목은 warning으로 남긴다.

- unsupported chart/SmartArt
- grouped/overlapping shape의 읽기 순서
- animation/build sequence
- embedded video/OLE
- remote relationship/font
- image-only slide의 의미

### 9.3 PPTX — `pptx-rendered-pageindex` experimental profile

실제 PageIndex tree를 원하는 경우 선택적 dual-track을 사용한다.

```text
Track A: PPTX native extraction → source-native slide serving projection
Track B: sandboxed PPTX→PDF render → PageIndex tree
                              ↓
            verified PDF page ↔ PPTX slide mapping
                              ↓
            PageIndex ranges를 slide ranges로 변환
                              ↓
            native slide content로 node summary/evidence 보강
```

candidate renderer는 local external executable인 LibreOffice headless다
([CLI parameters](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html),
[conversion filters](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)).
LibreOffice는 core Python dependency가 아니며 renderer adapter 뒤에 둔다. renderer executable/version,
export filter, font bundle fingerprint, rendered-PDF SHA를 generation manifest에 기록한다. LibreOffice
배포물은 MPL 2.0 조건이며 포함 component의 license는 version별로 다양하므로 실제 배포물의 third-party
notices까지 BOM 검토해야 한다
([LibreOffice licenses](https://www.libreoffice.org/licenses/)).

다음 조건을 모두 만족해야 PageIndex range를 slide citation으로 사용할 수 있다.

1. renderer adapter가 per-slide export 또는 동등한 explicit mapping manifest를 생성한다.
2. native slide의 title/unique text fingerprint 또는 per-slide render checksum으로
   `slide ordinal N ↔ PDF physical page N`을 독립 검증한다.
3. included slide count와 rendered PDF page count가 같다. 이는 필요조건이지 mapping proof는 아니다.
4. hidden-slide 포함 정책이 두 track에서 동일하다.
5. tree node ranges가 slide count 안에 있다.
6. render failure/skipped slide/password/repair는 mapping failure다. font substitution/layout 차이는 renderer가
   경고하지 않을 수도 있으므로 별도의 fidelity warning과 visual benchmark로 관리한다.

매핑이 실패하면 PageIndex 결과를 slide 근거라고 조용히 publish하지 않는다. `pptx-native`로 명시적 fallback
하거나 generation을 `failed`로 표시한다. 구조적으로 유효하지만 fidelity loss가 있는 결과는 `ready`와
structured warnings로 표현하며 별도 `degraded` 상태를 만들지 않는다.

rendered PDF는 원본이 아니며 Git canonical artifact로 commit하지 않는다. speaker notes, hidden slide,
animation, OLE, font/layout 의미는 렌더링에서 사라지거나 달라질 수 있으므로 native track이 항상 근거의
기준이다.

### 9.4 PageIndex Markdown mode를 PPTX 기본값으로 사용하지 않는 이유

MarkItDown output을 PageIndex Markdown mode에 그대로 넣으면 heading/line-number tree를 만들 수 있지만
slide range address가 보존되지 않고 모든 slide title의 heading level이 같을 수 있다. slide-native locator와
visual mapping을 잃으므로 initial default로 채택하지 않는다.

---

## 10. LongDocumentCompiler contract와 validator

### 10.1 Adapter contract

```text
LongDocumentCompiler.compile(
  source_snapshot,
  source_metadata,
  compiler_profile,
  budget,
  output_directory
) -> LongDocumentArtifact
```

normalized manifest 예시:

```json
{
  "artifact_version": 1,
  "source_id": "...",
  "source_sha256": "...",
  "build_request_fingerprint": "...",
  "attempt_id": "...",
  "source_format": "pptx",
  "locator_kind": "slide",
  "unit_count": 87,
  "compiler": {
    "name": "pptx-native",
    "version": "...",
    "model": "...",
    "config_sha256": "..."
  },
  "tree_file": "tree.json",
  "units_file": "units.json",
  "images": [],
  "checksums": {},
  "warnings": []
}
```

adapter는 PageIndex 고유 객체나 ambient filesystem path를 canonical publisher에게 노출하지 않는다.
compiler는 `artifact_digest`나 `index_generation`을 스스로 선언하지 않는다. validator/publisher가 tree,
units, asset bytes/checksums와 canonical compiler/source metadata를 canonical serialization한 뒤 digest를
계산한다. `attempt_id`, digest field 자신, staging path, publisher-assigned generation은 hash 대상에서 제외한다.

### 10.2 Publish 전 결정론적 검증

- source SHA가 requested raw snapshot과 일치한다.
- build request/attempt identity가 요청과 일치한다.
- unit ordinal은 1부터 연속적이며 duplicate가 없다.
- locator kind는 문서 전체에서 하나다.
- 모든 tree range가 `1..unit_count`이고 `start <= end`다.
- child range가 parent range를 벗어나지 않는다.
- node id가 unique하고 tree depth/node count 제한 안이다.
- 모든 output path가 staging root 내부이며 absolute path, `..`, symlink를 거부한다.
- image MIME, extension, count, bytes, decoded pixels를 제한한다.
- publish 가능한 asset은 안전하게 재인코딩된 raster allowlist로 제한한다. SVG, HTML, active content 및
  원본 embedded object를 web origin에서 직접 serve하지 않는다.
- page/slide별 및 전체 text byte/token을 제한한다.
- source JSON은 UTF-8이며 schema를 만족한다.
- `full_text`가 정확한 source JSON을 가리킨다.
- summary/source/images가 같은 source version과 generation을 기록한다.
- compiler/model/config/checksum/warning을 기록한다.
- PPTX rendered profile은 full slide↔page mapping proof를 가진다.
- source가 active이며 publish 직전 tombstone/update epoch가 변하지 않았다.
- final diff는 허용된 long-document artifact만 수정한다.

PageIndex/LLM output은 untrusted data다. validator를 통과했다고 문서 내용의 사실성이 검증되는 것은 아니며,
오직 artifact 구조와 provenance가 publish 가능한 상태임을 의미한다. 검증 후 계산한 `artifact_digest`에
결합해 publisher가 `index_generation`을 발급한다. generation을 삽입해 렌더한 최종 Summary/source JSON/image
set만 `published_projection_checksums`로 검증한다. manifest 자신은 그 checksum 집합에 넣지 않고 schema와
Git blob/commit integrity로 검증하여 다시 self-reference를 만들지 않는다.

---

## 11. Query contract

```text
authorized Base/Domain corpus selection
  → relevant document selection
  → selected document의 summary tree navigation
  → typed locator selection
  → bounded original-unit read
  → evidence verification
  → answer 또는 not_found
```

이 흐름에서 `kb_document_tree`와 `kb_document_read`는 commit-addressed artifact에 대한 **결정론적 read
primitive**다. LLM tree navigator와 answer synthesis는 그 위의 선택적 adapter layer이며, 기존 `kb_query`의
deterministic ranking/not-found 계약을 바꾸지 않는다.

권장 read primitive:

```text
kb_document_tree(source_id)
kb_document_read(source_id, locator={kind, start, end})
```

OpenKB 호환 `get_page_content(doc_name, pages)`/`kb_document_pages`는 PDF alias 및 PPTX numeric compatibility
wrapper로 제공할 수 있다. Agora의 canonical 내부 API는 typed locator를 사용한다.

필수 제한:

- 호출당 document/node/unit 수 상한
- unit range와 return bytes/token 상한
- authorization 이후에만 source_id/path resolve
- read 시작 시 curated commit을 한 번 resolve하고, summary/source/images/manifest를 그 Git tree object에서
  읽거나 동등한 atomic read snapshot/root를 사용한다. mutable working-tree path를 파일별로 따로 읽지 않는다.
- 모든 result에 repo/knowledge-scope/source/version/generation/locator/commit 포함
- speaker-note evidence임을 citation에서 명시
- image/chart evidence는 asset id와 extraction method를 명시

summary/tree는 evidence가 아니다. 실제 page/slide content가 claim을 지지해야 한다. 일반 corpus query에서
관련 문서나 근거가 없으면 `not_found`를 반환한다. 반면 사용자가 방금 업로드한 특정 `source_id`를 조회했는데
active generation이 없으면 `not_ready`와 `indexing`/`needs_ocr`/`failed` reason을 반환하며 이를
`not_found`로 위장하지 않는다. tree navigator는 후보 range를 제안할 뿐 기존 deterministic query floor를
제거하지 않는다.

---

## 12. Base KB, Domain KB, tenant isolation

- **Hard tenant boundary는 repo다.** Base와 Domain은 기본적으로 같은 repo 안의 knowledge scope이며 Domain
  path/ACL만으로 tenant isolation을 주장하지 않는다.
- Base는 `wiki/sources`/`wiki/summaries`의 기본 OpenKB-compatible corpus다. 사용자가 특정 분야를 별도
  구축할 때만 `wiki/<domain>/sources`/`summaries`를 활성화한다. 업로드 때 scope를 정하지 않으면 Base로 간다.
- Domain query는 선택한 Domain만 검색하고, 명시적으로 ratify된 fallback/ranking policy가 있을 때만 같은
  repo의 Base candidates를 합성한다. Base query는 Domain을 자동 검색하지 않으며 다른 Domain으로 fan-out하지
  않는다.
- 미래 per-domain ACL이 있으면 corpus selection 전에 적용하지만, 강한 비밀 격리가 필요하면 해당 Domain을
  별도 repo로 배치한다. 그 경우 Base와의 합성은 auth/federation/cross-repo ranking ADR 없이는 금지한다.
- repo마다 독립 binary inbox, worker scratch, PageIndex storage, renderer profile, credential, concurrency/cost
  budget을 사용한다. 저장 암호화를 나중에 채택한다면 encryption key도 repo별로 분리한다.
- tenant 간 source hash 존재 여부, artifact, cache hit, external PageIndex doc id를 노출하지 않고 cross-tenant
  blob/cache dedup을 하지 않는다.
- page/slide read는 candidate를 소유한 repo/scope에서 실행하고 result마다 `repo_id`와 `knowledge_scope`를
  유지한다. shared/global PageIndex collection은 tenant security boundary로 인정하지 않는다.

---

## 13. Update, recompile, reindex, delete

용어를 분리한다.

- **recompile:** 같은 committed summary/source generation에서 Schema v2가 허용한 concept/entity/index만 다시
  만든다. PageIndex와 raw extraction은 다시 실행하지 않는다.
- **reindex:** raw source를 새 compiler/profile/model/config로 다시 처리해 새 generation을 만든다.
- **update:** logical source에 새 source version을 추가한다.
- **forget:** 현재 serving과 future query에서 제거한다.
- **hard erasure:** Git history, remote, clone, backup, cloud provider copy까지 물리적으로 제거한다.

동일 source/version/build request가 곧 동일 artifact라는 뜻은 아니다. ready로 선택된
`artifact_digest`가 있을 때만 publish가 idempotent하며, compiler나 model 변경은 read path에서 자동
실행하지 않고 명시적 reindex job으로 남긴다. 새 generation이 ready가 된 commit에서만
manifest의 `active.generation`을 전환한다.

update가 short↔long threshold를 넘나들면 기존 flat `.md`와 long-document JSON/Summary/images 중 한 표현만
active projection이 되도록 같은 CAS publish에서 전환한다. obsolete projection, link, citation index를 함께
제거/재작성해 같은 source가 두 번 검색되지 않게 한다. 새 raw bytes는 versioned path에 남기고 이전 raw
version을 덮어쓰지 않는다.

delete/forget도 inbox event를 거쳐 curator가 처리한다. tombstone commit이 curated ref에 publish된 뒤부터
query에서 제외하며 늦은 worker의 publish를 막는다. inbox에 delete event가 도착하기만 한 시점을 “즉시”로
간주하지 않는다. summary, source JSON, current images, cache, pending jobs와 해당 source가 기여한 concept
provenance를 정리한다. 여러 source가 합쳐진 concept는 해당 source contribution만 제거한다.

현재 Git tree에는 active generation의 image set만 노출하고, 이전 세대는 과거 commit으로 재현한다. scratch,
orphan, superseded generation의 GC는 publish와 분리된 bounded job이며 retention/PR 상태를 확인한 뒤 수행한다.

일반 Git deletion은 과거 commit/remote/clone/backup의 원본을 지우지 않는다. retention ADR이 확정되기 전
hard erasure를 보장하지 않는다. 이는 team/민감 문서 출시의 별도 gate다.

---

## 14. Security, privacy, resource control

### 14.1 Untrusted parser/renderer sandbox

PDF parser, OOXML parser, LibreOffice renderer, OCR, PageIndex worker는 별도 process/sandbox에서 실행한다.

- local mode 기본 network-none
- wall/CPU/RSS/open-file/process/disk limits
- compressed bytes, expanded OOXML bytes, member/file/XML 크기 제한
- PDF page count, extracted chars, image count/bytes/pixels 제한
- PPTX slide count, relationship count, embedded object/media 크기 제한
- tree node/depth/token 제한
- no shell interpolation; explicit argv
- PPTX external relationship, remote image/font, macro/OLE를 실행하거나 fetch하지 않음
- SVG/HTML/active image content와 embedded object는 직접 web-serving하지 않음. 허용된 raster format으로
  decode-limit 아래 재인코딩한 derivative만 별도 download origin/안전한 response header로 제공

기존 PPTX zip-bomb guard는 유지하되 long-document worker에도 동일하거나 더 강한 limit를 적용한다.

### 14.2 Prompt injection

- document text, notes, alt text는 instruction이 아니라 untrusted data다.
- indexing/navigator model에는 shell, Git, wiki write, credentials, broad filesystem read를 주지 않는다.
- model output은 strict schema와 deterministic validator를 통과해야 한다.
- 외부 source에서 파생된 summary/concept는 provenance/trust flag를 갖는다.
- long-document summary와 raw page/slide content는 gold pack에서 기본 제외한다.
- concept/entity claim을 gold로 승격하려면 실제 page/slide evidence를 요구한다.

### 14.3 Cloud egress

mode를 ambient API key로 추론하지 않는다.

```text
local-offline
local-external-llm
cloud-pageindex
cloud-ocr-or-vision
```

repo별 explicit config와 consent가 필요하다. raw PDF/PPTX, rendered page, slide image, speaker notes 중 무엇이
어느 provider/region으로 전송되는지 audit record에 남긴다. credential과 provider query trace는 KB Git에
저장하지 않는다.

일반 writer는 capture와 자기 event 조회만 할 수 있다. reindex, forget/delete, cloud egress consent/profile
변경은 별도 operator/admin authorization을 요구하며 모든 변경을 audit한다.

---

## 15. Dependency와 license posture

OpenKB가 pin한 `pageindex==0.3.0.dev3`는 `pymupdf>=1.26.0`을 직접 의존한다
([PageIndex tag](https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pyproject.toml#L22-L34)).
이는 Agora가 core에서 PyMuPDF를 피하는 [ADR-0005](../adr/0005-fully-oss-bom.md) 방향과 충돌한다.

작성 시점 PageIndex main development snapshot은 PyPDF2/pypdfium2 기반으로 바뀌었지만 OpenKB가
사용하는 Collection API와 호환되지 않는다
([current dependencies](https://github.com/VectifyAI/PageIndex/blob/ae2a5b49b5411903633faa299201d6ba1769fd2f/pyproject.toml#L27-L49)).
따라서 adapter는 semantic version range가 아니라 exact commit/wheel hash와 SBOM으로 고정한다.

초기 허용 방향:

- Agora core에 OpenKB/PageIndex/PyMuPDF를 required dependency로 추가하지 않는다.
- permissive dependency로 구성된 audited local adapter/fork 또는 stable release를 사용한다.
- OpenKB dev3/PyMuPDF 경로는 user-installed optional adapter + 적절한 라이선스 또는 commercial license가
  있을 때만 고려한다.
- MarkItDown/python-pptx는 현재 optional ingest dependency를 재사용하되 exact version과 output contract를
  고정한다.
- LibreOffice는 optional external renderer이며 core library에 링크하지 않는다. 배포 image에 포함하면 전체
  license/notice BOM을 검증한다.

프로세스 격리가 라이선스 의무를 자동으로 없애는 것은 아니다. 최종 packaging은 별도 license review gate다.

---

## 16. Config shape — illustrative only

아래는 아직 parser에 wired되지 않은 Draft 예시다.

```yaml
long_documents:
  enabled: true

  routing:
    mode: auto                 # auto | force | never
    pdf_pages: 20              # OpenKB compatibility starting point
    pptx_slides: 20            # hypothesis; benchmark before ratification
    max_direct_tokens: 50000   # illustrative, not a decided default

  profiles:
    pdf:
      selected: local-structure-only
      local-structure-only:
        network: none
      local-model-enriched:
        enabled: false
        network: none
      pageindex-pinned:
        enabled: false           # audited permissive artifact 확보 후 opt-in
        exact_artifact: "TBD"
    pptx:
      compiler: pptx-native
      rendered_pageindex: false
      renderer: libreoffice

  pptx:
    speaker_notes: preserve-exclude-from-search
    hidden_slides: preserve-exclude-from-search
    image_ocr: off

  limits:
    max_source_bytes: 26214400
    max_units: 1000
    max_units_per_read: 12
    max_return_bytes: 524288
    max_tree_nodes: 2000
    max_tree_depth: 8
    max_images_per_unit: 50
    max_scratch_image_bytes: 524288000
    max_published_image_bytes: 52428800
    max_repo_asset_bytes: "TBD-by-deployment-policy"
    worker_timeout_seconds: 1800

  cloud:
    enabled: false
```

`local-structure-only`는 permissive parser/rule-based hierarchy의 목표 profile이고,
`local-model-enriched`는 local model summary/tree를 추가하는 별도 profile이다. `pageindex-pinned`는 정확한
wheel/commit과 license를 감사한 뒤 여는 실제 PageIndex adapter slot이다. 셋 다 아직 구현·감사 완료된 adapter
이름이 아니다. `local-structure-only`도 OpenKB wire artifact를 만드는 fallback 목표일 뿐 PageIndex와 동등한
tree/summary 품질을 보장하지 않는다. 모든 숫자는 구현 전에 representative corpus로 검증해야 한다. scratch cap,
한 generation의 Git-published cap, repo 전체 quota는 서로 다른 budget이다. 위 숫자는 illustrative이며
`pptx_slides=20`도 OpenKB PDF threshold와 대칭인 출발점일 뿐 품질 최적값이라는 주장이 아니다.

---

## 17. Observability와 운영 계약

저장되는 source/generation 상태:

```text
source: active · tombstoned
source proposal: awaiting_source_review · merged · rejected · preserved_for_requeue
build job: queued · compiling · needs_ocr · validating · validated · failed · cancelled
candidate: validated · awaiting_review · published · rejected
published generation: ready · superseded
```

필수 metric/log:

- repo/document/profile별 queue wait와 processing duration
- source units, tree nodes/depth, extracted chars/assets
- renderer/parser/LLM retry 수
- input/output/cache token과 추정/실제 비용
- resource-limit termination과 failure reason
- mapping/validation warning
- stale generation/tombstone publish 차단 수
- orphan attachment/artifact 수
- storage amplification(raw 대비 Git/derived bytes)

dashboard/API의 `indexing`, `stale`, `not_ready`는 저장 상태가 아니라 위 상태와 active/pending manifest에서
계산하는 view다. `indexing`은 active가 없거나 이전 active를 serve하면서 pending build가 진행 중인 경우,
`stale`은 pending과 active의 build request fingerprint가 다른 경우, `not_ready`는 source별 read에 사용할 ready
generation이 없는 경우다. 실패를 generic upload failure로 숨기지 않고 원본이 보존되었는지, 재시도 가능한지,
cloud consent가 필요한지 보여준다.

---

## 18. Test와 evaluation plan

### 18.1 Contract/invariant test

- face가 `raw/`/`wiki/`를 직접 쓰지 못함
- compiler가 canonical repo를 수정하지 못함
- publish는 repo별 single writer + CAS
- `_kb` 삭제 후 Git-tracked raw/source manifest에서 clone/offline query와 job inventory 복구
- cross-tenant same-name/same-hash artifact/cache/credential 완전 격리
- default upload는 Base, explicit Domain upload만 nested artifact root에 publish하며 Base query/다른 Domain으로
  자동 fan-out하지 않음
- 같은-repo Domain→Base fallback과 별도-repo federation을 각각 ranking/ACL gate로 차단
- Unicode normalization/case-folding/동일 filename·title 충돌에도 stable `doc_name` 생성
- stale worker가 updated/tombstoned source를 publish하지 못함
- crash injection: attachment/event, Commit A/enqueue, compile/validate, validate/Commit B, CAS/finalize 경계
- Commit A 후 inbox finalize와 durable job 재생성
- 같은 build request의 서로 다른 artifact digest를 자동 동일시하지 않음
- artifact digest에서 attempt/digest/generation을 제외해 self-reference와 attempt별 content drift를 방지하고,
  최종 published projection checksum을 별도로 검증
- pending source version마다 immutable raw pointer가 존재하고 stale 판정은 같은 타입의 build fingerprint끼리 비교
- PR mode에서 Commit A merge 전 compile 및 Commit B merge 전 serving 차단
- source PR merge/close/reject와 review-ledger loss마다 inbox event finalize/requeue/retention disposition 검증
- transient retry의 새 attempt id, max/backoff, terminal validation/resource failure, needs-OCR consent gate 검증
- publish와 동시에 반복 query해도 summary/source/images/manifest가 한 commit/generation으로만 관측됨
- short↔long update에서 obsolete projection/link/index가 원자적으로 제거되고 중복 검색되지 않음
- path traversal/symlink/absolute path 거부
- SVG/HTML/active asset 직접 serving 거부와 raster decode/pixel bomb 제한
- PDF artifact를 actual OpenKB reader로 읽는 golden compatibility test
- PPTX extra field를 legacy OpenKB page-reader unit이 무시하고 numeric units를 읽는 compatibility test
- unmodified OpenKB end-to-end query/lint/recompile/remove가 PPTX 호환 범위 밖임을 고정하는 negative test

### 18.2 PDF corpus

- 19/20/21-page threshold
- 200+ page 한국어 PDF의 후반부 사실
- 목차 없음, mixed orientation, printed label/physical page 불일치
- 표/그림이 많은 PDF
- scanned/image-only, encrypted, corrupt, blank, malformed/bomb PDF
- 동일 bytes 재업로드와 같은 filename의 다른 bytes

### 18.3 PPTX corpus

- 19/20/21 slides
- 한국어/영어, missing/substituted fonts
- hidden/blank/duplicate-title/reordered slides
- speaker notes on/off
- image-only slide
- table/chart/SmartArt/grouped/overlapping shapes
- animation/build, embedded video/OLE, external relationships
- presentation order와 package XML filename 순서 불일치
- corrupt/encrypted OOXML, zip bomb, huge image/pixel bomb
- LibreOffice render page-count mismatch와 font substitution
- native vs rendered-PageIndex tree 비교

### 18.4 Retrieval quality와 cost

- correct document recall
- correct page/slide unit recall
- citation precision과 excerpt-grounding rate
- nonexistent node/range 생성률
- `not_found` false-positive/false-negative
- table/chart/image/notes 질문 정확도
- prompt-injection canary 통과율
- P50/P95 index/query latency
- CPU/RSS/disk/Git amplification
- document/query당 LLM token과 cost
- crash/cancel/update/delete 후 orphan/stale artifact 수

baseline은 현재 flat Markdown search, OpenKB upstream PDF pipeline, `pptx-native`,
`pptx-rendered-pageindex`다. PPTX 기본 profile은 이 benchmark 후 결정한다.

---

## 19. 단계별 도입안

### Stage 0 — ADR와 schema ratification

- binary inbox attachment + raw preservation
- source/generation manifest, content-addressed raw path, PR/direct publish semantics
- 최소 LongDocument Artifact Schema(Summary/source/image/manifest dispatch)
- PDF OpenKB conformance + PPTX read-compatible extension 및 full OpenKB-native Schema v2의 후속 gate
- typed locator와 bounded unit query
- license/sandbox/cloud policy

### Stage 1 — 원본 보존과 fake compiler

- PDF/PPTX binary capture
- Commit A source state
- fake artifact compiler/validator
- crash/CAS/recovery/adversarial test

### Stage 2 — PDF local profile

- permissive PDF page extractor
- permissive `local-structure-only` adapter부터 시작하고, 감사된 PageIndex local adapter를 별도 profile로 추가
- OpenKB summary/source conformance
- `kb_document_tree`/`kb_document_read`

### Stage 3 — PPTX native profile

- slide-native structured extraction
- slide hierarchy compiler
- notes/hidden/assets policy
- slide citation UI/API

### Stage 4 — PPTX rendered PageIndex experiment

- sandboxed LibreOffice renderer
- slide↔PDF mapping proof
- actual PageIndex tree + native evidence enrichment
- native profile과 benchmark

### Stage 5 — compiler integration과 operations

- concept/entity generation with page/slide evidence
- async scheduler/dashboard/budgets/reindex/import
- same-repo Domain→Base fallback ranking policy; separate-repo 조합은 auth/federation ADR 이후에만

### Stage 6 — opt-in OCR/vision/cloud

- local OCR first
- explicit cloud consent/audit/delete receipt
- team privacy/retention gate 통과 후 enable

---

## 20. 열린 결정

이 Draft를 ADR로 승격하기 전에 다음을 결정해야 한다.

1. **PPTX default tree engine**
   - 추천: `pptx-native`를 안전한 기본값으로 두고 `pptx-rendered-pageindex`는 benchmark를 통과한 선택
     profile로 시작한다.

2. **PPTX long-document routing**
   - slide count만 볼지, extracted tokens/visual density/user force를 함께 사용할지 결정한다.
   - 추천: count + token/complexity + explicit override의 복합 조건.

3. **Speaker notes**
   - 구조적으로 보존하되 검색/LLM context에는 기본 제외할지, personal/team별 기본값을 달리할지 결정한다.
   - 추천: preservation과 serving을 분리하고 note evidence를 명시적으로 표시한다.

4. **Hidden slides**
   - original ordinal에는 포함하되 기본 retrieval에서 제외할지 결정한다.
   - 추천: preserve + default exclude; repo policy로 opt-in.

5. **Visual content**
   - slide rendering/OCR/VLM을 기본 처리할지, `needs_ocr`/on-demand로 둘지 결정한다.
   - 추천: local text/native asset가 기본, vision은 명시적 profile.

6. **Logical document identity/update**
   - stable source key를 caller가 제공할지, Agora가 path/provenance로 생성할지 결정한다.

7. **Large binary storage boundary**
   - changed bytes는 content-addressed/versioned raw path에 보존하는 것을 전제로, plain Git size cap,
     Git LFS, external immutable blob manifest의 경계를 정한다.
   - §2.1의 working assumption은 size cap 아래 plain Git 보존이다.

8. **OpenKB-native Schema v2**
   - 최소 long-document artifact dispatch를 먼저 ratify할지 full migration과 한 번에 승인할지 결정한다.
   - Summary 전용 dispatch뿐 아니라 source manifest, root index, concept/entity, ordinary Agora note와의
     link/lint/query/migration 규칙을 확정한다. current four-note-type parser에 단순히 `Summary`만 추가해서는
     해결되지 않는다.

9. **Forget vs hard erasure**
   - team/PII 배포 전에 Git remote/clone/backup/cloud provider까지 포함한 retention ADR이 필요하다.

10. **Compatibility version**
    - 어느 OpenKB commit/artifact fixture를 conformance target으로 pin하고 upgrade를 어떻게 승인할지 정한다.

11. **Publish review mode**
    - personal direct CAS와 team PR review 중 deployment별 기본값, 누가 raw/source PR 및 artifact PR을
      승인할지 정한다.

12. **Base/Domain schema migration**
    - 현재 domain-required raw/wiki layout에서 Base flat root를 어떻게 도입하고 기존 `general` content를
      이동/alias할지, Domain→Base fallback ranking과 per-domain ACL의 범위를 정한다.

---

## 21. 향후 ADR 승격 지도

한 ADR에 모든 결정을 넣지 않는다.

1. **Binary attachment + original preservation ADR**
   - 새 ADR이 [ADR-0020](../adr/0020-web-upload-write-path.md)의 deferred binary-staging 조항을
     materialize하고, 변경되는 조항을 명시적으로 amends/supersedes
   - [ADR-0010](../adr/0010-kb-wiki-schema.md)의 current raw-writer 조항 중 바뀌는 부분도 명시
   - append-only attachment, content-addressed raw writer, size/LFS, inbox finalize, crash/GC 계약

2. **Format-neutral LongDocumentCompiler + Schema v2 ADR**
   - [ADR-0004](../adr/0004-pluggable-adapters.md), [ADR-0010](../adr/0010-kb-wiki-schema.md) 확장
   - PDF/PPTX unit schema, Base/Domain artifact roots, source/generation manifest, OpenKB artifact,
     canonical/derived 경계, validator, direct/PR publish와 commit-consistent read

3. **Tree navigation + bounded locator query ADR**
   - [ADR-0009](../adr/0009-deterministic-query-contract.md),
     [ADR-0012](../adr/0012-deterministic-query-ranking.md) 확장
   - corpus selection, typed locator, evidence verification, `not_found`

4. **Compiler sandbox/resource/license ADR 또는 명시적 appendix**
   - 새 ADR/appendix가 [ADR-0005](../adr/0005-fully-oss-bom.md),
     [ADR-0008](../adr/0008-transactional-sandboxed-curation.md),
     [ADR-0013](../adr/0013-curator-sandbox-mechanism.md)에 명시적으로 layers on/amends

5. **Cloud OCR/vision egress ADR**
   - 실제 provider/profile을 채택할 때만 작성

retention/hard-erasure는 별도 reserved decision이며 이 Draft가 해결하지 않는다.

---

## 22. Provenance와 non-commitment

이 문서는 2026-08-17 OpenAI Codex가 Agora 저장소의 현재 코드/ADR과 다음 upstream snapshot을 읽고 작성한
설계 초안이다.

- OpenKB `ff54396e575ee6feb0113b631a34caa082b441cc`
- PageIndex `ae2a5b49b5411903633faa299201d6ba1769fd2f`
- OpenKB가 pin한 PageIndex `0.3.0.dev3` / commit `9ad54122bbd519cec8913198e2d63cff92781c1e`
- MarkItDown `0.1.6` / commit `e144e0a2be95b34df17433bac904e635f2c5e551`

업스트림 PageIndex의 PPTX native support는 확인되지 않았고 공식 문서는 PDF-only라고 명시한다. 따라서
PPTX 부분은 OpenKB의 현재 동작과 동일하다는 주장이 아니라, OpenKB path/query 철학을 보존하면서 Agora가
slide-native evidence를 추가하는 제안이다.

이 note는 기존 [`STRATEGY-2026-08.md`](../STRATEGY-2026-08.md)의 PageIndex 관련 가정 중
“기존 Extractor Protocol에 seam 변경 없이 들어간다”는 부분을 **재검토할 근거**를 기록한다. 비규범 Draft가
그 문서를 supersede하지는 않는다. 결정이 ratify되면 해당 section을 ADR로 승격하고 이 note에는
`superseded by ADR-00xx` pointer를 남긴다.

Related:
[`DESIGN.md`](../DESIGN.md) ·
[`ARCHITECTURE.md`](../ARCHITECTURE.md) ·
[`DATA-MODEL.md`](../DATA-MODEL.md) ·
[`ROADMAP.md`](../ROADMAP.md) ·
[`ADR index`](../adr/README.md) ·
[`retrieval design note`](retrieval-vs-vectordb.md)
