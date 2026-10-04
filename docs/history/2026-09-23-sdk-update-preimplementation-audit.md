> Historical design/review snapshot from 2026-09-23, retained for provenance.
> Current generic setup and measured scope: [managed firmware](../10_managed_firmware.md)
> and [candidate status](../08_candidate_status.md). Lab-specific policy proposals
> below are superseded. Local acceptance-results links refer to retained internal evidence.

# SDK 무인 펌웨어 업데이트 구현 전 코드 검토

2026-09-23. 판정: **설계 보강 후 구현 진행 가능. 연구자 40대에 배포할 준비가 끝났다는 판정은 보류.**

코드와 기존 테스트만 보고 '문제없음'이라고 결론 낼 수 없었다. 기존 테스트를 통과하는
코드에서 아래 6건을 별도 재현했다. 제품 소스는 수정하지 않았으며, 이 문서와 UX 계획,
오프라인 재현 자료를 추가했다. 장갑 접근·업데이트·원격 PC 설치·배포는 수행하지 않았다.

## 검토한 정확한 대상

| 대상 | 경로 / revision | 범위 |
| --- | --- | --- |
| SDK rc7 후보 | `oglo-python-storage-worker-20260920`, `50b59f72d81d769111db4e82451933b7e0b20a9e` | `src/oglo` 15개 모듈 전체, examples/tools의 Python 전체. 합계 25개 파일 8,081줄. pyproject/CI와 관련 테스트도 확인 |
| 펌웨어 0.9.17 후보 | `oglo-hardware-0917-canary-20260921`, `8a891365016724065ca70ad08f19d73e53c08f45` | 업데이트 매니저 전체, USB 명령 dispatch/진입/부팅 검증, 패키지/서명 검증과 관련 빌드 조건 |
| 웹 업데이터 | `hardware-ops-updater-0917-20260921`, `aa80e82df55657c4582cce88ff995ec92b4901b1` | application update, USB, 후검증/재개, firmware DB·채널, UI 호출 경로, artifact 인증·runtime hash RPC |

경로의 공통 상위 디렉터리는 `/Users/beomsoo/Development/OpenGraphLabs`다.
현재 대화 cwd인 `oglo-python`은 rc3 기반 조사 작업 디렉터리다. 전달 후보 rc7 코드와
혼동하지 않고 별도 worktree의 위 commit을 기준으로 검사했다. 세 후보 checkout은 검토 시작 시 clean이었다.

SDK 소스 목록·줄 수·SHA-256은
source inventory (`acceptance-results/sdk-update-preflight-audit-20260923/sdk-reviewed-source-inventory.json`, retained internal evidence)에 저장했다.
펌웨어 저장소 전체 하드웨어 설계, hardware-ops의 무관한 앱, 모든 외부 라이브러리 구현을
전수 검토했다는 뜻은 아니다. 실기기, 다른 OS, 배포 서버의 현재 상태는 이번 검사 범위 밖이다.

## 재현한 문제

### F1 — P1: 다른 프로세스가 사용하는 포트를 SDK가 열 수 있음

- 근거: SDK `_usb.py:173`의 `open_serial()`은 배타적 열기 설정이나 프로세스 간 잠금이 없다.
  `_owner_pid()`는 `open()`이 이미 실패한 경우에만 오류 설명을 위해 호출된다.
- 재현: 실제 장갑 대신 macOS PTY를 생성했다. 별도 프로세스가 pyserial `exclusive=True`로
  포트를 잡고 있어도 실제 SDK `open_serial()`이 두 번째로 열기에 성공했다.
- 영향: 동시에 읽어서 응답을 나눠 소비하거나, handshake STOP이 기존 수집에 개입할 수 있다.
  업데이트 바이트에 다른 프로그램의 명령이 섞이지 않는다는 보장이 현재 없다.
