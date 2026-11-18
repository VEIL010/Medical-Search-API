# VEIL Medical-Search-API

의료기관 쪽(Interface-)에서 전처리한 벡터 DB를 일괄 등록하고 검색하는 서버입니다. 질의 벡터 또는 암호문을 받아, 활성 DB에서 가장 비슷한 기록의 ID와 유사도 점수를 돌려줍니다. CSV 읽기·열 매핑·전처리기 학습과 보관은 Interface-의 기관 서비스가 담당합니다.

| 저장소 | 역할 |
| --- | --- |
| Interface- | 의료 속성 입력, CSV 검증·전처리, DB 등록과 검색 결과 표시 |
| Medical-Data-Pipeline | 의료 데이터 전처리와 벡터화 |
| **Medical-Search-API** | 벡터 DB 일괄 등록·버전 관리, 검색 요청 처리와 검색 엔진 연동 |
| BlindMatchCore | CKKS 암호화와 HE-C³ 연산 |

## 현재 상태

| 방식 (`scheme`) | 상태 | 서버가 보는 것 | Top-K 를 고르는 곳 |
| --- | --- | --- | --- |
| `plaintext` | 동작 | 질의 벡터 평문 | 서버 |
| `ckks-hec3` | 미연결 (501) | 질의 암호문 | 의료기관 (점수 복호화 후) |

`plaintext` 는 암호화 검색 결과와 비교할 기준값입니다. 서버가 질의 벡터를 그대로 보므로 실제 환자 데이터에는 쓰지 않습니다. `ckks-hec3` 는 요청·응답 모양만 정해 두었고, BlindMatchCore 가 준비되면 `app/engines.py` 의 `CoreEngine.match` 를 채우면 됩니다.

## 이 서버가 받지 않는 것

- 원본 의료 속성(나이, 혈압 등)은 받지 않습니다. 벡터로 바꾸는 일은 의료기관 쪽에서 합니다.
- `preprocessor.joblib` 은 읽지 않습니다. 기존 파일 기반 DB는 `database.npy` 와 `database_ids.csv`, 일괄 등록 DB는 검증된 벡터·불투명 ID·메타데이터만 저장합니다.
- 질의 내용은 로그와 오류 응답에 남기지 않습니다.

## 실행

