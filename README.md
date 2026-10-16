# Alice Nation 한국어 패치

![한국어 패치 적용 타이틀 화면](images/title.jpg)

Steam **Alice Nation**(爱丽丝与彼岸, 앨리스와 피안)과 **Alice Nation Demo**의 비공식 한국어 패치입니다.
번역표와 스토리 대사, UI 고정 문구, 이미지(로고·튜토리얼·도움말)를 한국어로 옮겼습니다.
중국어 원문을 기준으로 삼았습니다. 별도 프로그램(BepInEx 등) 없이 파일 덮어쓰기만으로 적용됩니다.

## 설치

1. [Releases](../../releases)에서 게임에 맞는 zip을 받습니다.
   - 정식판: `AliceNation_KoreanPatch_v*.zip`
   - 데모: `AliceNationDemo_KoreanPatch_v*.zip` (릴리스 이름이 `demo-`로 시작)
2. 압축을 게임 폴더(`alicenation.exe`가 있는 폴더)에 풀어서 덮어씁니다.
   - Steam 라이브러리 → 게임 우클릭 → 관리 → 로컬 파일 보기
3. 게임 메뉴의 [언어]에서 **한국어**를 고릅니다(English 자리).

**제거:** Steam → 게임 속성 → 설치된 파일 → **게임 파일 무결성 검사**

게임이 업데이트되면 새 패치가 나올 때까지 중국어·영어로 나오거나 동작하지 않을 수 있습니다.

| 패치 | 게임 | 게임 빌드(Steam buildid) |
|---|---|---|
| demo-v0.9.9 | 데모 | 25599022 (게임 내 v0.9.9) |
| demo-v0.9.11 | 데모 | 25833077 (게임 내 v0.9.11) |
| demo-v0.9.12 | 데모 | 25911555 |
| v1.0.0 | 정식판 | 25960830 (게임 내 v1.0.3) |

## 저장소 구성

| 경로 | 내용 |
|---|---|
| `translations/loc.ko.json` | 번역표 (`{키: 한국어}`), 정식판·데모 공용 |
| `translations/story.ko.json` | 스토리 대사 (`{원문 본문 해시: 한국어}`), 정식판·데모 공용 |
| `translations/ui_prefab.<full\|demo>.ko.json` | 프리팹 고정 문구 (`{번들: {path_id: 한국어}}`), 게임별 |
| `meta/<full\|demo>/*.json` | 번역할 때 기준 원문의 해시. 업데이트 시 원문이 바뀐 항목을 찾는 데 씁니다. |
| `tools/alice.py` | 게임과 번역 비교(check), 번역 반영(merge), 배포 파일 빌드(build). `--target full\|demo` |

저장소에는 게임 파일과 게임 원문, 이미지를 넣지 않습니다(`work/`, 로컬 전용).
배포 zip은 도구가 사용자의 게임 파일로 만든 결과물이며, Releases에만 올립니다.

## 면책

팬 번역이며 개발사·배급사와 관계가 없습니다. 게임의 권리는 각 권리자에게 있습니다.