- 수정 조건: 첫 명령 전에 장치 신원 기준 잠금, 호스트별 점유 검증과 가능한 OS 배타
  설정을 확보한다. 재부팅으로 tty가 사라져도 소유권을 유지한다. 협조적 flock만으로
  WebUSB 등 다른 모든 접근까지 차단한다고 주장하지 않는다. Mac/Linux에서 각각 충돌 시험한다.
- 증거: PTY 결과 (`acceptance-results/sdk-update-preflight-audit-20260923/sdk_port_ownership.json`, retained internal evidence),
  재현 코드 (`acceptance-results/sdk-update-preflight-audit-20260923/probes/sdk_port_ownership.py`, retained internal evidence).

### F2 — P1: 중간 전송 취소 후 일반 명령 재시도가 복구를 계속 늦출 수 있음

- 근거: 펌웨어 `.ino:3481` USB dispatcher는 바이너리 수신 중 모든 바이트를
  `feedBinaryByte()`로 보낸다. `firmware_update.cpp:247`은 잘못된 바이트에도 활동 시간을
  갱신한다. `service():351`의 만료 조건은 60초 무수신이다.
- `FW ABORT`도 그 단계에서는 텍스트 명령으로 실행되지 않는다. 웹의
  `application-update.ts:390` `abortBestEffort()`를 그대로 옮겨 쓰면 중간 수신을 취소했다고
  보장할 수 없다.
- 재현: 원본 updater C++와 원본 USB dispatcher 함수를 실행했다. ABORT가 binary parser에
  소비되고, 10초마다 GET CONFIG를 보내면 120초 후에도 수신 상태가 유지됐다.
  이후 아무 바이트도 보내지 않고 모의 시계를 60,001ms 진행하자 세션이 만료되고 CONFIG가 응답했다.
- 이 검사는 암호·flash·시간·I/O를 모의 처리한 **프로토콜 상태 검사**다. 실기기 USB 복구나
  실제 flash 쓰기/서명 검증을 증명하지 않는다. updater `.cpp/.h`는 정식 0.9.16 tag와
  검토 중인 0.9.17에서 바이트가 같음을 확인했다.
- 수정 조건: BEGIN 전부터 복구 이력을 남긴다. 전송 실패 후 이전 OUT 작업의 종료를
  확보하고, 무송신 정리 시간(예: 65초) 뒤 제한적으로 재확인한다. 재실행 시에도 이 상태를
  존중한다. 이 경로를 USB 스택 자체가 먹통인 경우의 해결책으로 오인하지 않는다.
- 증거: 상태 재현 결과 (`acceptance-results/sdk-update-preflight-audit-20260923/firmware_receive_recovery.json`, retained internal evidence),
  C++ 재현 (`acceptance-results/sdk-update-preflight-audit-20260923/probes/firmware_receive_recovery.cpp`, retained internal evidence),
  0.9.16/0.9.17 일치 검사 (`acceptance-results/sdk-update-preflight-audit-20260923/updater-source-equivalence.json`, retained internal evidence).

### F3 — P1: 웹 업데이트의 USB 쓰기/종료 대기에는 전체 기한이 없음

- 근거: `application-update.ts:425`, `:447`, `:484`에서 `writer.write()`가 끝나야 응답 timeout이
  시작된다. `:521`의 `closeBoard()`도 완료를 기다린다.
- 재현: 실제 업데이트 함수를 사용하고 Web Serial 경계만 모의 처리했다. BEGIN write가
  resolve/reject하지 않는 경우와 COMMIT 성공 후 close가 끝나지 않는 경우 모두,
  모의 시간 10분을 진행해도 업데이트 Promise가 미완료였다.
- 이는 실제 브라우저/장갑이 10분 멈췄다는 관측이 아니라 **멈추는 I/O를 가정했을 때
  호스트 함수가 기한 내 실패하지 않는다는 재현**이다.
- 수정 조건: SDK 이식 시 open/write/read/abort/close/rebind 전체에 기한과 취소 수단을 둔다.
  기다리기만 중단하고 살아 있는 쓰기 작업 뒤에서 재시작하지 않는다. 취소 불가능한 OS 호출은
  작업 프로세스 격리와 재확인으로 처리하며, 정리 불가능 시 해당 장치 자동 쓰기를 중단한다.
