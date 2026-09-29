# -*- coding: utf-8 -*-
"""Alice Nation Demo(爱丽丝与彼岸) 한국어 패치 관리 도구.

  python tools/alice.py check  [--game DIR]          게임과 번역 비교 -> work/todo_*.json
  python tools/alice.py merge  FILE [FILE ...]        번역 결과 반영 + 원문해시 기록
  python tools/alice.py build  [--game DIR] [--install] [--allow-stale]
                                                      덮어쓰기용 zip 생성 (dist/)

번역은 게임의 "English" 슬롯에 들어간다(게임 언어 메뉴의 English 자리가 "한국어"로 바뀜).
- 번역표   : resources.assets 의 LocalizationConfig_EN (키 -> 한국어)
- 스토리   : resources.assets 의 LiluoScript 대사 본문 (언어 무관, 직접 치환)
             story.ko.json 키 = 본문(지시자 제외) 해시, 장면별로 번역이 다른 줄만 줄 전체 해시
- UI 고정문구: prefabs 번들의 TextMeshPro m_text
- 이미지   : 타이틀 로고 / 튜토리얼 아틀라스 / 도움말 (work/images/, 저장소 밖)
- 글꼴     : 게임 내장 Lolita(한글 2350자)를 모든 TMP 글꼴의 폴백으로 연결
- Addressables catalog.json 의 번들 CRC 검사를 끈다(수정한 번들을 게임이 거부하지 않게)
"""
import argparse
import base64
import datetime
import glob
import hashlib
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
TR_DIR = os.path.join(ROOT, "translations")
META_DIR = os.path.join(ROOT, "meta")
WORK_DIR = os.path.join(ROOT, "work")      # git-ignored: 게임 원문·원본 백업·이미지
DIST_DIR = os.path.join(ROOT, "dist")      # git-ignored: 배포 zip
IMG_DIR = os.path.join(WORK_DIR, "images")  # title/ tutorial/ help/ misc/ (한국어 PNG)
ORIG_DIR = os.path.join(WORK_DIR, "orig")   # 게임 원본 백업 (패치본 위에서 다시 빌드할 때 사용)

DEFAULT_GAME = r"C:\Program Files (x86)\Steam\steamapps\common\alicenationDemo"
STEAM_APPID = "4556260"
UNITY_VER = "2022.3.62f3"
DATA = "alicenation_Data"
AA = DATA + "/StreamingAssets/aa"
SW = AA + "/StandaloneWindows64"
PKG = "AliceNationDemo_KoreanPatch"

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
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def story_ko(tr_story, line):
    """스토리 번역 조회: 줄 전체 해시(같은 본문을 장면마다 다르게 번역한 경우) -> 본문 해시 순"""
    return tr_story.get(h12(line)) or tr_story.get(h12(split_dir(line)[1]))


def split_dir(t):
    """'char:Player mood:a2 본문' -> ('char:Player mood:a2 ', '본문')"""
    m = DIRTOK.match(t)
    if m and not CJK.search(m.group(0)):
        return m.group(0), t[m.end():]
    return "", t


def stem(rel):
    """Addressables 번들 파일명에서 내용 해시 제거: foo.prefab_<32hex>.bundle -> foo.prefab"""
    return re.sub(r"_[0-9a-f]{32}\.bundle$", "", os.path.basename(rel))


def game_build(game):
    acf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(game))), f"appmanifest_{STEAM_APPID}.acf")
    if os.path.exists(acf):
        m = re.search(r'"buildid"\s+"(\d+)"', open(acf, encoding="utf-8", errors="ignore").read())
        if m:
            return m.group(1)
    return "unknown"


# ---------------------------------------------------------------- 원본 관리
# 게임 폴더 파일이 우리가 빌드한 것과 같으면(=패치 설치 상태) work/orig 의 원본을 쓰고,
# 아니면 게임 파일을 원본으로 보고 백업한다.
def orig_path(game, rel):
    gp = os.path.join(game, rel)
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


def find_rel(game, pattern):
    r = sorted(glob.glob(os.path.join(game, pattern), recursive=True))
    return [os.path.relpath(p, game).replace("\\", "/") for p in r]


