# -*- coding: utf-8 -*-
"""Alice Nation / Alice Nation Demo (爱丽丝与彼岸) 한국어 패치 관리 도구.

  python tools/alice.py check   [--target T] [--game DIR]   게임과 번역 비교 -> work/<T>/todo_*.json
  python tools/alice.py merge   [--target T] FILE [FILE ...] 번역 결과 반영 + 원문해시 기록
  python tools/alice.py images  [--target T] [--game DIR]   번역 대상 이미지 원본 추출 -> work/<T>/images_src/, todo_images.json
  python tools/alice.py build   [--target T] [--game DIR] [--install] [--allow-stale]
                                                             덮어쓰기용 zip 생성 (dist/)

대상(--target): full = 정식판 Alice Nation(기본), demo = Alice Nation Demo.
  공용: translations/loc.ko.json, story.ko.json (데모 번역이 정식판에 그대로 이어짐)
        단, 같은 키인데 게임마다 원문이 다른 번역표 항목은 translations/loc.<T>.ko.json (merge 가 자동으로 나눔)
  게임별: translations/ui_prefab.<T>.ko.json, meta/<T>/, VERSION.json 의 "<T>" 항목, work/<T>/
게임 폴더: --game > 환경변수 ALICE_GAME > Steam 라이브러리에서 VERSION.json 의 steam_appid 설치 경로 자동 탐색.
번역은 게임의 "English" 슬롯에 들어간다(게임 언어 메뉴의 English 자리가 "한국어"로 바뀜).
- 번역표   : resources.assets 의 LocalizationConfig_EN (키 -> 한국어)
- 스토리   : resources.assets 의 LiluoScript 대사 본문 (언어 무관, 직접 치환)
             story.ko.json 키 = 본문(지시자 제외) 해시, 장면별로 번역이 다른 줄만 줄 전체 해시
- UI 고정문구: prefabs 번들의 TextMeshPro m_text
- 이미지   : 타이틀 로고 / 튜토리얼 아틀라스 / 도움말 / 오류 이미지 (work/images/, 저장소 밖)
- 글꼴     : 게임 내장 글꼴 중 한글이 있는 것을 모든 TMP 글꼴의 폴백으로 연결
- Addressables catalog.json 의 번들 CRC 검사를 끈다(수정한 번들을 게임이 거부하지 않게)
"""
import argparse
import base64
import datetime
import glob
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import zipfile

import UnityPy
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
from UnityPy.helpers.TypeTreeNode import TypeTreeNode
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TR_DIR = os.path.join(ROOT, "translations")   # loc / story 번역은 정식판·데모 공용
DIST_DIR = os.path.join(ROOT, "dist")      # git-ignored: 배포 zip
STORY_SRC = os.path.join(ROOT, "work", "story_src.json")   # 번역된 대사 원문 기록(정식판·데모 공용)
TARGETS = ("full", "demo")


def set_target(t):
    """정식판(full) / 데모(demo) 별로 따로 두는 것: 원문 해시(meta/<t>/), UI 번역, 작업 폴더(work/<t>/)"""
    global TARGET, META_DIR, UI_TR, LOC_OWN, WORK_DIR, IMG_DIR, SRC_IMG_DIR, ORIG_DIR
    TARGET = t
    LOC_OWN = os.path.join(TR_DIR, f"loc.{t}.ko.json")   # 같은 키인데 게임마다 원문이 다른 항목만(공용 번역표보다 우선)
    META_DIR = os.path.join(ROOT, "meta", t)
    UI_TR = os.path.join(TR_DIR, f"ui_prefab.{t}.ko.json")   # 번들 객체 ID 가 빌드마다 달라 게임별
    WORK_DIR = os.path.join(ROOT, "work", t)      # git-ignored: 게임 원문·원본 백업·이미지
    IMG_DIR = os.path.join(WORK_DIR, "images")  # title/ tutorial/ help/ misc/ (한국어 PNG)
    SRC_IMG_DIR = os.path.join(WORK_DIR, "images_src")  # 번역 대상 이미지 원본 (images 명령)
    ORIG_DIR = os.path.join(WORK_DIR, "orig")   # 게임 원본 백업 (패치본 위에서 다시 빌드할 때 사용)


set_target("full")

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"
TEXT_CLASSES = ("Config.LocalizationConfig", "Story.LiluoScript")

CJK = re.compile(r"[㐀-鿿]")
DIRTOK = re.compile(r"^(?:[A-Za-z]+:\S*\s+)+")   # 대사 앞 지시자(char:Player mood:a2 ...)
HANGUL = re.compile(r"[가-힣]")