- 증거: Vitest 결과 (`acceptance-results/sdk-update-preflight-audit-20260923/web-blocked-io.log`, retained internal evidence),
  재현 테스트 (`acceptance-results/sdk-update-preflight-audit-20260923/probes/application-update-blocked-io.test.ts`, retained internal evidence).

### F4 — P2: 긴 녹화의 샘플 단위 replay가 O(N²) 계산을 수행함

- 근거: SDK `_replay.py:584`, `:606`, `:629` 및 각 다음 fallback 표현.
  `dict.get(key, np.rint(전체 host_t 배열 ...))`의 기본값은 키가 있어도 평가된다.
  이 표현이 샘플 루프 안에 있어 nanosecond 열이 이미 있는 녹화도 매 샘플마다
  전체 배열을 두 번 다시 계산한다.
- 재현: 각 모달리티 256행을 재생했을 때 `np.rint` 512회, 배열 원소 131,072개를 처리했다.
  정상이라면 이미 있는 ns 열을 그대로 참조하면 된다.
- 영향: 길이가 늘수록 샘플 단위 replay 비용이 급격히 증가한다. 저장된 데이터가 손상됐다는
  뜻은 아니고 `arrays()`/`summary()`가 동일한 반복 계산을 한다는 뜻도 아니다.
- 수정 조건: 필요한 시간 열을 루프 밖에서 한 번 선택/생성한다. 실제 ns 열이 있을 때
  fallback 계산 0회, legacy fallback도 모달리티당 상수 횟수라는 회귀 검사를 둔다.
- 증거: replay/rate 결과 (`acceptance-results/sdk-update-preflight-audit-20260923/sdk_replay_and_rate.json`, retained internal evidence),
  재현 코드 (`acceptance-results/sdk-update-preflight-audit-20260923/probes/sdk_replay_and_rate.py`, retained internal evidence).

### F5 — P2: 수신 정지 후에도 rates_seen이 예전 Hz를 표시함

- 근거: SDK `_stream.py:45`는 새 샘플이 올 때만 오래된 시각을 버린다.
  `hz:56` getter는 현재 시각을 보지 않는다.
- 재현: 2ms 간격 두 샘플 후 모의 시계를 약 90초 진행해도 약 500Hz를 반환했다.
- 영향: 이 값을 현재 수신 정상 여부로 표시하는 앱은 실제 상태를 잘못 보여줄 수 있다.
  `record()`의 별도 5초 무수신 감지까지 무효라는 뜻은 아니다.
- 수정 조건: 조회 시에도 창을 만료시키고 마지막 수신 나이/실제 측정 창을 따로 검사한다.
  `acceptance._rate()`도 처음·마지막 도착 샘플 사이의 평균이므로 그것만으로 측정 창의
  앞·뒤 무수신 시간을 증명하지 않는다. 업데이트 후 준비 판정에 캐시된 Hz만 사용하지 않는다.
- 증거: F4와 같은 재현 파일.

### F6 — P2: 공개 양손 예제는 한쪽 실패를 다른 쪽 종료까지 숨길 수 있음

- 근거: `examples/04_two_hands.py:26`이 왼손 future부터 기다리고, 동료 recorder를
  중단시키는 공유 stop event도 없다.
- 재현: 실제 예제 파일을 fake glove/실제 ThreadPoolExecutor로 실행했다. 오른손이
  즉시 실패했지만 왼손을 끝내기 전에는 호출자에게 실패가 전달되지 않았다.
- 영향: 기본 60초 예제를 장시간 수집으로 바꾼 연구자는 한 손이 실패한 사실을 늦게 알 수 있다.
- 수정 조건: `as_completed()`로 먼저 끝난 작업을 관찰하고, 실패 시 다른 손에 stop event를
  전달한 뒤 각 partial episode를 남긴다. `acceptance.py`에는 이미 이 패턴이 있으므로 예제에도 맞춘다.