# ---------------------------------------------------------------- 타입트리
class Types:
    def __init__(self, game):
        self.gen = TypeTreeGenerator(UNITY_VER)
        self.gen.load_local_game(game)
        self.cache = {}

    def nodes(self, fullname):
        if fullname not in self.cache:
            base = self.gen.get_nodes("Assembly-CSharp", fullname)
            self.cache[fullname] = TypeTreeNode.from_list(
                [TypeTreeNode(b.m_Level, b.m_Type, b.m_Name, 0, 0, m_MetaFlag=b.m_MetaFlag) for b in base])
        return self.cache[fullname]


def text_objects(env, types):
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
        if c in ("Config.LocalizationConfig", "Story.LiluoScript"):
            out.append((c, o))
    return out


def load_text(game, types, res_path):
    env = UnityPy.load(res_path, os.path.join(game, DATA, "globalgamemanagers.assets"))
    objs = text_objects(env, types)
    loc, story = {}, {}
    for c, o in objs:
        if c == "Config.LocalizationConfig":
            t = o.read_typetree(nodes=types.nodes(c))
            lang = t["m_Name"].split("_", 1)[1]
            for e in t["localizationInfos"]:
                loc.setdefault(e["key"], {})[lang] = e["value"]
        else:
            t = o.read_typetree(nodes=types.nodes(c))
            story[t["m_Name"]] = [l.get("content", "") for b in t["blocks"] for l in b["lines"]]
    return env, objs, loc, story


def src_of(v):
    return v.get("CN") or v.get("CN_TC") or ""


# ---------------------------------------------------------------- check
def cmd_check(a):
    game = a.game
    types = Types(game)
    _, _, loc, story = load_text(game, types, orig_path(game, DATA + "/resources.assets"))
    tr_loc, meta_loc = jload(os.path.join(TR_DIR, "loc.ko.json")), jload(os.path.join(META_DIR, "loc.srchash.json"))
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

    en_by_text = {k: v.get("EN", "") for k, v in loc.items() if CJK.search(k)}
    todo_story, seen = [], set()
    for name, lines in story.items():
        for c in lines:
            if not c or not CJK.search(c):
                continue
            body = split_dir(c)[1]
            i = h12(body)
            if story_ko(tr_story, c) is not None or i in seen:
                continue
            seen.add(i)
            todo_story.append(dict(id=i, script=name, cn=body, en=en_by_text.get(body, ""), status="신규"))

    tr_ui, meta_ui = jload(os.path.join(TR_DIR, "ui_prefab.ko.json")), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
    todo_ui, cand = [], []
    for rel in find_rel(game, SW + "/prefabs_assets_assets/**/*.bundle"):
        s = stem(rel)
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
            if pid in tr_ui.get(s, {}):
                if meta_ui.get(s, {}).get(pid) != h12(v):
                    todo_ui.append(dict(bundle=s, id=pid, cn=v, status="원문변경", prev_ko=tr_ui[s][pid]))
            elif CJK.search(v):
                cand.append(dict(bundle=s, id=pid, cn=v, status="후보(대부분 자리표시자)"))

    jsave(os.path.join(WORK_DIR, "todo_loc.json"), todo_loc)
    jsave(os.path.join(WORK_DIR, "todo_story.json"), todo_story)
    jsave(os.path.join(WORK_DIR, "todo_ui_prefab.json"), todo_ui + cand)
    n = lambda L, s: sum(1 for x in L if x["status"] == s)
    print(f"게임 빌드 {game_build(game)}")
    print(f"번역표   : 신규 {n(todo_loc, '신규')} / 원문변경 {n(todo_loc, '원문변경')} / 삭제 {len(gone_loc)}")
    print(f"스토리   : 신규 {len(todo_story)}")
    print(f"UI 고정문구: 원문변경 {len(todo_ui)} / 미번역 중국어 후보 {len(cand)}")
    print("-> work/todo_loc.json, work/todo_story.json, work/todo_ui_prefab.json")


