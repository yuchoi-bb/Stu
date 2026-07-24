# a tool 분석 (NVMe 덤프 확보 도구, C++)

- 기준 커밋: {SHA}  /  최종 갱신: {YYYY-MM-DD}

> 설계 문서 §7.2 규격. Studio Step 1이 이 파일을 prompt caching 블록으로 로드한다.
> 상단 기준 커밋 SHA는 stale 검사(§7.3)에 사용되므로 반드시 유지할 것.
> `{...}`는 코드베이스 담당자가 실제 값으로 채운다. NVMe 덤프 도메인 골격은 미리 반영.
> 등록: `python -m studio.manage map-analysis a-tool {repo} docs/analysis/a-tool.md`

## 0. 도메인 개요 (생성 컨텍스트)

a tool은 NVMe 장치에서 **덤프(telemetry / vendor dump)를 확보**한다. 확보 경로 3종:

- **Host-Initiated Dump (HID)**: 호스트가 트리거. NVMe Telemetry Host-Initiated
  (Get Log Page, Log ID `0x07`, Create 비트로 새 캡처). Data Area 1/2/3 경계와
  Generation Number를 헤더에서 읽어 순차 회수.
- **Controller-Initiated Dump (CID)**: 컨트롤러가 이벤트/에러 시 캡처 →
  호스트가 Telemetry Controller-Initiated (Log ID `0x08`) 회수. 생성은 컨트롤러 주도,
  호스트는 회수·파싱만.
- **VUC via Secure Command**: Vendor Unique Command를 **Security Send(`0x81`)/
  Security Receive(`0x82`)** 로 터널링해 전달. 여러 VUC가 이미 정의돼 있으며
  (SECP/SPSP 조합), 신규 VUC 추가·기존 VUC 확장이 주 개발 대상.

> 개발자는 위 3경로 관점에서 신규 덤프 기능·신규 VUC를 추가/수정한다. 생성 코드는
> **전송 계층(passthru/security)·표준 opcode를 임의 변경하지 않고** 위 프레임워크에
> 맞춰 확장해야 한다(§7 주의사항).

## 1. 파일 구조
(경로 트리 + 각 파일 역할 한 줄씩 — 실제 트리로 대체)

```
{src/}
  transport/        NVMe passthru 전송 계층 (ioctl/libnvme/OS별 백엔드)
    nvme_passthru.{h,cpp}    admin/IO passthru 래퍼
    security_cmd.{h,cpp}     Security Send/Receive(0x81/0x82) 래퍼
  dump/
    host_initiated.{h,cpp}   HID: Telemetry Host-Initiated(0x07) 캡처·회수
    ctrl_initiated.{h,cpp}   CID: Telemetry Controller-Initiated(0x08) 회수
    telemetry_log.{h,cpp}    Telemetry 로그 헤더/Data Area 파서
  vuc/
    vuc_registry.{h,cpp}     기존 VUC 정의 테이블 (opcode/SECP/SPSP/방향)
    vuc_command.{h,cpp}      VUC 요청/응답 직렬화, secure 터널 경유 실행
  model/
    dump_artifact.{h,cpp}    회수한 덤프 산출물(메타 + 바이너리) 모델
  cli/
    main.cpp                 CLI 진입점, 서브커맨드 디스패치
{include/}                   공개 헤더
{test/}                      테스트 (§6)
```

## 2. 공개 인터페이스
(함수/클래스 시그니처, 파라미터 타입, 반환값, 오류 코드 — 실제 시그니처로 대체)

- 전송 계층
  - `class NvmePassthru { Status admin(const NvmeAdminCmd&, span<byte> data); ... };`
  - `class SecurityCmd { Status send(uint8_t secp, uint16_t spsp, span<const byte>);
     Status receive(uint8_t secp, uint16_t spsp, span<byte> out); };`