- 증거: 양손 실패 결과 (`acceptance-results/sdk-update-preflight-audit-20260923/two_hands_failure.json`, retained internal evidence),
  재현 코드 (`acceptance-results/sdk-update-preflight-audit-20260923/probes/two_hands_failure.py`, retained internal evidence).

## 새 SDK 설계에 반드시 반영할 계약

아래는 이미 출시된 자동 업데이트 기능의 버그 목록이 아니다. **자동 업데이트 자체가 아직 없으므로**
구현 전에 확정해야 할 조건이다.

1. `connect(serial=...)`은 현재 여러 후보에 handshake한 뒤 serial을 확인한다.
   내부 `_connect_usb_port()`에 무조건 업데이트를 넣으면 비대상을 변경할 수 있다.
   '후보 조사 → 명확한 대상 확정 → 두 손 사전 검사 → 쓰기'를 분리한다.
2. `connect_pair()`는 현재 정확히 두 USB 장치만 허용한다. 20쌍의 전체 inventory와
   여러 쌍이 꽂힌 호스트의 명시적 serial 선택은 새 API/정책으로 정의한다.
3. LINK PING worker가 활성화된 일반 stream 세션과 업데이트 세션을 분리한다.
   현재 FW 명령 차단을 제거하는 방식으로 구현하지 않는다.
4. 전체 application 파일 해시와 `running_image_sha256`은 다르다. 검토 대상 0.9.17은
   파일 `bcfdb9944e27bc38289d44edb6b1a6d6c3838244fa805723c8dd2ee3a8c4a3ad`,
   실행 digest `eddf0ca99dcd929e202464d2a9c311923e895bee95fd7aa0c5dd7ec013a01615`다.
   기존 웹의 최종 서버 RPC는 실행 digest를 비교한다. SDK가 UI 검증 함수만 복사해
   이 검증을 빠뜨리거나 파일 해시와 실행 해시를 직접 비교하면 안 된다.
5. 같은 버전 재설치·다운그레이드는 현 펌웨어가 거부한다. 0.9.17 시험판과 정식판의
   해시가 다를 때 자동 덮어쓰기한다고 약속하면 안 된다. 실제 해시 일치일 때만 skip한다.
6. 기본 웹 후검증이 보존 비교하는 필드는 identity/일부 설정/GET ZERO다.
   이를 전체 CONFIG와 모든 런타임 설정의 보존 증거로 확대하지 않는다.
   특히 현재 SDK는 읽기 전용 IMU period를 얻지 못해 세션에서 직접 설정한 값만 기억한다.
7. artifact URL 서비스는 인증·station 권한·설치 시도와 묶여 있고 URL은 60초 만료다.
   무인 SDK에는 승인된 사전 배포 묶음 또는 별도 headless 인증 계약이 필요하다.
   관리자 credential을 SDK에 넣거나 기존 설치 원장을 무단 우회하지 않는다.
8. 현재 녹화 meta는 `fw_rev`만 기록하며 실제 실행 해시·USB 신원·정책 버전을 자동 저장하지 않는다.
   업데이트 후 읽은 검증 결과를 녹화 시작 스냅샷에 추가하고 replay에서 보존해야 한다.
9. 마지막 chunk ACK 유실 뒤에는 무조건 재전송하지 않는다. 현 펌웨어는 마지막 chunk 뒤
   바이너리 모드를 벗어난다. 웹에는 이 예외 처리가 이미 있으므로 SDK에서도 유지한다.
10. 첫 부팅 rollback 검증은 태스크/스캔 진행 확인이다. 호스트가 확인한 전체 센서 품질이나
    장시간 안정성 판정이 아니다. `rollback_supported`와 실제 앱 업데이트 동작을 실기기로 확인한다.

관련 공식 API 조건은 [Espressif OTA](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/system/ota.html)와
[pyserial API](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)를 확인했다.
이 문서의 재현 결론은 위 고정 checkout의 코드와 검사 결과에 근거한다.

## 실행한 검사와 정확한 한계

