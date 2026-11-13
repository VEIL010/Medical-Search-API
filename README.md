# VEIL Medical-Search-API

검색 요청을 받아 검색 엔진에 넘기고 결과를 돌려주는 서버입니다. 의료기관 쪽(Interface-)이 보낸 질의 벡터 또는 암호문을 받아, 등록 DB에서 가장 비슷한 기록의 ID와 유사도 점수를 돌려줍니다.

| 저장소 | 역할 |
| --- | --- |
| Interface- | 의료 속성 입력과 검색 결과 표시 |
| Medical-Data-Pipeline | 의료 데이터 전처리와 벡터화 |
| **Medical-Search-API** | 검색 요청 처리와 검색 엔진 연동 |
| BlindMatchCore | CKKS 암호화와 HE-C³ 연산 |

## 현재 상태

| 방식 (`scheme`) | 상태 | 서버가 보는 것 | Top-K 를 고르는 곳 |
| --- | --- | --- | --- |
| `plaintext` | 동작 | 질의 벡터 평문 | 서버 |
| `ckks-hec3` | 미연결 (501) | 질의 암호문 | 의료기관 (점수 복호화 후) |

`plaintext` 는 암호화 검색 결과와 비교할 기준값입니다. 서버가 질의 벡터를 그대로 보므로 실제 환자 데이터에는 쓰지 않습니다. `ckks-hec3` 는 요청·응답 모양만 정해 두었고, BlindMatchCore 가 준비되면 `app/engines.py` 의 `CoreEngine.match` 를 채우면 됩니다.

## 이 서버가 받지 않는 것

- 원본 의료 속성(나이, 혈압 등)은 받지 않습니다. 벡터로 바꾸는 일은 의료기관 쪽에서 합니다.
- `preprocessor.joblib` 은 읽지 않습니다. 등록 DB는 `database.npy` 와 `database_ids.csv` 만 씁니다.
- 질의 내용은 로그와 오류 응답에 남기지 않습니다.

## 실행

Python 3.12 기준입니다. 먼저 Medical-Data-Pipeline 에서 `python preprocess.py --pad-to 32 --query-count 20` 을 실행해 산출물 폴더(`data/processed/heart`)를 만듭니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:VEIL_DATA_DIR = "..\Medical-Data-Pipeline\data\processed\heart"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8200
```

등록 DB 파일이 없거나 형식이 맞지 않으면 서버는 시작하지 않고 이유를 출력합니다.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `VEIL_DATA_DIR` | `data/heart` | 파이프라인 산출물 폴더 |
| `VEIL_API_KEY` | 없음 | 설정하면 `/v1/*` 요청에 `X-API-Key` 헤더가 필요합니다 |
| `VEIL_MAX_TOP_K` | `50` | `top_k` 상한 |
| `VEIL_MAX_BODY_BYTES` | `67108864` | 요청 본문 상한 |

## API

### `GET /health`

`{"ok": true}`. 인증이 필요 없습니다.

### `GET /v1/meta`

```json
{
  "service": "veil-medical-search-api",
  "version": "0.1.0",
  "dataset": {"name": "heart", "records": 283, "dimension": 32},
  "schemes": [
    {"name": "plaintext", "encrypted": false, "connected": true},
    {"name": "ckks-hec3", "encrypted": true, "connected": false}
  ],
  "limits": {"max_top_k": 50}
}
```

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
| 413 | `body_too_large` | 본문이 상한을 넘음 |
| 422 | `invalid_request` | 차원 불일치, 정규화되지 않은 벡터, NaN, `top_k` 범위 밖, 모르는 필드 |
| 501 | `engine_not_connected` | `ckks-hec3` 엔진 미연결 |

## 테스트

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

테스트는 임시 폴더에 작은 합성 DB를 만들어 씁니다. 실제 파이프라인 산출물과 맞춰 보려면 폴더를 지정합니다. 이 경우 파이프라인이 저장한 `plaintext_topk.csv` 와 순위·점수가 같은지 20개 질의 전부 확인합니다.

```powershell
$env:VEIL_TEST_DATA_DIR = "..\Medical-Data-Pipeline\data\processed\heart"
.\.venv\Scripts\python.exe -m pytest
```

## 아직 없는 것

- 암호화 검색 (BlindMatchCore 연결)
- 요청 횟수 제한. 평문 방식에서는 점수를 반복 조회해 등록 벡터를 추정할 수 있으므로, 외부에 열기 전에 필요합니다.
- TLS. 앞단 프록시에서 처리하는 것을 전제로 합니다.
- 상세 기록 조회와 접근 기록. 이 서버는 기록 ID와 점수만 돌려줍니다.