# ---------------------------------------------------------------- merge
def cmd_merge(a):
    game = a.game
    types = Types(game)
    _, _, loc, _ = load_text(game, types, orig_path(game, DATA + "/resources.assets"))
    for f in a.files:
        d = jload(f)
        base = os.path.basename(f)
        if "story" in base:
            tr = jload(os.path.join(TR_DIR, "story.ko.json"))
            for k, v in d.items():
                tr[k] = split_dir(v)[1]
            jsave(os.path.join(TR_DIR, "story.ko.json"), tr)
        elif "ui_prefab" in base:
            tr, meta = jload(os.path.join(TR_DIR, "ui_prefab.ko.json")), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
            todo = {(x["bundle"], x["id"]): x["cn"] for x in jload(os.path.join(WORK_DIR, "todo_ui_prefab.json"), [])}
            for s, m in d.items():
                for pid, v in m.items():
                    tr.setdefault(s, {})[pid] = v
                    if (s, pid) in todo:
                        meta.setdefault(s, {})[pid] = h12(todo[(s, pid)])
            jsave(os.path.join(TR_DIR, "ui_prefab.ko.json"), tr)
            jsave(os.path.join(META_DIR, "ui_prefab.srchash.json"), meta)
        elif "loc" in base:
            tr, meta = jload(os.path.join(TR_DIR, "loc.ko.json")), jload(os.path.join(META_DIR, "loc.srchash.json"))
            for k, v in d.items():
                if k not in loc:
                    print(f"  [경고] 게임에 없는 키: {k}")
                    continue
                tr[k] = v
                meta[k] = h12(src_of(loc[k]))
            jsave(os.path.join(TR_DIR, "loc.ko.json"), tr)
            jsave(os.path.join(META_DIR, "loc.srchash.json"), meta)
        else:
            sys.exit(f"[오류] 파일명에 loc / story / ui_prefab 중 하나가 들어가야 합니다: {f}")
        print(f"반영: {f} ({len(d)})")


# ---------------------------------------------------------------- build
def build_resources(game, types, allow_stale, out_dir, stats):
    rel = DATA + "/resources.assets"
    env, objs, loc, _ = load_text(game, types, orig_path(game, rel))
    tr_loc, meta_loc = jload(os.path.join(TR_DIR, "loc.ko.json")), jload(os.path.join(META_DIR, "loc.srchash.json"))
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
    if len(data) < os.path.getsize(os.path.join(game, rel)) * 0.9:
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


def save_bundle(env, game, rel, out_dir):
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
    for rel in find_rel(game, SW + "/images_assets_assets/addressables/images/group_*.bundle"):
        if not os.path.exists(logo):
            break
        env = UnityPy.load(orig_path(game, rel))
        objs = {o.path_id: o for o in env.objects}
        spr = next(s for s in (o.read() for o in env.objects if o.type.name == "Sprite") if s.m_Name == "Title_EN")
        tex = objs[spr.m_RD.texture.path_id].read()
        canvas = Image.new("RGBA", (tex.m_Width, tex.m_Height), (0, 0, 0, 0))
        canvas.paste(Image.open(logo).convert("RGBA"), top_left(spr.m_RD.textureRect, tex.m_Height))
        set_tex(tex, canvas)
        outs.append(save_bundle(env, game, rel, out_dir)); done.append("Title_EN")
    # 튜토리얼 조각 (TeachAtlas 스프라이트 아틀라스)
    tut = {os.path.basename(p)[:-4]: p for p in glob.glob(os.path.join(IMG_DIR, "tutorial", "*.png"))}
    for rel in find_rel(game, SW + "/images_assets_assets/addressables/spriteatlas/teachatlas.spriteatlas_*.bundle"):
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
            rot = PACK_ROT.get((ad.settingsRaw >> 2) & 0xF)   # 아틀라스 패킹 회전/뒤집기
            if rot is not None:
                ko = ko.transpose(rot)
            imgs[tid].paste(ko, top_left(ad.textureRect, texs[tid].m_Height), ko)  # 알파 마스크: 이웃 스프라이트 보호
            done.append(s.m_Name)
        for tid, t in texs.items():
            set_tex(t, imgs[tid])
        outs.append(save_bundle(env, game, rel, out_dir))
    missing = sorted(set(tut) - set(done))
    # 도움말 (Help1~9 개별 번들, Help10 묶음 번들)
    for p in sorted(glob.glob(os.path.join(IMG_DIR, "help", "Help*.png"))):
        name = os.path.basename(p)[:-4]
        cands = find_rel(game, SW + f"/cgs_assets_assets/addressables/images/help/{name.lower()}.png_*.bundle") or \
            find_rel(game, SW + "/cgs_assets_assets/addressables/images/help_*.bundle")
        for rel in cands:
            src = os.path.join(out_dir, rel) if rel in outs else orig_path(game, rel)
            env = UnityPy.load(src)
            tex = next((t for t in (o.read() for o in env.objects if o.type.name == "Texture2D") if t.m_Name == name), None)
            if tex is None:
                continue
            set_tex(tex, Image.open(p).convert("RGBA"))
            if rel not in outs:
                outs.append(rel)
            save_bundle(env, game, rel, out_dir); done.append(name)
            break
    stats["images"] = dict(applied=len(done), missing=missing)
    return outs