# ---------------------------------------------------------------- utils
def jload(p, default=None):
    if not os.path.exists(p):
        return {} if default is None else default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jsave(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def h12(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha1(s).hexdigest()[:12]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def split_dir(t):
    """'char:Player mood:a2 본문' -> ('char:Player mood:a2 ', '본문')"""
    m = DIRTOK.match(t)
    if m and not CJK.search(m.group(0)):
        return m.group(0), t[m.end():]
    return "", t


def story_ko(tr_story, line):
    """스토리 번역 조회: 줄 전체 해시(같은 본문을 장면마다 다르게 번역한 경우) -> 본문 해시 순"""
    return tr_story.get(h12(line)) or tr_story.get(h12(split_dir(line)[1]))


def stem(rel):
    """Addressables 번들 파일명에서 내용 해시 제거: foo.prefab_<32hex>.bundle -> foo.prefab"""
    return re.sub(r"_[0-9a-f]{32}\.bundle$", "", os.path.basename(rel))


def loc_tr():
    """번역표 = 공용 loc.ko.json + 게임별 loc.<T>.ko.json(우선)"""
    return {**jload(os.path.join(TR_DIR, "loc.ko.json")), **jload(LOC_OWN)}


def config():
    """VERSION.json = {"full": {...}, "demo": {...}} 중 현재 대상"""
    return jload(os.path.join(ROOT, "VERSION.json")).get(TARGET, {})


def save_config(ver):
    allv = jload(os.path.join(ROOT, "VERSION.json"))
    allv[TARGET] = ver
    jsave(os.path.join(ROOT, "VERSION.json"), allv)


# ---------------------------------------------------------------- 게임 위치·구조
def steam_libraries():
    libs = [STEAM_DEFAULT]
    vdf = os.path.join(STEAM_DEFAULT, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf):
        for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="ignore").read()):
            p = m.group(1).replace("\\\\", "\\")
            if p not in libs:
                libs.append(p)
    return libs


def acf_value(acf, key):
    m = re.search(rf'"{key}"\s+"([^"]*)"', open(acf, encoding="utf-8", errors="ignore").read())
    return m.group(1) if m else None


class Game:
    """게임 폴더 구조(데이터 폴더·Addressables 경로·Unity 버전·Steam 빌드)를 자동으로 파악"""

    def __init__(self, arg):
        cfg = config()
        appid = str(cfg.get("steam_appid", ""))
        root = arg or os.environ.get("ALICE_GAME")
        self.acf = None
        if not root and appid:
            for lib in steam_libraries():
                acf = os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")
                if os.path.exists(acf):
                    root = os.path.join(lib, "steamapps", "common", acf_value(acf, "installdir"))
                    self.acf = acf
                    break
        if not root or not os.path.isdir(root):
            sys.exit(f"[오류] 게임 폴더를 찾지 못했습니다 (steam_appid={appid}). --game 으로 지정하세요.")
        self.root = os.path.abspath(root)
        if self.acf is None:   # --game 지정 시: 같은 라이브러리에서 installdir 이 일치하는 acf
            sa = os.path.dirname(os.path.dirname(self.root))
            for acf in glob.glob(os.path.join(sa, "appmanifest_*.acf")):
                if acf_value(acf, "installdir") == os.path.basename(self.root):
                    self.acf = acf
                    break
        datas = [d for d in os.listdir(self.root)
                 if d.endswith("_Data") and os.path.exists(os.path.join(self.root, d, "resources.assets"))]
        if len(datas) != 1:
            sys.exit(f"[오류] *_Data 폴더(resources.assets 포함)를 찾지 못했습니다: {self.root}")
        self.data = datas[0]
        self.aa = self.data + "/StreamingAssets/aa"
        plats = [d for d in os.listdir(os.path.join(self.root, self.aa)) if os.path.isdir(os.path.join(self.root, self.aa, d))] \
            if os.path.isdir(os.path.join(self.root, self.aa)) else []
        self.sw = self.aa + "/" + ("StandaloneWindows64" if "StandaloneWindows64" in plats else (plats[0] if plats else "StandaloneWindows64"))
        env = UnityPy.load(os.path.join(self.root, self.data, "resources.assets"))
        uv = next(iter(env.files.values())).unity_version
        self.unity = re.sub(r"c\d+$", "", uv)
        self.appid = os.path.basename(self.acf)[12:-4] if self.acf else appid
        self.buildid = acf_value(self.acf, "buildid") if self.acf else "unknown"
        self.res = self.data + "/resources.assets"

    def path(self, rel):
        return os.path.join(self.root, rel)

    def find(self, pattern):
        r = sorted(glob.glob(os.path.join(self.root, pattern), recursive=True))
        return [os.path.relpath(p, self.root).replace("\\", "/") for p in r]

    def describe(self):
        return f"{os.path.basename(self.root)} (appid {self.appid}, 빌드 {self.buildid}, Unity {self.unity})"


# ---------------------------------------------------------------- 원본 관리
# 게임 폴더 파일이 우리가 빌드한 것과 같으면(=패치 설치 상태) work/orig 의 원본을 쓰고,
# 아니면 게임 파일을 원본으로 보고 백업한다.
def orig_path(game, rel):
    gp = game.path(rel)
    built = jload(os.path.join(WORK_DIR, "built.json"))
    op = os.path.join(ORIG_DIR, rel)
    cur = sha(gp)
    if built.get(rel) == cur:
        if not os.path.exists(op):
            sys.exit(f"[오류] {rel} 가 패치본인데 원본 백업이 없습니다. Steam 무결성 검사 후 다시 실행하세요.")
        return op
    if not os.path.exists(op) or sha(op) != cur:
        os.makedirs(os.path.dirname(op), exist_ok=True)
        shutil.copy2(gp, op)
    return op