- 덤프 확보
  - `class HostInitiatedDump { Result<DumpArtifact> capture(const HidOptions&); };`
  - `class ControllerInitiatedDump { Result<DumpArtifact> collect(); };`
  - `class TelemetryLog { static Result<TelemetryHeader> parseHeader(span<const byte>);
     span<const byte> dataArea(int n) const; };`
- VUC
  - `struct VucSpec { uint8_t secp; uint16_t spsp; Direction dir; ...; };`
  - `class VucCommand { Result<VucResponse> exec(const VucSpec&, span<const byte> req); };`
  - `const VucSpec* VucRegistry::find(std::string_view name);`
- 반환/오류: `{Status/Result<T> 규약 — 성공/실패 표현, NVMe status code(SC/SCT)
  매핑 방식, 예외 사용 여부}`

## 3. 핵심 데이터 구조

- `TelemetryHeader`: `{Log ID, Generation Number, Data Area 1/2/3 last block, ...}`
- `NvmeAdminCmd`: `{opcode, nsid, cdw10..15, data_len, ...}` (passthru 전달 구조)
- `VucSpec` / `VucResponse`: `{SECP, SPSP, 방향, 페이로드 스키마}`
- `DumpArtifact`: `{경로 3종 구분(HID/CID/VUC), 메타(장치/시각/generation), 바이너리}`
- `{그 외 tool 고유 구조체}`

## 4. 빌드/실행 방법
(CI가 실제 사용하는 명령 기준 — studio-verify가 도는 명령과 반드시 일치)

- 빌드: `{예: cmake -S . -B build && cmake --build build -j}` (표준 C++{17/20})
- 실행: `{예: ./build/a-tool <subcommand> ...}`
- 장치 의존: `{실장치 필요 여부. CI는 실장치 없이 도는가 → 아래 §6 mock 규칙}`

## 5. 코딩 컨벤션

- 네이밍/포맷: `{clang-format 프로파일, 네임스페이스, 파일/클래스 네이밍}`
- 에러 처리: `{Result<T> vs 예외, NVMe status(SC/SCT) → 오류 매핑 규칙}`
- 로깅: `{로거/레벨 규칙, 민감정보(장치 시리얼 등) 마스킹 여부}`
- 엔디안/정렬: NVMe 구조체는 little-endian, `{패킹/정렬 규칙}` 준수

## 6. testcase 작성 규칙

- 프레임워크: `{예: GoogleTest}` · 위치: `{test/}` · 네이밍: `{TEST(Suite, Case)}`
- 실행: `{예: ctest --test-dir build}` (studio-verify가 도는 명령과 일치)
- **장치 없는 CI 전제 → 전송 계층 mock**: `NvmePassthru`/`SecurityCmd`를 인터페이스로
  두고 테스트는 fake 백엔드 주입(녹화된 로그/응답 바이트로 파서·VUC 직렬화 검증).
  실장치 의존 테스트는 `{태그/스킵 규칙}`으로 분리.
- 결정론 필수(§생성 프롬프트): 시간·난수·장치 상태 의존 금지, 고정 바이트 픽스처 사용.

## 7. 주의사항 (건드리면 안 되는 영역)

- **표준 opcode·전송 계층 고정**: Security Send/Receive(`0x81`/`0x82`), Get Log Page,
  admin passthru 경로는 변경 금지 — 신규 기능은 이 위에 얹는다.
- **기존 VUC opcode/SECP/SPSP 불변**: `vuc_registry`의 기존 항목은 수정하지 말고
  **추가**만. 잘못된 SECP/SPSP는 장치 미정의 동작 유발.
- **Data Area 경계·Generation Number 처리**: telemetry 회수 시 헤더 경계를 벗어난
  읽기 금지, generation 불일치 시 재캡처 규칙 준수.
- **파괴적 명령 주의**: `{format/sanitize/펌웨어 관련 opcode 등 — 덤프 tool 범위 밖,
  생성 금지}`
- `{그 외 tool 고유 제약}`
