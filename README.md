# Testvector Server (Internal)

본 프로젝트는 Rebellions ATOM NPU를 사용하는 서버에서 워크로드를 실행하고 모니터링하기 위한 Flask 기반의 내부용 서버입니다. 기존 `web_server.py`를 모듈화하여 성능과 유지보수성을 개선했습니다.

## 1. 프로젝트 개요
- Rebellions ATOM NPU 전용 워크로드 실행 및 상태 모니터링.
- 실시간 NPU 시스템 정보(온도, 전력, 사용률 등) 제공.
- 백그라운드 작업 관리 및 실행 로그 조회 기능.

## 2. 사전 요구사항
- **하드웨어**: Rebellions ATOM NPU가 장착된 서버
- **소프트웨어**:
  - Python 3.8+
  - Rebellions 소프트웨어 스택 (`rbln-smi`, `rbln` CLI 설치 필수)
  - `sudo` 권한 (NPU 센서 정보 조회 및 시스템 제어 시 필요)

## 3. 빠른 시작
```bash
# 1. 저장소 복제 (또는 파일 복사)
git clone <repository-url>
cd testvector-server

# 2. 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate

# 3. 패키지 설치
pip install -r requirements.txt

# 4. 환경변수 설정
cp .env.example .env  # .env 파일 생성 후 환경에 맞춰 수정

# 5. 개발 서버 실행
python run.py
```

## 4. 환경변수 설정 (.env)
`app/config.py`에서 사용하는 주요 환경변수 목록입니다.

| 변수명 | 설명 | 기본값 |
|--------|------|--------|
| `WORKLOAD_BASE_DIR` | 워크로드 파일(.bin)이 위치한 기본 디렉토리 | `/home/rebellions/yeun/testvector/cr13/v3.2.0` |
| `JOBS_DIR` | 작업 상태 및 결과가 저장되는 디렉토리 | `./jobs` |
| `RUN_SCRIPT` | 워크로드 실행 시 사용할 셸 스크립트 경로 | `./run_workloads.sh` |
| `LOG_DIR` | 실행 로그 파일이 저장되는 디렉토리 | `./logs` |
| `HOST` | 서버 바인딩 호스트 | `0.0.0.0` |
| `PORT` | 서버 포트 번호 | `5000` |
| `DEBUG` | 디버그 모드 여부 (`true`/`false`) | `false` |
| `POWEROFF_SUDO_PASSWORD` | sudo 명령 실행을 위한 패스워드 (필요 시) | (empty) |

## 5. 실행 방법

### 개발 환경
```bash
python3 run.py
```

### 운영 환경 (Gunicorn)
운영 환경에서는 안정성을 위해 Gunicorn 사용을 권장합니다.

```bash
gunicorn --workers 1 -b 0.0.0.0:5000 'run:app'
```

> **주의**: 반드시 **`--workers 1`** 설정을 사용해야 합니다. `JobManager`가 인메모리(in-memory) 방식으로 작업을 관리하므로, 멀티 워커 사용 시 작업 상태 공유가 불가능합니다.

### 부팅 시 자동 실행 (systemd)

PC가 켜질 때마다 서버가 자동으로 실행되도록 systemd 서비스를 등록합니다.

**1. 서비스 파일 생성:**
```bash
sudo nano /etc/systemd/system/workload-server.service
```

**2. 아래 내용을 붙여넣기 (경로는 환경에 맞게 수정):**
```ini
[Unit]
Description=Testvector Workload Server
After=network.target

[Service]
Type=simple
User=rebellions
WorkingDirectory=/home/rebellions/testvector-server
ExecStart=/home/rebellions/testvector-server/venv/bin/python web_server.py
Restart=always
RestartSec=3
Environment=PATH=/home/rebellions/testvector-server/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

> **경로 수정 가이드:**
> - `User`: 리눅스 사용자명
> - `WorkingDirectory`: 프로젝트 클론 경로
> - `ExecStart`: venv 내 python 경로 + web_server.py
> - venv 경로 확인: `which python` (venv 활성화 상태에서)

**3. 서비스 등록 및 시작:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable workload-server.service   # 부팅 시 자동 실행 등록
sudo systemctl start workload-server.service    # 지금 바로 시작
```

**4. 상태 확인:**
```bash
sudo systemctl status workload-server.service
```

**5. 유용한 명령어:**
```bash
sudo systemctl restart workload-server.service  # 재시작 (코드 업데이트 후)
sudo systemctl stop workload-server.service     # 중지
journalctl -u workload-server.service -f        # 실시간 로그 확인
```