Python 3.12 기준입니다. 먼저 Medical-Data-Pipeline 에서 `python preprocess.py --pad-to 32 --query-count 20` 을 실행해 산출물 폴더(`data/processed/heart`)를 만듭니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:VEIL_DATA_DIR = "..\Medical-Data-Pipeline\data\processed\heart"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8200
```

기존 파일 기반 검색은 그대로 사용할 수 있습니다. 파일이 일부만 있거나 형식이 맞지 않으면 서버는 시작하지 않습니다.

### Interface-에서 새 DB 등록하기

첫 등록부터 시작하려면 파이프라인 산출물이 없어도 됩니다. 인증 키를 설정하고 **한 worker**로 실행합니다. 같은 키를 Interface-의 기관 서비스에 설정해야 합니다. 브라우저에는 API 키를 전달하지 않습니다.

```powershell
# 안전하게 생성한 키를 넣고 Interface-의 기관 서비스에도 동일하게 설정합니다.
$env:VEIL_API_KEY = "<기관 서비스와 공유하는 키>"
$env:VEIL_DATASET_DIR = ".local/datasets"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8200 --workers 1
```

Interface-의 `/datasets/register`에서 CSV를 등록하고 `/datasets/`에서 등록 DB를 검색합니다. UI 설정과 CSV 형식은 [Interface- README](https://github.com/VEIL010/Interface-)를 참고하세요.

등록 키가 설정되어 있고 기존 DB 파일이 전혀 없으면 빈 서버로 시작합니다. 등록 전 `/v1/meta`의 `dataset`과 `active`는 `null`, 검색은 `409 dataset_not_registered`입니다. 등록된 스냅샷이 있으면 재시작 때 해당 버전을 우선 복원합니다. 손상된 활성 스냅샷을 기존 DB로 조용히 대체하지 않습니다.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `VEIL_DATA_DIR` | `data/heart` | 파이프라인 산출물 폴더 |
| `VEIL_API_KEY` | 없음 | 검색·메타 API 인증 키. 등록 전용 키가 없으면 DB 등록에도 사용합니다 |
| `VEIL_ENROLLMENT_API_KEY` | `VEIL_API_KEY` | DB를 교체할 권한의 전용 키. 둘 다 없으면 등록 API는 503입니다 |
| `VEIL_DATASET_DIR` | `.local/datasets` | 일괄 등록 스냅샷과 활성 버전 포인터 저장 폴더 |
| `VEIL_MAX_TOP_K` | `50` | `top_k` 상한 |
| `VEIL_MAX_BODY_BYTES` | `67108864` | 실제 수신 본문 상한. `/v1/datasets`는 이 값과 5 MiB 중 작은 값 |

`.env.example`은 설정 참고용이며 자동으로 읽지 않습니다. 키·원본 CSV·등록 벡터·기관의 전처리기는 Git에 올리지 않습니다. 기관 서비스가 다른 컴퓨터에 있다면 TLS와 접근 통제 등 배포 구성이 별도로 필요합니다. 현재 연결 검증은 loopback의 평문 테스트를 기준으로 합니다.

## API

### `GET /health`

`{"ok": true}`. 인증이 필요 없습니다.

### `GET /v1/meta`

```json
{
  "service": "veil-medical-search-api",
  "version": "0.2.0",
  "dataset": {"name": "heart", "records": 283, "dimension": 32},
  "schemes": [
    {"name": "plaintext", "encrypted": false, "connected": true},
    {"name": "ckks-hec3", "encrypted": true, "connected": false}
  ],
  "limits": {"max_top_k": 50},
  "active": null,
  "encrypted": false,
  "mode": "local_plaintext"
}
```

`active`는 기존 파일 DB일 때 `null`, 일괄 등록 후에는 아래 등록 응답의 메타데이터입니다. `dataset`은 현재 검색에 사용하는 DB이며, 일괄 등록본이 기존 파일 DB보다 우선합니다.

### `POST /v1/datasets` — 벡터 DB 일괄 등록

기관 서비스가 `X-API-Key`를 포함한 JSON으로 호출합니다. 등록 키 설정과 인증이 필수이며 브라우저의 직접 호출(`Origin` 또는 `Sec-Fetch-Site` 헤더)은 거부합니다. 파일 업로드, SQL 연결 문자열, 원본 의료 속성, 전처리기 파일은 이 API로 받지 않습니다.

```json
{
  "dataset_id": "fd86cd23-27be-43a1-8801-d798a36070ca",
  "name": "합성 테스트 DB",
  "scheme": "plaintext",
  "transformer_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "records": 2,
  "dimension": 2,
  "ids": ["R000001", "R000002"],
  "vectors": [[1.0, 0.0], [0.0, 1.0]]
}
```

예시의 해시는 형식을 보여주기 위한 값입니다. 실제로는 기관에서 학습·보관한 전처리기 파일 바이트의 SHA-256을 사용합니다. UUID는 소문자 표준 형식, ID는 영문·숫자·`_`·`-` 1~64자이며 서로 달라야 합니다. 최대 2,000행·128차원, 모든 벡터가 유한한 숫자로 구성된 L2 단위 벡터여야 합니다. 정답 열과 원본 식별자는 Interface-에서 제외합니다.

성공은 `201`입니다.

```json
{
  "active": {
    "dataset_id": "fd86cd23-27be-43a1-8801-d798a36070ca",
    "name": "합성 테스트 DB",
    "scheme": "plaintext",
    "records": 2,
    "dimension": 2,
    "transformer_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "created_at": "2026-11-18T00:00:00+00:00"
  },
  "encrypted": false,
  "mode": "local_plaintext"
}
```

등록은 기존 DB에 행을 덧붙이는 작업이 아니라 **새 버전을 활성 DB로 교체**하는 작업입니다. 먼저 불변 스냅샷을 완전히 저장하고 활성 포인터를 원자적으로 교체합니다. 실패하면 이전 활성 DB를 유지하며 이전 스냅샷도 보관합니다. 같은 UUID의 재등록은 `409 dataset_exists`입니다. 응답을 받지 못한 경우 `/v1/meta`로 활성 버전을 확인하세요. 한 서버 프로세스 안에서 등록을 직렬화하며, 다중 worker나 여러 프로세스의 공유 저장 폴더는 지원하지 않습니다.

암호화 등록은 아직 지원하지 않아 `plaintext` 외의 방식은 `501 encryption_not_connected`입니다. 평문으로 자동 전환하지 않습니다.

### `POST /v1/search` — 평문 기준 검색

요청의 `vector` 는 파이프라인과 같은 전처리를 거친 L2 정규화 벡터여야 하고, 길이는 `dataset.dimension` 과 같아야 합니다.

```json
{"scheme": "plaintext", "vector": [0.0312, -0.1187, "… 32개"], "top_k": 5}
```

```json
{
  "scheme": "plaintext",
  "encrypted": false,
  "dataset": "heart",
  "searched": 283,
  "top_k": 5,
  "results": [
    {"rank": 1, "record_id": "H00113", "score": 0.92767655},
    {"rank": 2, "record_id": "H00173", "score": 0.84618275}
  ],
  "elapsed_ms": 0.041
}
```

점수는 코사인 유사도이고 소수 8자리로 반올림합니다. 동점이면 먼저 등록된 기록이 앞에 옵니다. `top_k` 가 등록 기록 수보다 크면 전부 돌려줍니다.

**일괄 등록 DB를 검색할 때는** 전처리 버전이 섞이지 않도록 `dataset_id`와 `transformer_hash`를 함께 보냅니다. 두 값이 현재 활성 등록본과 정확히 같아야 합니다. 누락 또는 이전 버전이면 `409 dataset_changed`, 둘 중 하나만 보내면 `422`입니다. 등록에 사용한 동일 전처리기로 질의를 변환해야 합니다.

```json
{"scheme": "plaintext", "dataset_id": "fd86cd23-27be-43a1-8801-d798a36070ca", "transformer_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "vector": [1.0, 0.0], "top_k": 3}
```

등록 DB의 검색 응답에는 `dataset_id`가 추가됩니다. 기존 파일 DB는 이전 요청·응답 형식을 유지합니다. 기존 전처리기를 쓰는 화면에서 DB 교체 이후 버전 없이 검색하면, 다른 전처리 공간의 벡터끼리 비교하지 않도록 거부됩니다.

### `POST /v1/search` — 암호화 검색 (미연결)

```json
{"scheme": "ckks-hec3", "payload": "<BlindMatchCore 가 만든 질의 암호문, base64>"}
```

연결되면 다음 모양으로 응답합니다. 서버는 비밀키가 없으므로 순위를 매기지 않습니다.

```json
{"scheme": "ckks-hec3", "encrypted": true, "dataset": "heart", "searched": 283, "encrypted_scores": "<base64>", "elapsed_ms": 0.0}
```

`payload` 안의 구조(암호문, 평가 키 전달 방식)는 BlindMatchCore 규격이 정해지면 그에 따릅니다.

### 오류

모든 오류는 같은 모양입니다.

```json
{"error": {"code": "invalid_request", "message": "vector 는 32차원이어야 합니다."}}
```

| 상태 | `code` | 경우 |
| --- | --- | --- |
| 401 | `unauthorized` | API 키가 없거나 틀림 |
| 403 | `browser_request_rejected` | 브라우저가 등록 API를 직접 호출 |
| 409 | `dataset_not_registered`, `dataset_changed`, `dataset_exists` | 미등록, 전처리 버전 불일치, UUID 중복 |
| 413 | `body_too_large` | 본문이 상한을 넘음 |
| 415 | `json_required` | 등록 요청이 JSON이 아님 |
| 422 | `invalid_request` | 차원 불일치, 정규화되지 않은 벡터, NaN, `top_k` 범위 밖, 모르는 필드 |
| 422 | `invalid_vectors`, `invalid_ids` | 등록 벡터·차원·행 개수·ID 불일치 |
| 500 | `storage_failed` | 스냅샷 저장 실패. 이전 활성 DB 유지 |
| 501 | `engine_not_connected` | `ckks-hec3` 엔진 미연결 |
| 501 | `encryption_not_connected` | 암호화 DB 등록 미연결 |
| 503 | `token_not_configured` | 등록 권한 키 미설정 |

## 테스트

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

테스트는 임시 폴더에 작은 합성 DB를 만들어 씁니다. 실제 파이프라인 산출물과 맞춰 보려면 폴더를 지정합니다. 이 경우 파이프라인이 저장한 `plaintext_topk.csv` 와 순위·점수가 같은지 20개 질의 전부 확인합니다.

일괄 등록 테스트는 인증·잘못된 입력·스트리밍 본문 크기·DB 교체·이전 질의 거부·저장 실패 복구·재시작 복원·기존 검색 호환성을 검증합니다.

```powershell
$env:VEIL_TEST_DATA_DIR = "..\Medical-Data-Pipeline\data\processed\heart"
.\.venv\Scripts\python.exe -m pytest
```

## 아직 없는 것

- 암호화 검색 (BlindMatchCore 연결)
- 요청 횟수 제한. 평문 방식에서는 점수를 반복 조회해 등록 벡터를 추정할 수 있으므로, 외부에 열기 전에 필요합니다.
- TLS. 앞단 프록시에서 처리하는 것을 전제로 합니다.
- 상세 기록 조회와 접근 기록. 이 서버는 기록 ID와 점수만 돌려줍니다.
