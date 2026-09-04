# 설계 노트: Stratum — 목표 위키 구조 (디렉터리가 종류다)

> **상태: Draft · Exploratory · Non-normative · NOT ratified.**
> **근거:** 2026-08-22/23 설계 워크플로 — 서로 다른 전제에서 출발한 목표 아키텍처 후보 4안을 독립
> 생성 → 심사 2인 → 적대적 파괴 시도 → 마이그레이션 감사. **기록:** 2026-09-02.
>
> 이 문서는 **결정이 아니다.** 어떤 ADR도 supersede하지 않고 구현 승인도 아니다. 아키텍처 SSOT는
> [`DESIGN.md`](../DESIGN.md)이고, 아래의 어떤 항목도 별도 ADR이 Accepted 되기 전에는 규범적 동작을
> 바꾸지 않는다. 판정 맥락은 [`STRATEGY-2026-08.md` §14.8](../STRATEGY-2026-08.md)이다.
>
> 자매 노트: [`openkb-compatible-long-document-compiler.md`](openkb-compatible-long-document-compiler.md)
> (장문 serving 계약). 그 노트의 `sources:`/`summaries:` 제안이 이 레이아웃의 `summaries/`에 착지한다.

---

## 1. 진단 — 스키마의 축이 뒤집혀 있다

경로가 **주제**를 지고(`wiki/<domain>/themes/…`), 닫힌 4값 `type:` enum이 **종류**를 진다. 이 역전
하나가 여섯 블로커의 기계적 원인이다:

