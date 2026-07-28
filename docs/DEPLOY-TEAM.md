# 팀 배포 가이드 — 허브 토폴로지 (2~10명, Phase-4 auth 이전) — issue #68

Phase-4 auth 없이 소규모 팀(2~10명)이 하나의 KB를 공유하는 **허브 중심 토폴로지** 가이드.
핵심 제약은 구조적이다: inbox는 git-ignore된 `_kb/` 아래에 있으므로 **git으로 쓰기를 동기화하는
것은 원리적으로 불가능**하다 — 모든 쓰기는 허브 1대의 로컬 파일시스템에 착지해야 한다. 이 구성은
불변식(#2 모든 쓰기는 inbox 경유, #3 inbox append-only, 단일-writer 큐레이터)을 그대로 유지한다.

이 가이드는 **이미 코드에 랜딩된 기능만** 다룬다: `agora sync` push-only 백업(#64), `deploy/`
상시 구동 유닛(#65), 업로드 SSRF 가드 + `web.upload.url_enabled` 스위치(#66) + zip-bomb
캡(#53), `web.security` Host 허용목록 + Origin 검사(#94), `web.identity.trusted_header`
per-user 신원(#67), MCP 항해 도구
`kb_read`/`kb_neighbors`(#58), gold 팩 원격 소비 채널 `kb_context` / `GET /api/gold/{pack}`(#40).

## 1. 토폴로지 — 허브 1대

```
                     ┌───────────────────────── 허브 (1대) ─────────────────────────┐
                     │  knowledge repo (git)                                        │
 팀원 A (MCP 쓰기/읽기) │   ├─ wiki/ · index.md · raw/ · log.md   ← git-tracked (SSOT) │
 ssh -T agora@hub ───┼──▶ agora serve --writer alice ─▶ _kb/inbox/alice/ (append만) │
                     │   └─ _kb/   ← git-ignored 스풀 (inbox·cursor·gold·index)     │
 팀원 B (브라우저)      │  agora watch ── 큐레이터(단일 writer, flock) ──▶ wiki/ 편집   │
 https://kb.example──┼──▶ Caddy/nginx (TLS + basic auth) ─▶ agora web @127.0.0.1   │
                     │  agora harvest  (별도 스케줄 유닛, #65)                        │
                     │  agora sync ──(push-only · FF-only)──▶ bare/호스팅 미러       │
                     └──────────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                     팀원 read-only clone (git clone, 읽기 전용)
```

**허브 1대가 소유하는 것**: knowledge repo 본체, `agora watch`(큐레이션 스케줄러),
`agora web`(127.0.0.1 바인드), `agora harvest`(별도 스케줄). 상시 구동은
[`deploy/`](../deploy/)의 launchd/systemd 유닛으로 — 설치 절차·검증·로그(PYTHONUNBUFFERED)
· harvest가 별도 유닛이어야 하는 이유는 [`deploy/README.md`](../deploy/README.md) 참조.

**규율 — 큐레이션 홈은 허브 1대뿐이다.** 큐레이터의 단일-writer 잠금(`fcntl.flock`)은
**호스트-로컬**이다: 같은 허브 안의 동시 `curate`는 안전하다 — 잠금은 비블로킹이라 진 쪽
호출은 대기 없이 즉시 noop(`reason_lock_held`)으로 반환된다(파손 없음; 단, 그 호출이 요청한
큐레이션은 수행되지 않고 캡처는 다음 트리거까지 inbox에 남는다). 반면 **두 번째 호스트에서
같은 repo(클론)를 curate하는 것을 코드가 막지 못한다**. 멀티 머신 큐레이션 토폴로지는 #46
ADR로 확정되기 전까지 금지 — 클론에서는 절대 `agora curate`/`agora watch`를 돌리지 않는다.

**허브 repo.yaml 종합 예시** (팀 배포에서 기본값과 달라지는 핵심 키만; 나머지는 기본값):

```yaml
# _kb/repo.yaml — 팀 허브 종합 예시
name: engineering
kind: team                        # 스코프 게이트 신원 선언 (§5) — 미선언이어도 team 취급되지만 명시가 원칙

backup:
  remote: git@git.example.com:team/kb-mirror.git   # 읽기 배포용 미러 (§4)
  auto: true                      # watch 틱 큐레이션 성공 후 자동 push (비대화형·시간 제한)

curator:
  limits:
    max_candidates_per_run: 64    # 큐레이션 1회 처리 후보 상한 (#60) — 팀 유입량에 맞춰 조정

web:
  security:
    allowed_hosts:                # Host 허용목록 (§2, #94) — 명시하면 기본값을 확장이 아니라
                                  # **대체**하므로, 필요한 호스트를 빠짐없이 적는다
      - kb.example.com            # 프록시가 원 Host를 넘기는 공개 호스트를 반드시 추가한다
      - 127.0.0.1                 # 허브 로컬 헬스체크/Prometheus 스크레이프용 (남겨둔다)
      - localhost                 # SSH 터널 브라우저 접속용 (§2의 ssh -L 뒤 localhost:8000)
    # require_origin: true        # 상태변경 요청에 Origin/Referer 필수화 — 스크립트 업로드가
                                  # 없는 팀이라면 권장 (구형 브라우저 잔여 리스크까지 차단)
  identity:
    trusted_header: X-Remote-User # 인증 프록시가 강제 주입하는 헤더 (§2, #67)
    strip_domain: true            # alice@example.com → alice
  upload:
    url_enabled: false            # 서버측 URL 추출 오프 스위치 (#66) — 필요할 때만 true
                                  # (true여도 SSRF 가드는 항상 동작: 사설/루프백/링크로컬/
                                  #  메타데이터 대상과 그리로의 리다이렉트 거부)
    # max_uncompressed_bytes: 209715200   # zip 계열(docx/xlsx/pptx/epub) 압축해제 실크기 캡 (#53)

# harvest:                        # 옵트인 (기본 off) — 팀 리포에서는 scope: team 커넥터만 통과 (§5)
#   enabled: true
#   scope_lock: team
```

설정 로더의 fail-loud는 **부분적**이다: 코드가 읽는 키의 **타입 불일치**는 기동 시
`ConfigError`로 즉시 실패하지만, **키 이름 오타**(미지 키)는
`backup:`·`web.identity:`·`web.security:` 블록에서만 거부되고 나머지(`kind`, `curator.*`, 그 외
`web.*`, `harvest.*` 등)는 조용히 무시되어 기본값이 적용된다 —
예: `max_candidates_per_rn:` 오타는 에러 없이 캡이 기본 32로
남고, `url_enabld:` 오타는 URL 추출이 켜진(기본 true) 채로 남는다. 그러니 위 예시를 그대로
복사해 값만 바꾸고, 반영 후 `uv run agora doctor --repo <repo>`로 backup 라인을 확인하되
doctor가 표면화하지 않는 값(curator 캡 등)은 실제로 달라졌는지 별도로 확인한다.

## 2. Reverse proxy — 웹 face 노출

웹 face는 **인증도 TLS도 없다**. 외부 노출은 반드시 인증 reverse proxy를 앞에 세운다.
**앱을 공개 인터페이스에 직접 바인드하는 것은 금지다** — `--host`를 0.0.0.0 등 비-루프백
주소로 바꾸지 않는다. `deploy/` 유닛은 `--host 127.0.0.1`을 하드코딩하며(테스트로 잠금), 이
가이드의 어떤 예시도 비-루프백 바인드를 포함하지 않는다. 프록시가 없는 원격 접근은 SSH
터널(`ssh -L 8000:127.0.0.1:8000 hub`)만 사용한다.

프록시의 세 가지 의무(#67의 신뢰 경계 — 셋 다 필수):
(1) 모든 요청 인증, (2) 인증된 사용자로 `X-Remote-User` **강제 설정**(set — append 아님),
(3) 클라이언트가 보낸 위조 사본 제거. 여기에 팀 배포에서는
(4) **`/metrics`·`/dashboard`·`/api/dashboard` 경로 차단**(운영 내부 전용 — Prometheus
스크레이프와 운영자 대시보드, 그리고 대시보드 패널의 JSON 쌍둥이 라우트
`/api/dashboard/*`는 허브 로컬에서만)을 더한다.

### Host 표준 — 프록시는 **원 Host를 보존**한다 (#94)

웹 face는 이제 **Host 허용목록**(`web.security.allowed_hosts`, 기본 `localhost`+`127.0.0.1`)을
starlette `TrustedHostMiddleware`로 강제한다. 목록 밖 Host는 **400**이다. 이것이 DNS rebinding
(공격자 도메인을 TTL 0으로 `127.0.0.1`에 재바인딩해 same-origin으로 KB 전량을 읽는 공격)을
막는 유일한 수단이다 — 재바인딩된 요청도 Host에는 여전히 공격자 **도메인 이름**이 실린다.

**표준은 하나다: 프록시는 클라이언트가 보낸 원 Host(`kb.example.com`)를 포트까지 포함해 그대로
앱에 넘기고, 운영자는 그 호스트를 `web.security.allowed_hosts`에 추가한다.** Caddy
`reverse_proxy`는 원 Host를 기본 보존하므로 아래 스니펫이 그대로 표준이고, nginx `proxy_pass`는
기본이 `$proxy_host`(=`127.0.0.1:8000`)라 **`proxy_set_header Host $http_host;` 한 줄이 필수**다
(`$host`가 아니라 `$http_host`인 이유: `$host`는 포트를 떼므로 공개 포트가 443/80이 아닐 때
브라우저 `Origin`과 어긋난다). 이 표준을 따르면 §1 예시의 `allowed_hosts` 한 곳만 보고 두 프록시
구성이 모두 동작한다.

> `allowed_hosts`를 명시하면 기본값(`localhost`+`127.0.0.1`)은 **확장이 아니라 대체**된다.
> 공개 호스트만 적으면 허브 로컬 헬스체크(`curl 127.0.0.1:8000/api/status`)와 SSH 터널 뒤의
> `http://localhost:8000` 접속이 400이 된다 — §1 예시가 셋을 모두 담고 있는 이유다.
>
> **원 Host 보존은 선택이 아니다.** 앱이 받는 Host가 브라우저가 실제로 접속한 호스트와 다르면
> 읽기는 (그 Host를 목록에 넣어) 통과시킬 수 있어도 **브라우저 업로드는 403**이다. Origin 검사가
> "요청 자신의 Host"를 기준으로 하기 때문이며, 이는 의도된 설계다(§아래 CSRF 문단). 403 본문이
> 이 요구사항을 그대로 알려준다.
>
> **`X-Forwarded-Host`/`X-Forwarded-Proto`는 신뢰하지 않는다**(코드가 읽지 않는다). #67이
> `X-Remote-User`에 대해 세운 "프록시가 강제 set/strip 한다"는 신뢰 경계는 운영자가 명시적으로
> 선언한 헤더에만 적용되며, 그 선언이 없는 헤더로 보안 판정을 뒤집지 않는다.
>
> **IPv6 리터럴(`--host ::1`)은 미지원**이다. starlette의 매칭이 `Host.split(":")[0]`이라
> `[::1]:8000`은 어떤 패턴과도 매치될 수 없다. 루프백은 IPv4(`--host 127.0.0.1`, 기본값)로
> 바인드하거나 호스트명으로 접근한다. `allowed_hosts`에 `::1`을 넣으면 기동 시 `ConfigError`가
> 이유와 대안을 알려준다(조용한 무시가 아니다).

**CSRF(브라우저發 인박스 주입) 방어는 "요청 자신의 Host" 기준이다.** 상태변경 3라우트
(`POST /api/upload`·`POST /api/upload-batch`·HTMX `POST /upload`)는 요청의 `Origin`(없으면
`Referer`)의 **host:port**가 그 요청이 보내진 `Host`와 다르면 **403**이고 인박스에는 아무것도
append되지 않는다(scheme은 비교하지 않는다 — TLS 종단 때문). 기준이 `allowed_hosts` 목록이
**아닌** 이유: 목록에는 쓰기 신뢰와 무관한 항목(허브 로컬 `127.0.0.1` 헬스체크용,
`*.team.example.com` 와일드카드)이 들어가는데, 목록을 공유하면 팀원 노트북의 아무 로컬 포트나
탈취된 형제 서브도메인이 공개 허브에 쓸 수 있게 된다. `Origin`이
**아예 없는** 요청(스크립트·CI·아래 검증 `curl`)은 기본값에서 **통과**한다 — 브라우저는
크로스사이트 쓰기에 항상 `Origin`을 붙이므로 불일치 거절만으로 브라우저 경로는 닫히고, absent
거절을 기본으로 하면 이 문서의 업로드 절차부터 깨진다. 스크립트 업로드를 쓰지 않는 팀은
`require_origin: true`로 absent까지 막는 것을 **권장**한다(잔여 리스크: `Origin`을 붙이지 않는
구형 브라우저). GET 헬스체크는 상태변경이 아니므로 Origin 검사 대상이 아니다 — GET에서 통과해야
하는 것은 Host 허용목록뿐이다.

**프레이밍(clickjacking)도 막는다.** 모든 응답에 `X-Frame-Options: DENY` +
`Content-Security-Policy: frame-ancestors 'none'`이 붙는다 — iframe 안에서의 클릭은 face 자신의
origin으로 제출되므로 위 Origin 검사로는 구분할 수 없기 때문이다. 웹 face를 다른 페이지에
임베드하는 용례는 없다.

**Caddy** (공개 DNS면 TLS 자동 발급; 사내망이면 `tls internal` 추가):

```caddyfile
kb.example.com {
    basic_auth {
        alice $2a$14$...   # caddy hash-password 로 생성
        bob   $2a$14$...
    }

    # 운영 내부 전용 경로 — 팀원에게도 노출하지 않는다
    handle /metrics* {
        respond 403
    }
    handle /dashboard* {
        respond 403
    }
    handle /api/dashboard* {
        respond 403
    }

    handle {
        # reverse_proxy 는 원 Host(kb.example.com)를 기본 보존한다 = #94 Host 표준.
        # 앱 쪽 web.security.allowed_hosts 에 kb.example.com 이 있어야 200이 된다(§1 예시).
        reverse_proxy 127.0.0.1:8000 {
            header_up X-Remote-User {http.auth.user.id}   # 강제 설정: 클라이언트 값 덮어씀
        }
    }
}
```

**nginx** (등가 구성):

```nginx
server {
    listen 443 ssl;
    server_name kb.example.com;
    ssl_certificate     /etc/letsencrypt/live/kb.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kb.example.com/privkey.pem;

    auth_basic           "Agora";
    auth_basic_user_file /etc/nginx/agora.htpasswd;   # htpasswd -B 로 생성

    # 운영 내부 전용 경로 차단
    location /metrics       { return 403; }
    location /dashboard     { return 403; }
    location /api/dashboard { return 403; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $http_host;             # #94 Host 표준: 원 Host 를 포트까지 보존
                                                      # (없으면 $proxy_host=127.0.0.1:8000 이 가고
                                                      # 브라우저 Origin 과 어긋나 업로드가 403.
                                                      # $host 는 포트를 떼므로 비표준 포트에서
                                                      # 같은 증상 — $http_host 를 쓴다)
        proxy_set_header X-Remote-User $remote_user;  # 강제 설정: 위조 사본 대체
    }
}
```

두 스니펫 모두 헤더를 **set**(append 아님)한다 — 이것이 위조 사본 제거의 실체다. 심층 방어로
face 자체도 **쓰기 요청(업로드)에서는** 같은 헤더가 2회 이상 오면 400으로 거부하고, 헤더 값이
유효하지 않아도 400이다(프록시 미설정/위조 신호 — 읽기 요청은 헤더를 무시하므로 400 프로브는
업로드로 한다). 위조 테스트(`curl -u alice -H 'X-Remote-User: mallory' …` 업로드 → 응답
**영수증**의 `identity_source: "header"` 확인 + 허브 `_kb/inbox/web/`에 방금 착지한 이벤트
frontmatter가 `source: web:alice`인지 확인 — `source`는 영수증이 아니라 inbox 이벤트에
기록된다)를 포함한 #67 신뢰 경계의 상세와
최소 스니펫은 [`deploy/README.md`](../deploy/README.md)의 "Per-user identity" 절과 정합이다 —
위 예시는 거기에 경로 차단(4)을 더한 팀 배포 완성본이다.

## 3. MCP 쓰기 — stdio-over-SSH (forced-command 필수)

에이전트의 쓰기(`kb_remember`)는 허브의 MCP stdio face로 보낸다. 레시피는 SSH가 원격에서
`agora serve`를 실행하고 stdio를 그대로 잇는 것이다:

```bash
# 팀원 머신 — Claude Code 예시 (다른 MCP 클라이언트도 stdio 명령 등록은 동일)
claude mcp add agora-team -- ssh -T agora@kb.example.com
```

`-T`는 pty를 요청하지 않는다(아래 `restrict`가 pty를 거부하므로 필수).

**forced-command + 제한 옵션은 선택이 아니라 필수다.** 일반 SSH 계정을 주면 SSH = 허브
파일시스템 전체 접근이다: `wiki/`를 직접 편집해 불변식 #2(모든 쓰기는 inbox 경유)를 우회하고,
타인의 `_kb/inbox/<writer>/`를 조작·삭제해 불변식 #3(append-only)을 우회하며, `--writer`를
바꿔 신원을 위조할 수 있다. 허브의 `authorized_keys`에서 **key당 명령과 writer를 서버가
고정**한다 (한 항목은 물리적 한 줄이어야 한다):

```
command="/ABSOLUTE/PATH/TO/uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /ABSOLUTE/PATH/TO/knowledge-repo --writer alice",restrict ssh-ed25519 AAAA...alice의-공개키... alice@laptop
command="/ABSOLUTE/PATH/TO/uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /ABSOLUTE/PATH/TO/knowledge-repo --writer bob",restrict ssh-ed25519 AAAA...bob의-공개키... bob@desktop
```

- `command="…"`: 클라이언트가 무슨 명령을 요청하든 서버가 이 argv만 실행한다(요청 명령은
  무시됨). `ssh agora@hub ls`를 시도해도 `agora serve`가 뜨는지로 검증한다.
- `restrict`(OpenSSH 7.2+): pty·포트 포워딩·agent 포워딩·X11을 일괄 차단. 구버전이면
  `no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`을 나열한다.
- **key당 `--writer` 고정**: writer 신원이 SSH 키에 바인딩된다. 팀원은 자신의 inbox
  네임스페이스(`_kb/inbox/alice/`)에만 append하게 된다. 단, 이 신원은 provenance 수준이다 —
  모든 키가 OS 계정 하나(agora)에 착지하므로 OS 수준 격리는 아니며, sshd 설정 실수 하나가
  곧 전체 FS 노출임을 전제로 검증 명령을 반드시 돌린다.

**kb_curate 노출 규율.** MCP face는 7개 도구(`kb_remember`/`kb_query`/`kb_read`/
`kb_neighbors`/`kb_context`/`kb_status`/`kb_curate`)를 전부 등록한다 — 즉
**`kb_curate`(`force` 파라미터 포함)가 키를 가진 전원에게 열리며, rate-limit은 없다**.
허브 안 동시 호출이 파손을 일으키지는 않는다 — 잠금(비블로킹 flock)에 진 호출은 대기·브레인
비용 없이 즉시 noop(`reason_lock_held`)으로 반환된다. 단, 그 noop은 "이미 다른 런이 돌고
있다"는 뜻일 뿐 요청한 큐레이션이 수행된 것이 아니고, 잠금을 잡은 호출마다 브레인(LLM) 비용이
발생한다. 운영 규율로 합의한다: 큐레이션 트리거는 허브의 `agora watch` 스케줄에 맡기고,
수동 `kb_curate`는 사전 합의된 경우로 제한한다.

읽기 항해도 같은 채널로 완결된다: `kb_query`(검색) → `kb_read`(노트 열람) →
`kb_neighbors`(링크 추적) → 재검색(#58), 그리고 `kb_context`(gold 표준 컨텍스트, §4).

## 4. 읽기 배포 — push-only 미러 + read-only clone

허브가 curated 브랜치를 미러로 push하고(#64), 팀원은 그 미러를 read-only로 clone한다.

**허브 측**: `_kb/repo.yaml`의 `backup.remote`(§1 예시)에 미러를 지정한다. 미러는 사내 git
호스팅(GitHub private/Forgejo 등)을 권장한다 — **§3의 forced-command 키로는 git 접근이
불가능하므로(의도된 것) MCP 쓰기 채널과 git 읽기 채널은 반드시 분리**되고, 호스팅의 read 권한이
그 분리를 공짜로 준다. push는 수동(`agora sync`) 또는 `backup.auto: true`로 watch 틱
큐레이션 성공 후 자동이며, 항상 **push-only·fast-forward-only·`--force` 없음**이다.
non-fast-forward 거부(미러가 앞서 있음 = 다른 머신이 push했음)는 덮어쓰지 않고 #46을
가리키는 에러로 보고된다. 서비스 매니저 아래의 비대화형 자격증명 요건(ssh agent /
credential helper)은 [`deploy/README.md`](../deploy/README.md) 참조.

```bash
uv run agora sync --repo /ABSOLUTE/PATH/TO/knowledge-repo   # 수동 push; doctor의 backup 라인에 결과 기록
```

**팀원 측**:

```bash
git clone git@git.example.com:team/kb-mirror.git ~/team-kb   # read-only로 취급
git -C ~/team-kb pull --ff-only                              # 갱신
```

clone에는 git-tracked 전부(`wiki/`·`index.md`·`raw/`·`log.md`)가 있다 — grep·Obsidian 열람
등 로컬 읽기 용도로 충분하다.

> **Footgun 1 — 로컬 클론에 `kb_remember` 금지.** 클론에 MCP face를 붙여 `kb_remember`하면
> 그 캡처는 **로컬 `_kb/inbox/`에 고립**된다: `_kb/`는 git-ignore라 push/pull 어느 쪽으로도
> 이동하지 않고, 클론에서 큐레이터를 돌리지 않는 한(§1 규율상 금지) 아무도 소비하지 않는다 —
> **조용히 유실**된다. 쓰기는 반드시 §3의 SSH MCP로 허브에 보낸다.
>
> **Footgun 2 — gold 팩은 clone으로 배포되지 않는다.** `_kb/gold/<pack>.md`도 git-ignore다.
> 파일 include(`@…/_kb/gold/default.md`)는 허브 로컬에서만 성립한다. 원격 팀원의 정식 소비
> 채널은 **MCP `kb_context`**(+ `agora://gold/{pack}` 리소스·`gold_context` 프롬프트)와
> **`GET /api/gold/{pack}`**(프록시 경유)이다 — 모든 채널이 빌드된 팩을 byte-identical로
> 서빙한다(#40).
>
> **Footgun 3 — 백업은 `_kb/`를 보호하지 않는다.** push는 git-tracked 내용만 나른다.
> **미큐레이션 inbox 이벤트·harvest 커서·gold 팩은 허브 디스크와 함께 유실될 수 있다**
> (캡처→큐레이션 사이의 잔존 창). 중요 캡처가 쌓였으면 `agora curate` 후 `agora sync`.
>
> **Footgun 4 — 미러 curated 브랜치는 보호 브랜치여야 한다.** 미러 호스트에서 팀원에게
> `push` 권한을 주면(향후 Phase-4 auth가 그 비트를 Agora writer 롤로 읽는다, ADR-0036 §2)
> 그 권한은 **미러에 raw `git push`도 허용**한다 — 큐레이터를 우회한 바이트를 curated 브랜치에
> 직접 밀어 넣어 `pull --ff-only`로 팀 전체에 복제될 수 있고(불변식 #2 우회), Agora는 이를
> 감지하지 못한다(`agora doctor`는 원격 브랜치 보호 상태를 못 본다). 미러 curated 브랜치는
> **보호 브랜치로 설정**해 **허브의 `agora sync` 배포 키만** 쓰게 하라. 팀원의 쓰기는 §3 SSH
> MCP → inbox → 큐레이터 경로로만 간다.

## 5. 비밀 취급 — redaction 경계와 금지선

**Redaction은 harvest 경계에만 존재한다.** `harvest.redact`(ADR-0023 §5)는 커넥터가 소스를
fact로 만드는 시점에 시크릿 패턴을 마스킹한다 — 그게 전부다. **직접 쓰기 경로
(`kb_remember`, 웹 업로드)는 무필터**다: 비밀을 붙여넣으면 inbox를 거쳐 curated 브랜치의
**git 이력에 영구 잔류**하고, §4의 미러 push로 팀 전체에 복제된다. 이력 재작성 없이는 제거할
수 없으며, **right-to-delete(삭제 전파) 메커니즘은 아직 저작되지 않았다**(ADR-0031, #42).
운영 규율: 자격증명·토큰·개인정보는 애초에 쓰지 않는다 — 사후 구제 수단이 없다.

**mail:/chat: 커넥터는 ADR-0031(#42)이 Accepted 되기 전까지 금지다.** 보존 기한과 삭제
전파를 결정하는 ADR이 개인·사내 커뮤니케이션 소스의 hard prerequisite다(ADR-0023 §12).

**`kind: team` 선언과 스코프 게이트.** harvester의 스코프 게이트(`check_scope`)는
`커넥터 scope == harvest.scope_lock == repo kind` 삼중 일치를 요구하며 fail-closed다:
repo `kind`가 **미선언이면 team으로 취급**되므로, personal 소스(예: `~/.claude` 메모리,
세션 커넥터)가 팀 리포로 흘러드는 것은 선언 없이도 이미 거부된다. `kind: team`을 선언하는
실익은 (a) team-스코프 커넥터(`scope: team` + `harvest.scope_lock: team`)를 명시적으로
허용할 수 있게 되는 것, (b) 리포 신원이 문서화되는 것이다. 팀 허브에는 §1 예시대로 선언한다.

## 설치 체크리스트

각 단계의 검증 명령까지 통과해야 다음으로 넘어간다.

0. **허브에 agora-kb 소스 준비** — 소스 체크아웃 후
   [`deploy/README.md`](../deploy/README.md)의 Placeholders 절차대로
   `uv sync --directory /ABSOLUTE/PATH/TO/agora-kb --extra web --extra ingest --extra metrics`
   (패키지는 미릴리스 — `uv run`이 유일한 실행 형태다; 이하의 `uv run` 명령은 체크아웃
   디렉터리에서 실행하거나 `--directory /ABSOLUTE/PATH/TO/agora-kb`를 붙인다)
   → 검증: `uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora --help` 성공
1. **허브에 repo 초기화** — `uv run agora repo init /path/team-kb --name engineering --kind team`
   → 검증: `uv run agora doctor --repo /path/team-kb` (init 상태·git 라인 정상)
2. **§1 종합 예시로 `_kb/repo.yaml` 구성** (kind/backup/curator.limits/web)
   → 검증: `uv run agora doctor --repo /path/team-kb` 가 ConfigError 없이 backup 라인 표시

   > **#96 이후 `agora doctor`는 브레인까지 판정에 포함한다.** 설정된 백엔드의 `argv[0]`이
   > PATH에 있고 실행 가능한지, `agora-ollama-brain`이라면 데몬이 `/api/tags`에 응답하는지까지
   > 확인해서, 쓸 수 없으면 `status: unhealthy` + **exit 1**이다(큐레이션이 불가능한 노드가
   > healthy로 보이면 안 되므로 의도된 동작). 따라서 **브레인이 없는 노드에서는 doctor 게이트가
   > 실패한다** — 브레인을 고치거나(doctor가 PATH에 이미 있는 헤드리스 CLI 에이전트를
   > `agora-cli-brain`으로 붙이는 복붙 가능한 블록을 출력한다, ADR-0016; Ollama는 더 무거운
   > 대안으로 함께 안내), 브레인이 없는 것이 정상인 노드(웹 전용·CI·큐레이션을 다른 곳에서
   > 하는 허브)에서는 `uv run agora doctor --repo /path/team-kb --skip-probe`로 검증한다.
   > `--skip-probe`는 브레인 도달성만 판정에서 제외하며 나머지 검사는 그대로다. 이 허브 문서의
   > 다른 `agora doctor` 검증 단계도 같은 규칙을 따른다.
3. **상시 구동 유닛 설치** (watch·web·harvest — [`deploy/README.md`](../deploy/README.md) 절차)
   → 검증: `curl -s http://127.0.0.1:8000/api/status` 응답 + 유닛 상태
   (`launchctl print` / `systemctl --user status`) + 재부팅 후 재확인
4. **프록시 구성** (§2) → 검증: `curl -u alice https://kb.example.com/api/status` 200
   (= 원 Host 보존 + `allowed_hosts` 등록이 둘 다 맞았다는 뜻; 400이면 §2 Host 표준 재확인) ·
   `curl https://kb.example.com/api/status` 401 · `curl -u alice https://kb.example.com/metrics`
   403 · `curl -u alice https://kb.example.com/api/dashboard/health` 403 · 허브 로컬에서
   `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.com' http://127.0.0.1:8000/api/status`
   가 **400**(#94 rebinding 차단) · 위조 헤더 업로드 테스트에서 응답 영수증이
   `identity_source: "header"` **그리고** 허브 `_kb/inbox/web/`에 착지한 이벤트 frontmatter가
   `source: web:alice`(§2 — `source`는 영수증에 없다). 그 업로드 `curl`은 `Origin`이 없어
   기본 정책에서 그대로 통과한다 — `require_origin: true`로 강화했다면
   `-H 'Origin: https://kb.example.com'`을 붙여야 한다.
5. **SSH forced-command 키 배포** (§3, key당 `--writer`)
   → 검증: `ssh agora@kb.example.com ls` 가 ls를 실행하지 **않고** MCP 서버가 뜸(JSON-RPC 대기)
6. **팀원 MCP 등록** — `claude mcp add agora-team -- ssh -T agora@kb.example.com`
   → 검증: 클라이언트에서 `kb_status` 호출 성공, `kb_remember` 1건 후 허브의
   `_kb/inbox/<writer>/`에 이벤트 착지 확인
7. **읽기 미러 + clone** (§4) → 검증: `uv run agora sync --repo /path/team-kb` 가
   `sync: pushed …` 출력 · 팀원 clone에서 `ls ~/team-kb/_kb` 가 **없음**(footgun 상기)
8. **gold 팩 채널** — `uv run agora gold build --repo /path/team-kb`
   → 검증: `curl -u alice https://kb.example.com/api/gold/default` 가 팩 반환(또는 MCP
   `kb_context`), `agora gold status` 가 fresh