## 6. API 요약

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/health` | 서버 상태 체크 |
| GET | `/api/system-info` | NPU 시스템 정보 (온도, 전력, 사용률 등) 조회 |
| GET | `/api/workload-folders` | 사용 가능한 워크로드 폴더 목록 조회 |
| GET | `/api/workloads` | 특정 폴더 내 워크로드 파일(.bin, .sh) 목록 조회 |
| POST | `/api/run` | 워크로드 실행 요청 |
| GET | `/api/job/<id>` | 특정 작업의 상태 및 결과 조회 |
| GET | `/api/job/<id>/logs` | 작업 실행 로그 실시간 조회 (offset 기반) |
| POST | `/api/job/<id>/cancel` | 실행 중인 작업 취소 |
| GET | `/api/job/<id>/download` | 작업 완료 후 로그 파일 다운로드 |

### 주요 API 응답 예시
- **POST `/api/run`**
  ```json
  {"success": true, "job_id": "job_20260430_123456"}
  ```
- **GET `/api/job/<id>`**
  ```json
  {
    "success": true, 
    "job": {
      "job_id": "...",
      "status": "completed",
      "progress": {...},
      "result": "success",
      ...
    }
  }
  ```

## 7. 프로젝트 구조
```text
testvector-server/
├── app/
│   ├── services/
│   │   ├── job_manager.py  # 작업 상태 및 생명주기 관리
│   │   ├── sensor.py       # NPU 센서 데이터(rbln-smi 등) 수집
│   │   └── workload.py     # 워크로드 실행 로직
│   ├── config.py           # 환경변수 및 설정 관리
│   ├── routes.py           # API 엔드포인트 정의
│   └── __init__.py         # Flask 앱 팩토리
├── jobs/                   # 작업 상태 파일 저장소
├── logs/                   # 실행 로그 및 배치 스크립트
├── templates/              # 대시보드 UI (HTML)
├── requirements.txt        # 의존성 목록
├── run.py                  # 진입점 스크립트
└── run_workloads.sh        # 실제 NPU 실행 래퍼 스크립트
```

## 8. 개발 및 테스트
Pytest를 사용하여 단위 테스트를 실행할 수 있습니다.
```bash
# 전체 테스트 실행
python3 -m pytest tests/ -v
```

## 9. 주의사항
1. **Gunicorn 워커 수**: 위에서 언급했듯이 `JobManager`의 상태 공유 문제로 인해 반드시 `workers=1`로 실행해야 합니다.
2. **하드웨어 의존성**: 본 서버는 Rebellions NPU 하드웨어와 `rbln-smi` 도구에 의존합니다. 하드웨어가 없는 환경에서는 일부 API가 정상 작동하지 않을 수 있습니다.
3. **API 계약**: 외부 클라이언트(예: `logs/run_power500_batch.py`)와의 연동을 위해 기존 API 경로 및 응답 구조를 변경할 때 주의가 필요합니다.

## 10. 웹페이지 사용 설명서

브라우저에서 `http://<서버IP>:<포트>` 로 접속합니다.

### 화면 구성

웹페이지는 두 개의 탭으로 구성되어 있습니다.

- **📋 워크로드 실행** — 벡터 선택, 실행 대기열 관리, 실행 및 모니터링
- **📊 실시간 그래프** — NPU 온도/전력/사용률 실시간 차트

상단에는 NPU 시스템 정보(온도, 전력, 사용률 등)가 실시간으로 표시됩니다.

### 워크로드 실행 탭

#### 왼쪽 패널 (상/하 분리)

| 영역 | 설명 |
|------|------|
| **실행 대기** (상단) | Run 버튼 클릭 시 실행될 대기열. 위에서부터 순차 실행 |
| **버튼 영역** (중간) | 추가/제거/그룹추가/그룹제거/순서변경 버튼 |
| **전체 벡터** (하단) | 현재 선택된 워크로드 폴더 내 .bin 파일 목록 |

#### 벡터 추가/제거 흐름

1. **하단 전체 벡터** 목록에서 체크박스로 원하는 벡터 선택
2. **▲ 추가** 버튼 → 실행 대기 목록에 단건으로 추가
3. **▲ 그룹추가** 버튼 → 같은 종류의 벡터끼리 묶어서 그룹으로 추가 (병렬 실행됨)
4. 실행 대기 목록에서 체크 후 **▼ 제거** 또는 **▼ 그룹제거** → 대기열에서 삭제
5. **🔼 위로** / **🔽 아래로** → 실행 순서 변경
6. **Clear** → 대기열 전체 비우기

#### 그룹 추가 규칙

- `bert`, `retinanet`, `resnet` 이 포함된 벡터만 그룹 추가 가능
- 같은 종류끼리만 그룹으로 묶을 수 있음 (예: bert 4개 → OK, bert + retinanet → 불가)
- 그룹 내 벡터는 병렬로 동시 실행됨

#### 오른쪽 패널

| 영역 | 설명 |
|------|------|
| **실행 설정** | 워크로드 폴더 선택, 실행 시간(초) 입력, Run/Cancel 버튼 |
| **진행 상황** | 실행 중인 작업의 진행률 바 + 현재 실행 중인 벡터명 표시 |
| **로그 출력** | 실시간 로그 스트리밍 |

#### 실행 흐름

1. 워크로드 폴더 선택 (드롭다운)
2. 전체 벡터에서 원하는 벡터 선택 → 추가/그룹추가로 대기열 구성
3. 실행 시간(초) 입력
4. **Run** 클릭 → 대기열 순서대로 실행 시작
   - 단건 항목: 순차 실행 (앞 작업 완료 후 다음 실행)
   - 그룹 항목: 그룹 내 벡터 병렬 실행
5. 진행 상황에서 완료 수/전체 수 + 현재 실행 중인 작업 확인
6. **Cancel** 클릭 → 실행 중인 모든 작업 취소

#### 옵션

| 옵션 | 설명 |
|------|------|
| **로그 자동 저장(서버)** | 실행 로그를 서버의 logs/ 디렉토리에 자동 저장 |
| **완료시 자동 다운로드(로컬)** | 단건 실행 완료 시 로그 파일을 브라우저로 자동 다운로드 |

### 기타 기능

- **⏻ Power Off**: 서버 PC 원격 종료 (확인 2회 필요)
- **시스템 정보 갱신 주기**: 상단의 슬라이더로 1~3초 간격 조절