# ---------------------------------------------------------------- 타입트리
class Types:
    def __init__(self, game):
        self.gen = TypeTreeGenerator(game.unity)
        self.gen.load_local_game(game.root)
        self.cache = {}

    def nodes(self, fullname):
        if fullname not in self.cache:
            try:
                base = self.gen.get_nodes("Assembly-CSharp", fullname)
            except Exception:
                sys.exit(f"[오류] 게임 코드에서 {fullname} 클래스를 찾지 못했습니다. 텍스트 구조가 바뀌었을 수 있습니다.")
            self.cache[fullname] = TypeTreeNode.from_list(
                [TypeTreeNode(b.m_Level, b.m_Type, b.m_Name, 0, 0, m_MetaFlag=b.m_MetaFlag) for b in base])
        return self.cache[fullname]


def text_objects(env):
    """resources.assets 의 LocalizationConfig / LiluoScript 객체"""
    ms = {}
    for o in env.objects:
        if o.type.name == "MonoScript":
            try:
                t = o.read_typetree()
                ms[o.path_id] = (t.get("m_Namespace", "") + "." if t.get("m_Namespace") else "") + t.get("m_ClassName", "")
            except Exception:
                pass
    out = []
    for o in env.objects:
        if o.type.name != "MonoBehaviour" or not o.assets_file.name.endswith("resources.assets"):
            continue
        o.reset(); o.read_int(); o.read_long(); o.read_boolean(); o.align_stream(); o.read_int()
        c = ms.get(o.read_long())
        if c in TEXT_CLASSES:
            out.append((c, o))
    return out


def load_text(game, types, res_path):
    env = UnityPy.load(res_path, game.path(game.data + "/globalgamemanagers.assets"))
    objs = text_objects(env)
    found = {c for c, _ in objs}
    if set(TEXT_CLASSES) - found:
        sys.exit(f"[오류] resources.assets 에서 {sorted(set(TEXT_CLASSES) - found)} 를 찾지 못했습니다. 텍스트 위치가 바뀌었을 수 있습니다.")
    loc, story = {}, {}
    for c, o in objs:
        t = o.read_typetree(nodes=types.nodes(c))
        if c == "Config.LocalizationConfig":
            lang = t["m_Name"].split("_", 1)[1]
            for e in t["localizationInfos"]:
                loc.setdefault(e["key"], {})[lang] = e["value"]
        else:
            story[t["m_Name"]] = [l.get("content", "") for b in t["blocks"] for l in b["lines"]]
    if not any("EN" in v for v in loc.values()):
        sys.exit("[오류] LocalizationConfig_EN 이 없습니다. 언어 슬롯 구성이 바뀌었을 수 있습니다.")
    return env, objs, loc, story


def src_of(v):
    return v.get("CN") or v.get("CN_TC") or ""


# ---------------------------------------------------------------- 이미지 대상
def image_targets(game):
    """번역 대상 이미지 원본: {"title/Title_EN": (PIL, 위치정보)} 형태로 열거"""
    out = {}
    for rel in game.find(game.sw + "/images_assets_assets/addressables/images/group_*.bundle"):
        env = UnityPy.load(orig_path(game, rel))
        for o in env.objects:
            if o.type.name == "Sprite":
                s = o.read()
                if s.m_Name.startswith("Title_"):
                    out["title/" + s.m_Name] = s.image
    for rel in game.find(game.sw + "/images_assets_assets/addressables/spriteatlas/teachatlas.spriteatlas_*.bundle"):
        env = UnityPy.load(orig_path(game, rel))
        for o in env.objects:
            if o.type.name == "Sprite":
                s = o.read()
                out["tutorial/" + s.m_Name] = s.image
    for rel in game.find(game.sw + "/cgs_assets_assets/addressables/images/help*.bundle") + \
            game.find(game.sw + "/cgs_assets_assets/addressables/images/help/*.bundle"):
        env = UnityPy.load(orig_path(game, rel))
        for o in env.objects:
            if o.type.name == "Texture2D":
                t = o.read()
                if t.m_Name.startswith("Help"):
                    out["help/" + t.m_Name] = t.image
    ress = game.path(game.res + ".resS")   # 원본 백업 옆엔 .resS 가 없으므로 게임 폴더 것을 같이 로드(패치가 수정하지 않는 파일)
    env = UnityPy.load(orig_path(game, game.res), *([ress] if os.path.exists(ress) else []))
    for o in env.objects:
        if o.type.name == "Texture2D" and o.assets_file.name.endswith("resources.assets"):
            t = o.read()
            if t.m_Name == "fallbackImage":
                out["misc/fallbackImage"] = t.image
    return out


def img_hash(im):
    return h12(im.convert("RGBA").tobytes())


def image_status(game):
    meta = jload(os.path.join(META_DIR, "images.srchash.json"))
    notext = jload(os.path.join(META_DIR, "images.notext.json"))   # 글자 없음으로 확인한 원본
    done = {os.path.relpath(p, IMG_DIR).replace("\\", "/")[:-4] for p in glob.glob(os.path.join(IMG_DIR, "*", "*.png"))}
    tg = image_targets(game)
    rows = []
    for k, im in sorted(tg.items()):
        if k in done:
            st = "번역됨" if meta.get(k) == img_hash(im) else "원본변경"
        elif k in meta:
            st = "번역 파일 없음"
        elif k in notext:
            st = "글자 없음(확인됨)" if notext[k] == img_hash(im) else "원본변경(이전엔 글자 없음)"
        elif k.startswith("title/") and not k.endswith("_EN"):
            st = "참고용(EN 아님)"
        else:
            st = "신규(글자 여부 확인 필요)"
        rows.append(dict(id=k, size=list(im.size), status=st))
    missing = sorted(done - set(tg))
    return tg, rows, missing