def build_prefab_text(game, out_dir, stats):
    tr, meta = jload(os.path.join(TR_DIR, "ui_prefab.ko.json")), jload(os.path.join(META_DIR, "ui_prefab.srchash.json"))
    outs, applied, stale = [], 0, 0
    for rel in find_rel(game, SW + "/prefabs_assets_assets/**/*.bundle"):
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
            outs.append(save_bundle(env, game, rel, out_dir))
    stats["ui_prefab"] = dict(applied=applied, stale_skipped=stale)
    return outs


def build_font(game, out_dir, stats):
    outs = []
    for rel in find_rel(game, SW + "/fonts_assets_all_*.bundle"):
        env = UnityPy.load(orig_path(game, rel))
        fa = {}
        for o in env.objects:
            if o.type.name == "MonoBehaviour":
                t = o.read_typetree()
                if "m_FallbackFontAssetTable" in t:
                    fa[t["m_Name"]] = (o, t)
        lol = fa["Lolita SDF"][0].path_id
        added = []
        for name, (o, t) in fa.items():
            if name != "Lolita SDF" and lol not in [p["m_PathID"] for p in t["m_FallbackFontAssetTable"]]:
                t["m_FallbackFontAssetTable"].append({"m_FileID": 0, "m_PathID": lol})
                o.save_typetree(t); added.append(name)
        if added:
            outs.append(save_bundle(env, game, rel, out_dir))
        stats["font_fallback_added"] = added
    return outs


def build_catalog(game, out_dir, stats):
    rel = AA + "/catalog.json"
    raw = open(orig_path(game, rel), encoding="utf-8").read()
    c = json.loads(raw)
    ex = base64.b64decode(c["m_ExtraDataString"])
    pat = re.compile('"m_Crc":'.encode("utf-16le") + rb"((?:\d\x00)+)")
    cnt = 0

    def rep(m):
        nonlocal cnt
        cnt += 1
        n = len(m.group(1)) // 2
        return '"m_Crc":'.encode("utf-16le") + ("0" + " " * (n - 1)).encode("utf-16le")   # 길이 보존
    ex2 = pat.sub(rep, ex)
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
    game = a.game
    ver = jload(os.path.join(ROOT, "VERSION.json"))
    pver = ver.get("patch_version", "1.0.0")
    name = f"{PKG}_v{pver}"
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
    ver.update(built=datetime.date.today().isoformat(), game_buildid=game_build(game), patch_version=pver, stats=stats)
    jsave(os.path.join(ROOT, "VERSION.json"), ver)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"-> {zp} ({len(files)}개 파일)")

    if a.install:
        built = jload(os.path.join(WORK_DIR, "built.json"))
        for rel in files:
            orig_path(game, rel)  # 덮어쓰기 전 원본 백업 보장
            shutil.copy2(os.path.join(out_dir, rel), os.path.join(game, rel))
            built[rel] = sha(os.path.join(game, rel))
        jsave(os.path.join(WORK_DIR, "built.json"), built)
        print("게임 폴더에 설치 완료")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    m = sub.add_parser("merge")
    m.add_argument("files", nargs="+")
    b = sub.add_parser("build")
    for p in (c, m, b):
        p.add_argument("--game", default=DEFAULT_GAME)
    b.add_argument("--install", action="store_true")
    b.add_argument("--allow-stale", action="store_true")
    a = ap.parse_args()
    {"check": cmd_check, "merge": cmd_merge, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