| 검사 | 결과 | 한계 |
| --- | --- | --- |
| SDK `python -m pytest -q`, Python 3.14.0/macOS | **509 passed, 1 skipped, 11 deselected**, 67.74초 | OpenCV 미설치로 camera 테스트 모듈 skip. 11개 hardware 테스트 제외. 다른 OS/Python 조합은 이번에 실행하지 않음 |
| 펌웨어 `unittest discover .../tests` | **272건 중 271 성공, 1 skip**, 4.927초 | skip은 새 빌드 ELF/도구 경로를 지정해야 하는 검사. 새 전체 펌웨어 빌드나 실장갑 flash는 수행하지 않음 |
| 웹 `pnpm --filter @hardware-ops/oglo-updater test` | **70 passed / 11 files** | 실제 브라우저 USB·Supabase 배포 상태 검사 아님 |
| 이번 blocked-I/O Vitest | **6 passed** | 기존 4개 시나리오 + 새 2개 재현. 위 70개와 겹치므로 76개 독립 검사로 합산하지 않음 |
| PTY·원본 C++ 상태·replay/rate·양손 예제 재현 | 모두 예상한 취약 동작을 재현 | PASS는 버그 재현 성공이지 수정 완료가 아님 |
| rc7 sdist → wheel 빌드 | 성공 | 발견한 문제가 그대로 들어 있는 감사용 artifact. 배포용 승인 아님 |
| 별도 target 디렉터리에 wheel 설치 후 `check_installed.py`, CLI help | 15개 모듈 원본 일치 / 정상 종료 | 기본 Python 환경의 SDK를 교체하지 않음 |

빌드 artifact/checksum과 설치 검증은
감사 자료 폴더 (`acceptance-results/sdk-update-preflight-audit-20260923/`, retained internal evidence)에 있다.
재현 실행 안내 (`acceptance-results/sdk-update-preflight-audit-20260923/README.md`, retained internal evidence)도 남겼다.
기존 테스트 결과는 요약을 보존했으며, 모든 suite의 전체 원시 stdout 로그를 저장한 것은 아니다.
추가 재현 결과와 blocked-I/O 로그는 별도 파일로 남겼다.

## 다음 실행 순서와 완료 기준

1. SDK F1/F4/F5/F6 수정 및 각 재현을 회귀 검사로 편입한다. F1의 OS별 배타성은 실기기에서 확인한다.
2. F2/F3를 포함한 상태 머신을 전용 업데이트 엔진으로 구현한다. BEGIN 전 durable journal,
   무송신 복구, 제한 시간, 재부팅 중 잠금, 실제 신원/해시 재확인을 구현한다.
3. 이미지·정책 고정, 보정 비교, 후검증, 40대 inventory를 연결한다. `latest` 자동 추종은 없다.
4. 원래 정식 **0.9.16** 실장갑 한 쌍을 시작점으로 앱 업데이트 → 자동 재부팅 → 동일 장치 재연결 →
   실행 해시 → 보정/설정 → 세 모달리티 수신 → 재실행 skip을 Mac/Linux에서 확인한다.
5. READY·중간 ACK·마지막 ACK·COMMIT 응답 유실, 전송 중 종료/재실행, 포트 충돌,
   한 손만 완료, 호스트 재부팅, USB 재인식 변경을 시험한다. 지원하는 복구 경로와
   수동 재연결이 필요한 경로를 분명하게 구분한다.
6. 연구실 R-00065/L-00067에서 먼저 검증하고, 실제 수집 시작/끝·수신·저장 기록을 갖춘
   연속 장시간 양손 검사를 완료한 뒤 나머지 38대에 확대한다.

이번 검토는 누락을 줄이기 위한 코드·재현 검사다. 미래 오류가 전혀 없다는 보증이나
이전 field USB 정지의 원인이 위 6건이라는 판정은 아니다. 특히 기존 ROM flash 성공은
0.9.16의 응용 프로그램 업데이트와 자동 재부팅 경로의 실기기 합격을 대신하지 못한다.