# ---------------------------------------------------------------- 글꼴
def font_report(game):
    """번역에 쓰인 한글 vs 게임 내장 글꼴(TMP 원본 TTF) 커버리지"""
    from fontTools.ttLib import TTFont
    used = set()
    for d in (loc_tr(), jload(os.path.join(TR_DIR, "story.ko.json"))):
        for v in d.values():
            used |= set(HANGUL.findall(v))
    for m in jload(UI_TR).values():
        for v in m.values():
            used |= set(HANGUL.findall(v))
    res = []
    for rel in game.find(game.sw + "/fonts_assets_all_*.bundle"):
        env = UnityPy.load(orig_path(game, rel))
        fonts = {}
        for o in env.objects:
            if o.type.name == "Font":
                d = o.read()
                if d.m_FontData:
                    cm = TTFont(io.BytesIO(bytes(d.m_FontData))).getBestCmap()
                    fonts[o.path_id] = (d.m_Name, {chr(c) for c in cm})
        for o in env.objects:
            if o.type.name == "MonoBehaviour":
                t = o.read_typetree()
                if "m_FallbackFontAssetTable" in t:
                    src = t.get("m_SourceFontFile", {}).get("m_PathID")
                    name, chars = fonts.get(src, ("?", set()))
                    res.append(dict(asset=t["m_Name"], path_id=o.path_id, source=name, dynamic=t.get("m_AtlasPopulationMode") == 1,
                                    covered=len(used & chars), missing="".join(sorted(used - chars))))
    return used, res


# ---------------------------------------------------------------- check
def cmd_check(a):
    game = Game(a.game)
    print(f"게임: {game.describe()}")
    types = Types(game)
    _, _, loc, story = load_text(game, types, orig_path(game, game.res))
    tr_loc, meta_loc = loc_tr(), jload(os.path.join(META_DIR, "loc.srchash.json"))
    tr_story = jload(os.path.join(TR_DIR, "story.ko.json"))

    todo_loc = []
    for k, v in loc.items():
        if CJK.search(k):
            continue  # 대사 키(원문 자체가 키) -> 스토리에서 처리
        cn = src_of(v)
        if k not in tr_loc:
            st = "신규"
        elif meta_loc.get(k) != h12(cn):
            st = "원문변경"
        else:
            continue
        todo_loc.append(dict(id=k, cn=cn, en=v.get("EN", ""), jp=v.get("JA", ""), status=st, prev_ko=tr_loc.get(k)))
    gone_loc = [k for k in tr_loc if k not in loc]

    tm = {src_of(v): dict(ko=tr_loc[k], key=k) for k, v in loc.items() if k in tr_loc and src_of(v)}   # 번역 메모리(API 번역 참고용)
    en_by_text = {k: v.get("EN", "") for k, v in loc.items() if CJK.search(k)}
    todo_story, seen = [], set()
    # 번역된 대사의 원문 기록 {본문 해시: {cn, dir, script}} — 업데이트로 원문이 바뀌어 게임에서 사라져도 이전 번역을 참고하려고
    story_src = jload(STORY_SRC)
    for name, lines in story.items():
        for n, c in enumerate(lines):
            if not c or not CJK.search(c):
                continue
            d, body = split_dir(c)
            i = h12(body)
            ko = story_ko(tr_story, c)
            if ko is not None:
                tm.setdefault(body, dict(ko=ko, key=name, dir=d.strip()))
                story_src[i] = dict(cn=body, dir=d.strip(), script=name)
                continue
            if i in seen:
                continue
            seen.add(i)
            ctx = [dict(dir=split_dir(x)[0].strip(), cn=split_dir(x)[1], ko=story_ko(tr_story, x) or "")
                   for x in lines[max(0, n - 3):n] if x and CJK.search(x)]   # 앞 대사(번역 문맥)
            todo_story.append(dict(id=i, script=name, n=n, dir=d.strip(), cn=body, en=en_by_text.get(body, ""), status="신규", ctx=ctx))
    for i, s in story_src.items():   # 지금 게임에는 없는 대사의 이전 번역
        if i in tr_story:
            tm.setdefault(s["cn"], dict(ko=tr_story[i], key=s["script"] + "(이전 버전)", dir=s["dir"]))

    tr_ui, meta_ui = jload(UI_TR), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
    todo_ui, cand, seen_b, seen_p = [], [], set(), set()
    for rel in game.find(game.sw + "/prefabs_assets_assets/**/*.bundle"):
        s = stem(rel)
        seen_b.add(s)
        env = UnityPy.load(orig_path(game, rel))
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                t = o.read_typetree()
            except Exception:
                continue
            v = t.get("m_text")
            if not isinstance(v, str):
                continue
            pid = str(o.path_id)
            seen_p.add((s, pid))
            if pid in tr_ui.get(s, {}):
                tm.setdefault(v, dict(ko=tr_ui[s][pid], key=s))
                if meta_ui.get(s, {}).get(pid) != h12(v):
                    todo_ui.append(dict(bundle=s, id=pid, cn=v, status="원문변경", prev_ko=tr_ui[s][pid]))
            elif CJK.search(v) or v.strip() == "English":   # English = 언어 메뉴(한국어로 바꿀 자리)
                cand.append(dict(bundle=s, id=pid, cn=v, status="후보(대부분 자리표시자)"))
    ui_gone = sorted(set(tr_ui) - seen_b)
    ui_lost = [(s, pid, ko) for s, m in tr_ui.items() if s in seen_b for pid, ko in m.items() if (s, pid) not in seen_p]
    for s, pid, ko in ui_lost:   # 객체 ID가 바뀌어 적용 안 될 항목 -> 같은 문구를 후보에서 다시 찾아 번역
        todo_ui.append(dict(bundle=s, id=pid, cn="", status="사라진 항목(ID 변경?)", prev_ko=ko))

    _, img_rows, img_missing = image_status(game)
    used, fonts = font_report(game)
    cat_bin = game.find(game.aa + "/catalog*.bin")

    jsave(os.path.join(WORK_DIR, "todo_loc.json"), todo_loc)
    jsave(os.path.join(WORK_DIR, "todo_story.json"), todo_story)
    jsave(os.path.join(WORK_DIR, "todo_ui_prefab.json"), todo_ui + cand)
    jsave(os.path.join(WORK_DIR, "todo_images.json"), [r for r in img_rows if r["status"] != "번역됨"])
    jsave(os.path.join(WORK_DIR, "tm.json"), tm)
    jsave(STORY_SRC, story_src)
    n = lambda L, s: sum(1 for x in L if x["status"] == s)
    print(f"번역표   : 신규 {n(todo_loc, '신규')} / 원문변경 {n(todo_loc, '원문변경')} / 삭제 {len(gone_loc)}")
    print(f"스토리   : 신규 {len(todo_story)}")
    print(f"UI 고정문구: 원문변경 {len(todo_ui) - len(ui_lost)} / 사라진 항목 {len(ui_lost)} / 미번역 후보 {len(cand)}"
          + (f" / 사라진 번들 {ui_gone}" if ui_gone else ""))
    c = {}
    for r in img_rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    print(f"이미지   : {c}" + (f" / 게임에 없는 번역 이미지 {img_missing}" if img_missing else ""))
    best = max(fonts, key=lambda r: r["covered"]) if fonts else None
    if best:
        print(f"글꼴     : 번역 한글 {len(used)}자 중 {best['asset']}({best['source']}) {best['covered']}자"
              + (f" — 누락 {len(best['missing'])}자: {best['missing'][:40]}" if best["missing"] else " (전부 커버)"))
    else:
        print("글꼴     : [경고] fonts 번들/TMP 글꼴을 찾지 못함")
    if cat_bin:
        print(f"[경고] 바이너리 카탈로그 발견 {cat_bin} — catalog.json CRC 처리 방식으로는 부족할 수 있음")
    print("-> work/todo_loc.json, todo_story.json, todo_ui_prefab.json, todo_images.json")