- 긴 문서에 집이 없다 (`type: Summary`는 lint L1-11 hard error)
- 증거에 티어가 없다 (`raw/`가 검색 코퍼스 밖 — [#139])
- 엔티티에 노드 종류가 없다
- 무손실을 지키려면 `domains[0]`가 **거짓일 수도 있는 주제를 단언**해야 한다 (ADR-0022 catch-all)
- 새로운 종류의 지식이 lint 재작성을 요구한다
- 도메인이 **경로 세그먼트 외 어디에도 기록되지 않는다** → 무복귀선

OpenKB에서 가져오는 것은 디렉터리 모양이 아니라 **원리 하나 — 디렉터리가 종류다.**
(모양을 그대로 맞추는 안은 [`STRATEGY-2026-08.md` §10](../STRATEGY-2026-08.md)에서 이미 기각됐다:
`openkb add`의 롤백이 `wiki/concepts`·`wiki/entities`를 디렉터리째 스냅샷한 뒤 백업에 없는 라이브
파일을 `unlink()` 한다.)

## 2. 목표 레이아웃

```text
<repo>/                          한 git 저장소 = 한 테넌트
  index.md · log.md · AGENTS.md  정본
  wiki/                          정본 · Base 루트 — 첫 세그먼트가 종류다
    concepts/[<any>/<sub>/]<slug>.md   Concept  (was type: theme)
    summaries/<doc-slug>.md            Summary  긴 문서 한 편의 항해 트리   (신규)
    notes/<yyyy>/<mm>/<slug>.md        Note     날짜 캡처 + 무손실 바닥 (was type: daily)
    maps/<slug>.md                     Map      (was type: moc)
    entities/<slug>.md                 Entity   등록되되 게이트된 채움      (신규)
    people/<person>/**.md              사람의 영역 — 큐레이터가 쓰지 않고 lint는 권고만.
                                       검색·그래프·web·MCP는 1급 코퍼스로 읽는다.
                                       `file:` 커넥터 → 후보 게이트로만 wiki/에 닿는다
  assets/**                      정본 · 첨부 (옵시디언 호환), `/assets/{path}`
  raw/                           정본 · 증거 티어 — 자리를 옮기지 않는다
    <domain>/<event_id>.md             그대로 둔다. 무의미하지만 무해한 샤드 키가 된다
    _blob/<ab>/<sha256>.<ext>          원본 바이트 — 불변, 콘텐츠 주소            (신규)
    _blob/<ab>/<sha256>.meta.yaml      캡처 provenance 사이드카                  (신규)
    _pages/<sha256>/<gen>/units.json   git이 추적하는 유일한 파생물 — 클론만으로 오프라인 응답
  _kb/                           파생 · gitignore · 재구축만. 지우면 시간만 잃는다
    inbox/ processing/ processed/ failed/ index/ graph/ gold/ cursors/ longdoc/
```

**정본** = 조용히 바뀌면 안 되는 것. **파생** = 바이트 동일하게 재구축 가능한 것. 파생물이 정본을
침범하는 지점은 `raw/_pages/` 하나뿐이고, 그건 *"클론만으로 오프라인에서 답한다"* 는 계약을 사기 위한
의도적 예외다.

## 3. 규칙 넷

1. **도메인은 경로를 떠나 `subjects:` 프론트매터가 된다.** 종류 디렉터리 *아래* 자유 하위 폴더
   (`wiki/concepts/engineering/`)는 허용하되 **어떤 코드도 그 세그먼트를 읽지 않는다.** 옵시디언에서
   폴더로 정리하던 사람은 그대로 쓰고, 아고라는 프론트매터만 본다.
2. **`people/` 트리가 정본 1급 시민이 된다.** 오늘의 "한 저장소, 두 네임스페이스"
   ([`STRATEGY-2026-08.md` §4](../STRATEGY-2026-08.md))는 사람 영역을 아고라가 **아예 못 보게** 해서
   안전을 샀다 — 못 보니까 검색도 안 된다. Stratum은 읽기는 1급, 쓰기는 금지로 분리한다.
3. **`raw/`는 옮기지 않는다.** 후보 4안이 전부 옮기려 했고 전부 틀렸다. `src/` 전체에서 `raw/` 경로로
   부터 도메인을 **읽어내는** 코드는 0건이지만, 그 문자열은 **모든 노트의 `sources:` 프론트매터에
   저장돼 있고** lint L1-7/L1-8이 그 참조의 해석 가능성을 검증한다. 재경로화 = 저장소 모든 노트의
   provenance 사슬 재작성 = **아무도 이름 붙이지 않았던 두 번째 무복귀선.**
4. **OpenKB 바이트 수준 상호운용은 의도적으로 포기한다.** JSON 모양과 필드명은 유지하되 `full_text:`가
   `wiki/` 밖(`raw/_pages/`)을 가리키므로 OpenKB 클라이언트가 아고라 저장소를 직접 읽을 수 없다.
   상호운용 경로는 `agora export`다. 동거는 §2 롤백 폭발 반경 때문에 **금지**다.

## 4. 무복귀선은 하나뿐이고, 뒤집기 *전에* 닫는다

나머지는 기계적으로 복구된다(`_NOTE_TYPES` 참조는 3곳). 도메인만은 경로 외 어디에도 없다.

**닫는 법** ([#156]): 워커가 `domain:`을 모든 노트 프론트매터에 **물질화**한다. 이것만 하면 이후 모든 도메인
결정이 되돌릴 수 있게 되고, 뒤집기를 서두를 이유가 사라진다. `subjects: []`는 **아무것도 잃지 않고
아무것도 단언하지 않는** 초기값이라 ADR-0022의 no-loss catch-all과 충돌하지 않는다.

## 5. 적대적 검증이 찾은 셋 — 전부 "Phase 0는 안전하다"를 겨눈다

| | 발견 | 현재 상태 |
|---|---|---|
| **D1** | 가장 많이 인용된 결정론적 승리가 [#146]을 못 고친다 — 이겨야 할 `_passes_gate`가 점수가 아니라 **불리언**이고, `d_moc=0` 스텁은 `struct ≥ 0.7 → combined ≥ 0.245 > FLOOR 0.18`이라 **어떤 크기의 점수 변경으로도** 못 막는다 | **절반 해소.** 2026-08-24 `be3ed22`가 구조 항을 렉시컬 증거에 조건부로 걸었다(ADR-0012 애드덤). **thin-page 절반은 남아 있고** `tests/core/test_wiki_lexical_evidence_146.py`에 `strict=True` xfail로 고정돼 있다 |
| **D2** | 바이트 우선 캡처가 `curator/worker.py:1591` `_is_engine_written_raw`(#135가 굳힌 함수)의 재작성을 **강제한다** — `read_text(utf-8)` 완전 일치로만 승인하므로 바이너리 blob은 통과할 수 없다 | **미해결.** 관문 A |
| **D3** | 유니코드 슬러거가 `curator/plan.py:92` `_SAFE_TOKEN_RE_PATTERN`(ASCII 전용)에 막힌다 — 그 정규식은 포맷 규칙이 아니라 **탈출 방지 보안 통제**다 | **미해결.** 관문 A |

> **6개월 뒤 후회 후보로 지목된 것:** *"콘텐츠 주소가 되면 `raw/` 승인 게이트는 이식이 아니라
> 사라진다"* 는 단순화. **`hash(bytes) == basename`은 무결성 검사이고 `path in raw_writes`는 저작자
> 검사다. 대체재가 아니다.** 이름과 해시가 맞는 blob을 PASS-2 브레인이 쓰면 그냥 통과한다 —
> [#135]가 닫은 심기(planting) 경로가 "단순화"의 얼굴로 다시 열린다.

## 6. 비용 — 그리고 초판이 23배 틀렸던 곳

```
레이아웃 어휘를 담은 줄:  897줄  (테스트 650 + src 247)
그 어휘를 언급하는 파일:  38개 파일, 총 26,793줄     ← 초판이 편집비로 오독한 수치
```

파일 단위 지표를 줄 단위 편집 비용으로 읽은 **약 23배 과대 계상**이었다. 그 정정의 결과로 초판의
**Phase −1(테스트 심 ~1,200줄)이 삭제된다** — 그것으로 피하려던 897줄 편집보다 비싸고,
`core/layout.py`가 이미 접근자 27개를 갖고 있어 넷을 더하면 된다(~40줄).

전체 추정 **≈8 인월**(초판 ~15에서 정정). 그중 "출시 전이라 하위 호환을 안 지켜도 된다"는 제약 해제가
2.4, 측정 오류 정정이 6.5다 — **둘의 성격이 다르므로 합쳐서 "제약 해제가 7 인월을 아꼈다"고 적으면
안 된다.**

## 7. 관문 둘 — 승인의 선행 조건

- **A — 무결성 경계 v2를 Phase 0의 *첫* 단위로** ([#154]). D2·D3의 숨은 재작성을 비용 매겨진 단위 하나로
  바꾼다. 출구 조건: [#135] TAMPER/DELETE/COVERED-DELETE 매트릭스 + [#136] 파생 충돌 코퍼스 통과.
  비싸면 Phase 2가 아니라 **2주차에** 안다.
- **B — n=24 검색 하네스 5갈래 재실행** ([#155]) + [#146]의 네 질의 재생. 일주일 미만, 마이그레이션 없음.
  **적대자 예측: arm B는 넷 중 0개를 뒤집는다.** 맞으면 설계의 검색 절반이 지금 무너진다.

그리고 관문보다 앞에 **레이아웃 독립 정직성 계약 넷**이 온다 — 어느 아키텍처를 고르든 동일하게
필요하고, 하나는 건너뛰면 되돌릴 수 없다:

1. [#144] 쓰기 경로 랭커 심 — `curator/bundle.py:145`가 후보마다 `wiki.query()`를 부르고 op 어휘에
   DELETE가 없어 **오병합이 영구**다. *목록에서 유일하게 되돌릴 수 없는 항목.*
2. [#146] thin-page 절반
3. [#147] 불변식 6 위반 2건 — 코어가 Claude Code를 하드코딩해 모든 `session:` 커넥터가 조용히 0건
4. [#152] 사람이 쓴 노트 하나가 런을 영구 정지 — 옵시디언 호환의 실질 전제

## 8. 하지 않는 것

- **`raw/` 이동** (§3-3) · **`agora rebuild`** — 실측에서 재큐레이션이 실제 vault 12개 중 **0개**를
  복구했고, 도그푸드의 `raw/`는 사람의 산문과 바이트 동일이라 재생성은 오너의 글을 패러프레이즈한다
- **뒤집기 선착수** — 897줄은 지금도 3주 뒤에도 897줄이다. 무결성 경계가 열린 채 레이아웃까지 움직이면
  문제가 생겼을 때 원인이 둘 중 어느 쪽인지 가릴 수 없다
- **새 `type:` 값 추가** — [`STRATEGY-2026-08.md` §12](../STRATEGY-2026-08.md)가 이미 판정했다

## 9. 미결

- 비준은 이슈 [#153]이 소유한다. 경로: 이 노트 → 레이아웃 ADR 1건(+ ADR-0010/0011/0012/0022 애드덤). ADR 판정 초안은
  **KEEP 11 · AMEND 12 · SUPERSEDE 5 · WITHDRAW 0**이며, 모든 supersede가 해당 ADR이 스스로 예약한
  "Future work"의 집행이다 [조사]
- `summaries/`의 내부 계약은 자매 노트에 있고 그쪽도 **미비준**이다
- `people/` 트리를 검색 코퍼스에 넣을 때의 레닥션 경계 — `file:` 커넥터 경계(ADR-0023)와 **읽기 코퍼스**
  경계는 같지 않다. 미설계

[#135]: https://github.com/handochan/agora-kb/issues/135
[#136]: https://github.com/handochan/agora-kb/issues/136
[#139]: https://github.com/handochan/agora-kb/issues/139
[#144]: https://github.com/handochan/agora-kb/issues/144
[#146]: https://github.com/handochan/agora-kb/issues/146
[#147]: https://github.com/handochan/agora-kb/issues/147
[#152]: https://github.com/handochan/agora-kb/issues/152
[#153]: https://github.com/handochan/agora-kb/issues/153
[#154]: https://github.com/handochan/agora-kb/issues/154
[#155]: https://github.com/handochan/agora-kb/issues/155
[#156]: https://github.com/handochan/agora-kb/issues/156