# ---------------------------------------------------------------- merge
def cmd_merge(a):
    game = Game(a.game)
    types = Types(game)
    _, _, loc, _ = load_text(game, types, orig_path(game, game.res))
    for f in a.files:
        d = jload(f)
        base = os.path.basename(f)
        if "story" in base:
            tr = jload(os.path.join(TR_DIR, "story.ko.json"))
            for k, v in d.items():
                tr[k] = split_dir(v)[1]
            jsave(os.path.join(TR_DIR, "story.ko.json"), tr)
        elif "ui_prefab" in base:
            tr, meta = jload(UI_TR), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
            todo = {(x["bundle"], x["id"]): x["cn"] for x in jload(os.path.join(WORK_DIR, "todo_ui_prefab.json"), [])}
            for s, m in d.items():
                for pid, v in m.items():
                    tr.setdefault(s, {})[pid] = v
                    if (s, pid) in todo:
                        meta.setdefault(s, {})[pid] = h12(todo[(s, pid)])
            jsave(UI_TR, tr)
            jsave(os.path.join(META_DIR, "ui_prefab.srchash.json"), meta)
        elif "loc" in base:
            tr, own = jload(os.path.join(TR_DIR, "loc.ko.json")), jload(LOC_OWN)
            meta = jload(os.path.join(META_DIR, "loc.srchash.json"))
            others = [jload(os.path.join(ROOT, "meta", t, "loc.srchash.json")) for t in TARGETS if t != TARGET]
            for k, v in d.items():
                if k not in loc:
                    print(f"  [경고] 게임에 없는 키: {k}")
                    continue
                hs = h12(src_of(loc[k]))
                if k in own or any(k in m and m[k] != hs for m in others):
                    own[k] = v      # 다른 게임에선 원문이 달라 공용 번역을 덮으면 안 됨
                else:
                    tr[k] = v
                meta[k] = hs
            jsave(os.path.join(TR_DIR, "loc.ko.json"), tr)
            if own:
                jsave(LOC_OWN, own)
            jsave(os.path.join(META_DIR, "loc.srchash.json"), meta)
        else:
            sys.exit(f"[오류] 파일명에 loc / story / ui_prefab 중 하나가 들어가야 합니다: {f}")
        print(f"반영: {f} ({len(d)})")


# ---------------------------------------------------------------- images
def cmd_images(a):
    """번역 대상 이미지 원본을 work/images_src/ 로 추출하고, 번역 이미지(work/images/)의 기준 해시를 기록"""
    game = Game(a.game)
    tg, rows, missing = image_status(game)
    if os.path.exists(SRC_IMG_DIR):
        shutil.rmtree(SRC_IMG_DIR)
    for k, im in tg.items():
        p = os.path.join(SRC_IMG_DIR, k + ".png")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        im.save(p)
    jsave(os.path.join(WORK_DIR, "todo_images.json"), [r for r in rows if r["status"] != "번역됨"])
    if a.record:   # 현재 work/images 의 번역 이미지를 "현재 원본 기준"으로 확정
        meta = jload(os.path.join(META_DIR, "images.srchash.json"))
        for p in glob.glob(os.path.join(IMG_DIR, "*", "*.png")):
            k = os.path.relpath(p, IMG_DIR).replace("\\", "/")[:-4]
            if k in tg:
                meta[k] = img_hash(tg[k])
        jsave(os.path.join(META_DIR, "images.srchash.json"), meta)
        print("meta/images.srchash.json 갱신")
    if a.record_notext:   # 번역 이미지가 없는 나머지 대상(참고용 로고 제외)을 "글자 없음 확인"으로 기록
        notext = jload(os.path.join(META_DIR, "images.notext.json"))
        for r in rows:
            if r["status"].startswith(("신규", "원본변경(이전엔")):
                notext[r["id"]] = img_hash(tg[r["id"]])
        jsave(os.path.join(META_DIR, "images.notext.json"), notext)
        print("meta/images.notext.json 갱신")
    c = {}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    print(f"원본 {len(tg)}장 -> work/images_src/  {c}")
    if missing:
        print(f"[경고] 게임에 없는 번역 이미지: {missing}")


# ---------------------------------------------------------------- build
def build_resources(game, types, allow_stale, out_dir, stats):
    rel = game.res
    env, objs, loc, _ = load_text(game, types, orig_path(game, rel))
    tr_loc, meta_loc = loc_tr(), jload(os.path.join(META_DIR, "loc.srchash.json"))
    tr_story = jload(os.path.join(TR_DIR, "story.ko.json"))

    applied = stale = untr = 0

    def val(k):
        nonlocal applied, stale, untr
        v = loc[k]
        if CJK.search(k):                       # 대사 키 -> 스토리 번역
            ko = story_ko(tr_story, k)
            return (split_dir(k)[0] + ko) if ko else (v.get("EN") or k)
        if k in tr_loc and (allow_stale or meta_loc.get(k) == h12(src_of(v))):
            applied += 1
            return tr_loc[k]
        if k in tr_loc:
            stale += 1
        else:
            untr += 1
        return v.get("EN") or src_of(v) or k

    s_app = s_untr = 0
    for c, o in objs:
        t = o.read_typetree(nodes=types.nodes(c))
        if c == "Config.LocalizationConfig":
            if t["m_Name"] != "LocalizationConfig_EN":
                continue
            t["localizationInfos"] = [{"key": k, "value": val(k)} for k in loc]
        else:
            for b in t["blocks"]:
                for l in b["lines"]:
                    s = l.get("content", "")
                    if not s or not CJK.search(s):
                        continue
                    pre, body = split_dir(s)
                    ko = story_ko(tr_story, s)
                    if ko is None:
                        s_untr += 1
                        continue
                    l["content"] = pre + ko
                    s_app += 1
        o.save_typetree(t, nodes=types.nodes(c))

    fb = os.path.join(IMG_DIR, "misc", "fallbackImage.png")
    if os.path.exists(fb):
        for o in env.objects:
            if o.type.name == "Texture2D" and o.assets_file.name.endswith("resources.assets"):
                tx = o.read()
                if tx.m_Name == "fallbackImage":
                    tx.set_image(Image.open(fb).convert("RGBA"), target_format=tx.m_TextureFormat,
                                 mipmap_count=max(1, tx.m_MipCount or 1))
                    tx.save()

    sf = next(f for n, f in env.files.items() if n.replace("\\", "/").endswith("resources.assets"))
    data = sf.save()
    # UnityPy 저장이 드물게 잘린 파일을 내는 경우가 있어 크기·재로드로 검증
    if len(data) < os.path.getsize(game.path(rel)) * 0.9:
        sys.exit(f"[오류] resources.assets 저장 크기 이상 ({len(data):,}b)")
    out = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    _, _, loc2, story2 = load_text(game, types, out)
    left = sum(1 for L in story2.values() for c in L if CJK.search(split_dir(c)[1]))
    if len(loc2) != len(loc) or left != s_untr:
        sys.exit(f"[오류] resources.assets 재로드 검증 실패 (키 {len(loc2)}/{len(loc)}, 중국어 잔존 {left}/{s_untr})")
    stats["loc"] = dict(applied=applied, stale_skipped=stale, untranslated=untr)
    stats["story"] = dict(applied=s_app, untranslated=s_untr)
    return [rel]


PACK_ROT = {1: Image.Transpose.FLIP_LEFT_RIGHT, 2: Image.Transpose.FLIP_TOP_BOTTOM,
            3: Image.Transpose.ROTATE_180, 4: Image.Transpose.ROTATE_90}


def top_left(rect, tex_h):
    return int(math.floor(rect.x)), tex_h - int(math.floor(rect.y)) - int(math.ceil(rect.height))


def set_tex(tex, img):
    tex.set_image(img, target_format=tex.m_TextureFormat, mipmap_count=max(1, tex.m_MipCount or 1))
    tex.save()


def save_bundle(env, rel, out_dir):
    data = env.file.save(packer="lz4")
    out = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    return rel


def build_images(game, out_dir, stats):
    done, outs = [], []
    # 타이틀 로고 (English 자리 = Title_EN, 로고 전용 텍스처)
    logo = os.path.join(IMG_DIR, "title", "Title_EN.png")
    for rel in game.find(game.sw + "/images_assets_assets/addressables/images/group_*.bundle"):
        if not os.path.exists(logo):
            break
        env = UnityPy.load(orig_path(game, rel))
        objs = {o.path_id: o for o in env.objects}
        spr = next((s for s in (o.read() for o in env.objects if o.type.name == "Sprite") if s.m_Name == "Title_EN"), None)
        if spr is None:
            continue
        tex = objs[spr.m_RD.texture.path_id].read()
        canvas = Image.new("RGBA", (tex.m_Width, tex.m_Height), (0, 0, 0, 0))
        canvas.paste(Image.open(logo).convert("RGBA"), top_left(spr.m_RD.textureRect, tex.m_Height))
        set_tex(tex, canvas)
        outs.append(save_bundle(env, rel, out_dir)); done.append("title/Title_EN")
    # 튜토리얼 조각 (TeachAtlas 스프라이트 아틀라스)
    tut = {os.path.basename(p)[:-4]: p for p in glob.glob(os.path.join(IMG_DIR, "tutorial", "*.png"))}
    for rel in game.find(game.sw + "/images_assets_assets/addressables/spriteatlas/teachatlas.spriteatlas_*.bundle"):
        if not tut:
            break
        env = UnityPy.load(orig_path(game, rel))
        objs = {o.path_id: o for o in env.objects}
        atlas = next(o.read() for o in env.objects if o.type.name == "SpriteAtlas")
        rdmap = {(str(k1), k2): v for (k1, k2), v in atlas.m_RenderDataMap}
        texs, imgs = {}, {}
        for o in env.objects:
            if o.type.name != "Sprite":
                continue
            s = o.read()
            if s.m_Name not in tut:
                continue
            ad = rdmap[(str(s.m_RenderDataKey[0]), s.m_RenderDataKey[1])]
            tid = ad.texture.path_id
            if tid not in texs:
                texs[tid] = objs[tid].read(); imgs[tid] = texs[tid].image.convert("RGBA")
            ko = Image.open(tut[s.m_Name]).convert("RGBA")
            if ko.size != s.image.size:
                print(f"  [경고] 튜토리얼 {s.m_Name}: 크기 불일치 {ko.size} != 원본 {s.image.size} — 건너뜀")
                continue
            rot = PACK_ROT.get((ad.settingsRaw >> 2) & 0xF)   # 아틀라스 패킹 회전/뒤집기
            if rot is not None:
                ko = ko.transpose(rot)
            imgs[tid].paste(ko, top_left(ad.textureRect, texs[tid].m_Height), ko)  # 알파 마스크: 이웃 스프라이트 보호
            done.append("tutorial/" + s.m_Name)
        for tid, t in texs.items():
            set_tex(t, imgs[tid])
        if texs:
            outs.append(save_bundle(env, rel, out_dir))
    # 도움말 (Help 개별 번들 / 묶음 번들)
    for p in sorted(glob.glob(os.path.join(IMG_DIR, "help", "Help*.png"))):
        name = os.path.basename(p)[:-4]
        cands = game.find(game.sw + f"/cgs_assets_assets/addressables/images/help/{name.lower()}.png_*.bundle") + \
            game.find(game.sw + "/cgs_assets_assets/addressables/images/help_*.bundle")
        for rel in cands:
            src = os.path.join(out_dir, rel) if rel in outs else orig_path(game, rel)
            env = UnityPy.load(src)
            tex = next((t for t in (o.read() for o in env.objects if o.type.name == "Texture2D") if t.m_Name == name), None)
            if tex is None:
                continue
            set_tex(tex, Image.open(p).convert("RGBA"))
            if rel not in outs:
                outs.append(rel)
            save_bundle(env, rel, out_dir); done.append("help/" + name)
            break
    if os.path.exists(os.path.join(IMG_DIR, "misc", "fallbackImage.png")):
        done.append("misc/fallbackImage")   # resources.assets 단계에서 적용
    want = {os.path.relpath(p, IMG_DIR).replace("\\", "/")[:-4] for p in glob.glob(os.path.join(IMG_DIR, "*", "*.png"))}
    stats["images"] = dict(applied=len(done), missing=sorted(want - set(done)))
    return outs


def build_prefab_text(game, out_dir, stats):
    tr, meta = jload(UI_TR), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
    outs, applied, stale = [], 0, 0
    for rel in game.find(game.sw + "/prefabs_assets_assets/**/*.bundle"):
        m = tr.get(stem(rel))
        if not m:
            continue
        env = UnityPy.load(orig_path(game, rel))
        n = 0
        for o in env.objects:
            pid = str(o.path_id)
            if o.type.name != "MonoBehaviour" or pid not in m:
                continue
            t = o.read_typetree()
            if meta.get(stem(rel), {}).get(pid) != h12(t.get("m_text", "")):
                stale += 1
                continue
            t["m_text"] = m[pid]
            o.save_typetree(t); n += 1
        if n:
            applied += n
            outs.append(save_bundle(env, rel, out_dir))
    stats["ui_prefab"] = dict(applied=applied, stale_skipped=stale)
    return outs


def build_font(game, out_dir, stats):
    """한글 커버리지가 가장 높은 TMP 글꼴을 나머지 TMP 글꼴의 폴백에 추가 (참조만 추가, 객체 추가 없음)"""
    used, rep = font_report(game)
    outs = []
    by_bundle = {}
    for rel in game.find(game.sw + "/fonts_assets_all_*.bundle"):
        by_bundle[rel] = rep
    if not rep:
        stats["font"] = "fonts 번들 없음"
        return outs
    best = max(rep, key=lambda r: r["covered"])
    if best["missing"]:
        print(f"  [경고] 글꼴 {best['asset']}에 없는 한글 {len(best['missing'])}자: {best['missing'][:60]} (□로 표시될 수 있음)")
    for rel in by_bundle:
        env = UnityPy.load(orig_path(game, rel))
        added = []
        for o in env.objects:
            if o.type.name != "MonoBehaviour" or o.path_id == best["path_id"]:
                continue
            t = o.read_typetree()
            if "m_FallbackFontAssetTable" in t and best["path_id"] not in [p["m_PathID"] for p in t["m_FallbackFontAssetTable"]]:
                t["m_FallbackFontAssetTable"].append({"m_FileID": 0, "m_PathID": best["path_id"]})
                o.save_typetree(t); added.append(t["m_Name"])
        if added:
            outs.append(save_bundle(env, rel, out_dir))
        stats["font"] = dict(korean_font=best["asset"], hangul_used=len(used), hangul_missing=len(best["missing"]), fallback_added=added)
    return outs


def build_catalog(game, out_dir, stats):
    rel = game.aa + "/catalog.json"
    if not os.path.exists(game.path(rel)):
        sys.exit(f"[오류] {rel} 가 없습니다(바이너리 카탈로그일 수 있음). CRC 처리 방식을 새로 만들어야 합니다.")
    raw = open(orig_path(game, rel), encoding="utf-8").read()
    c = json.loads(raw)
    ex = base64.b64decode(c["m_ExtraDataString"])
    cnt = 0

    def mk(enc):
        def rep(m):
            nonlocal cnt
            cnt += 1
            w = len("0".encode(enc))
            n = len(m.group(1)) // w
            return '"m_Crc":'.encode(enc) + ("0" + " " * (n - 1)).encode(enc)   # 길이 보존
        return rep
    ex2 = re.sub('"m_Crc":'.encode("utf-16le") + rb"((?:\d\x00)+)", mk("utf-16le"), ex)   # UTF-16 JSON
    ex2 = re.sub(rb'"m_Crc":(\d+)', mk("utf-8"), ex2)                                       # UTF-8 JSON
    assert len(ex2) == len(ex)
    new = raw.replace(json.dumps(c["m_ExtraDataString"]), json.dumps(base64.b64encode(ex2).decode()))
    assert new != raw or cnt == 0
    out = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(new)
    stats["catalog_crc_disabled"] = cnt
    return [rel]


def cmd_build(a):
    game = Game(a.game)
    print(f"게임: {game.describe()}")
    ver = config()
    pver = ver.get("patch_version", "1.0.0")
    pkg = ver.get("package", "AliceNation")
    name = f"{pkg}_KoreanPatch_v{pver}"
    out_dir = os.path.join(DIST_DIR, name)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    stats = {}
    types = Types(game)
    files = []
    files += build_resources(game, types, a.allow_stale, out_dir, stats)
    files += build_images(game, out_dir, stats)
    files += build_prefab_text(game, out_dir, stats)
    files += build_font(game, out_dir, stats)
    files += build_catalog(game, out_dir, stats)
    files = sorted(set(files))

    zp = os.path.join(DIST_DIR, name + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(os.path.join(out_dir, rel), rel)
    ver.update(built=datetime.date.today().isoformat(), game_buildid=game.buildid, patch_version=pver, stats=stats)
    save_config(ver)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"-> {zp} ({len(files)}개 파일)")

    if a.install:
        built = jload(os.path.join(WORK_DIR, "built.json"))
        for rel in files:
            orig_path(game, rel)  # 덮어쓰기 전 원본 백업 보장
            shutil.copy2(os.path.join(out_dir, rel), game.path(rel))
            built[rel] = sha(game.path(rel))
        jsave(os.path.join(WORK_DIR, "built.json"), built)
        print("게임 폴더에 설치 완료")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    m = sub.add_parser("merge")
    m.add_argument("files", nargs="+")
    i = sub.add_parser("images")
    i.add_argument("--record", action="store_true", help="work/images 의 번역 이미지를 현재 원본 기준으로 확정")
    i.add_argument("--record-notext", action="store_true", help="번역 이미지가 없는 신규 대상을 '글자 없음 확인됨'으로 기록(직접 확인 후)")
    b = sub.add_parser("build")
    for p in (c, m, i, b):
        p.add_argument("--target", choices=TARGETS, default="full", help="full = 정식판(기본), demo = 데모")
        p.add_argument("--game", default=None)
    b.add_argument("--install", action="store_true")
    b.add_argument("--allow-stale", action="store_true")
    a = ap.parse_args()
    set_target(a.target)
    {"check": cmd_check, "merge": cmd_merge, "images": cmd_images, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
