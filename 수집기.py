#!/usr/bin/env python3
"""
데이터 수집기
─────────────
언론사별로 주요 기사를 모아 위젯이 읽을 파일로 저장합니다.
네이버 뉴스 메인처럼 "언론사 하나 = 화면 하나" 구조로 만듭니다.

  설정.json  →  [수집기]  →  콘텐츠/데이터.js  →  signage.html 이 읽음

수집 방식
  1) 언론사마다 지정된 RSS(여러 섹션 가능)를 모아 최신순으로 정렬합니다.
  2) 제외키워드(스포츠 전적·부고 등)에 걸리는 기사는 뺍니다.
  3) 대표이미지가 없는 기사는 기사 페이지의 og:image 를 읽어 보강합니다.
  4) 원문을 화면에 직접 띄울 수 있는지는 언론사(도메인)마다 한 번만 확인합니다.

※ AI를 쓰지 않습니다. 정해진 주소에서 데이터를 받아 파일에 적을 뿐이라
  아무리 자주 돌려도 토큰 비용은 발생하지 않습니다.

실행
  ./.venv/bin/python 수집기.py           한 번 수집하고 종료
  ./.venv/bin/python 수집기.py --반복     설정한 주기마다 계속 수집 (Ctrl+C 종료)
"""

import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).parent
CONF = BASE / "설정.json"
OUT = BASE / "콘텐츠" / "데이터.js"
IMGDIR = BASE / "콘텐츠" / "img"

# 사이니지에 내보낼 이미지 크기
# 화면 표시 크기(1008x700)의 75% 로 저장합니다.
# 이 기기는 사진 한 장 푸는 데 94ms 나 걸리는데, 그 비용은 픽셀 수에 비례합니다.
# 75% 로 줄이면 픽셀이 56% 라 푸는 시간도 그만큼 짧아집니다.
# 몇 미터 떨어져 보는 화면이라 화질 차이는 눈에 띄지 않습니다.
IMG_W, IMG_H, IMG_Q = 756, 525, 74

KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

NS = {"media": "http://search.yahoo.com/mrss/", "dc": "http://purl.org/dc/elements/1.1/"}

SKY = {"1": ("맑음", "☀️"), "3": ("구름많음", "⛅"), "4": ("흐림", "☁️")}
PTY = {"1": ("비", "🌧"), "2": ("비/눈", "🌨"), "3": ("눈", "❄️"), "4": ("소나기", "🌦")}

_frame_cache: dict = {}  # 도메인별 원문 표시 가능 여부
_seen_items: list = []   # 이번에 훑은 기사 전부 (하단바 속보를 고를 때 다시 씁니다)

# 하단바는 자리가 좁아 언론사 이름을 줄여 씁니다
SHORT_PRESS = {"연합뉴스": "연합", "조선일보": "조선", "동아일보": "동아",
               "경향신문": "경향", "매일경제": "매경", "세계일보": "세계"}


def log(icon, msg):
    print(f"[{datetime.now():%H:%M:%S}] {icon} {msg}", flush=True)


def read_key() -> str:
    """
    공공데이터 인증키를 찾습니다. 설정 파일에는 두지 않습니다.
      1) 환경변수 DATA_GO_KR_KEY   (클라우드에서는 GitHub Secrets 로 넣습니다)
      2) 옆에 있는 .env 파일        (내 컴퓨터에서 쓸 때. 깃에는 올라가지 않습니다)
    """
    import os
    v = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if v:
        return v
    try:
        for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATA_GO_KR_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def load_conf() -> dict:
    if not CONF.exists():
        sys.exit(f"설정 파일이 없습니다: {CONF}")
    cfg = json.loads(CONF.read_text(encoding="utf-8"))
    # 인증키는 설정 파일이 아니라 .env / 환경변수에서 가져와 붙입니다.
    cfg.setdefault("공공데이터포털", {})["인증키"] = read_key()
    return cfg



def get(url: str, timeout: int = 12):
    """
    페이지를 받아옵니다.
    클라우드(GitHub Actions)는 서버가 해외에 있어 국내 언론사 응답이 느리거나
    막히는 일이 있습니다. 그래서 브라우저처럼 보이는 머리말을 붙이고,
    시간이 걸리면 조금 더 기다렸다가 한 번 더 시도합니다.
    """
    head = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "close",
    }
    last = None
    for wait in (timeout, timeout * 2, timeout * 3):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=head), timeout=wait)
        except Exception as e:
            last = e
            code = getattr(e, "code", None)
            if code in (401, 403, 404):        # 막힌 것은 다시 물어도 같습니다
                break
            time.sleep(1)
    raise last


def clean_text(s: str) -> str:
    """일부 언론사는 따옴표를 두 번 인코딩해 보내므로 더 이상 변하지 않을 때까지 풀어줍니다."""
    import html

    s = s or ""
    for _ in range(3):
        un = html.unescape(s)
        if un == s:
            break
        s = un
    return re.sub(r"\s+", " ", s).strip()


def clean_author(s: str) -> str:
    """
    기자 표기를 다듬습니다.
    RSS 는 'shimmy@sbs.co.kr (심우섭 기자)' 처럼 이메일을 함께 주는 경우가 많아
    화면에는 이름만 남깁니다.
    """
    s = clean_text(s)
    if not s:
        return ""
    m = re.search(r"\(([^)]+)\)", s)          # 괄호 안 이름 우선
    if m:
        s = m.group(1)
    s = re.sub(r"[\w.+-]+@[\w.-]+", "", s)     # 남은 이메일 제거
    s = re.sub(r"\s*[|/]\s*.*$", "", s)        # '이름 | 부서' 형태 정리
    s = s.strip(" ,·-")
    return s if len(s) <= 20 else ""


def minutes_ago(pub_date: str) -> int:
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(pub_date)
        return max(0, int((datetime.now(dt.tzinfo) - dt).total_seconds() // 60))
    except Exception:
        return 9999


def ago_text(m: int) -> str:
    if m >= 9999:
        return ""
    if m < 60:
        return f"{m}분 전"
    if m < 1440:
        return f"{m // 60}시간 전"
    return f"{m // 1440}일 전"


# ────────────────────────────── 뉴스 ──────────────────────────────
def parse_feed(url: str) -> list:
    """RSS 하나에서 기사 목록을 뽑습니다."""
    try:
        root = ET.fromstring(get(url, timeout=20).read())
    except Exception as e:
        log("⚠️", f"피드 실패: {url.split('/')[2]} — {str(e)[:40]}")
        return []

    out = []
    for it in root.iterfind(".//item"):
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue

        img = ""
        for tag in ("media:content", "media:thumbnail"):
            el = it.find(tag, NS)
            if el is not None and el.get("url"):
                img = el.get("url")
                break
        if not img:
            enc = it.find("enclosure")
            if enc is not None and (enc.get("type") or "").startswith("image"):
                img = enc.get("url", "")

        out.append({
            "title": title,
            "link": link,
            "min": minutes_ago(it.findtext("pubDate") or ""),
            "author": clean_author(it.findtext("dc:creator", namespaces=NS) or it.findtext("author") or ""),
            "image": img,
            "summary": clean_text(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))[:400],
        })
    return out


def can_frame(url: str) -> bool:
    """
    원문을 화면에 직접 띄울 수 있는지 판단합니다.
    같은 언론사는 정책이 같으므로 도메인마다 한 번만 확인합니다.
    """
    host = urllib.parse.urlparse(url).netloc
    if host in _frame_cache:
        return _frame_cache[host]
    ok = False
    try:
        with get(url, timeout=10) as r:
            h = {k.lower(): v for k, v in r.headers.items()}
        xfo = (h.get("x-frame-options") or "").upper()
        csp = (h.get("content-security-policy") or "").lower()
        ok = not ("DENY" in xfo or "SAMEORIGIN" in xfo or "frame-ancestors" in csp)
    except Exception:
        ok = False
    _frame_cache[host] = ok
    return ok


def fetch_meta(url: str, press: str = "") -> dict:
    """
    기사 페이지에서 대표이미지와 요약을 가져옵니다.

    ※ 본문 전문은 가져오지 않습니다.
      언론사 기사에는 "무단 전재·재배포, AI 학습 및 활용 금지" 저작권 표시가 붙어 있고,
      일부 언론사는 robots.txt 로 기사 페이지 수집을 막아두고 있습니다.
      화면에는 공유용으로 공개된 요약(og:description)만 쓰고,
      전문을 읽고 싶으면 QR 또는 원문 페이지로 넘어가도록 만들었습니다.
    """
    out = {"image": "", "desc": ""}
    try:
        with get(url, timeout=12) as r:
            page = r.read(200_000).decode("utf-8", "ignore")
    except Exception:
        return out

    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)', page, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', page, re.I)
    if m:
        out["image"] = m.group(1).strip()

    d = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']+)', page, re.I) \
        or re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)', page, re.I)
    if d:
        out["desc"] = clean_text(d.group(1))[:500]
    return out


def prepare_image(url: str) -> str:
    """
    기사 이미지를 내려받아 화면 크기에 맞게 줄이고 흑백으로 변환해 저장합니다.

    원본은 장당 300KB~2MB나 되어 사이니지(라즈베리파이3급 CPU)가 디코딩하다 버벅입니다.
    미리 줄이고 흑백으로 바꿔두면 용량이 1/10 수준으로 떨어지고,
    화면에서 CSS 필터를 쓸 필요도 없어져 훨씬 가볍습니다.

    반환값: 콘텐츠 폴더 기준 상대경로 (실패하면 빈 문자열)
    """
    import hashlib

    if not url:
        return ""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return url  # Pillow 가 없으면 원본 주소를 그대로 씁니다

    IMGDIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest()[:16] + ".jpg"
    path = IMGDIR / name
    rel = f"img/{name}"
    if path.exists():
        return rel

    try:
        import io

        with get(url, timeout=12) as r:
            raw = r.read(6_000_000)
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im = ImageOps.grayscale(im)
        im = ImageOps.autocontrast(im, cutoff=1)
        im = ImageOps.fit(im, (IMG_W, IMG_H), method=Image.LANCZOS, centering=(0.5, 0.3))
        im.save(path, "JPEG", quality=IMG_Q, optimize=True, progressive=False)
        return rel
    except Exception:
        return ""


def sweep_images(keep: set) -> None:
    """이번 수집에 쓰이지 않은 이미지 파일을 지웁니다."""
    if not IMGDIR.exists():
        return
    removed = 0
    for f in IMGDIR.glob("*.jpg"):
        if f"img/{f.name}" not in keep:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log("🧹", f"오래된 이미지 {removed}개 정리")


def order_items(lists: list, mode: str) -> list:
    """
    여러 피드에서 받은 기사를 어떤 순서로 늘어놓을지 정합니다.

      편집순 : 앞 피드부터 실린 순서 그대로.
               SBS 헤드라인이나 JTBC 이슈처럼 언론사가 직접 고른 순서를 살립니다.
      번갈아 : 피드마다 한 건씩 돌아가며 뽑습니다.
               섹션이 여럿일 때 한 섹션이 화면을 다 차지하지 않게 하면서도,
               각 피드 안의 순서(언론사가 정한 순서)는 그대로 둡니다.
      최신순 : 발행시각이 빠른 순. 한겨레처럼 시각이 없는 곳에는 쓸 수 없습니다.
    """
    if mode == "최신순":
        return sorted([x for r in lists for x in r], key=lambda x: x["min"])
    if mode == "편집순":
        return [x for r in lists for x in r]
    out = []                                       # 번갈아
    for i in range(max((len(r) for r in lists), default=0)):
        for r in lists:
            if i < len(r):
                out.append(r[i])
    return out


def collect_press(press: dict, drop_words: list, want_image: bool, max_min: int = 1440) -> dict | None:
    """언론사 하나의 기사를 모읍니다."""
    name = press.get("이름", "?")
    feeds = press.get("피드", [])
    limit = int(press.get("건수", 5))
    if not feeds:
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        lists = list(ex.map(parse_feed, feeds))
    if not any(lists):
        log("❌", f"{name} — 기사를 가져오지 못했습니다")
        return None

    # 하단바가 속보를 고를 때 쓰도록, 추린 것 말고 받아온 전부를 남겨 둡니다.
    for r in lists:
        for n in r:
            _seen_items.append({"title": n["title"], "min": n["min"], "press": name})

    mode = press.get("정렬", "자동")
    if mode == "자동":
        # 피드가 하나면 그 순서가 곧 언론사의 편집 순서입니다.
        mode = "편집순" if len(feeds) == 1 else "번갈아"

    items = order_items(lists, mode)

    # 제외 키워드
    if drop_words:
        items = [n for n in items if not any(w in n["title"] for w in drop_words)]

    # 너무 오래된 기사 빼기 (한겨레처럼 발행시각이 없는 곳은 그대로 둡니다)
    old = [n for n in items if n["min"] != 9999 and n["min"] > max_min]
    if old:
        items = [n for n in items if n not in old]

    # 정해진 순서를 지키면서 제목 중복만 걸러냅니다
    seen, uniq = set(), []
    for n in items:
        key = re.sub(r"[^\w가-힣]", "", n["title"])[:20]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(n)

    top = uniq[:limit]

    # 기사 페이지를 열어 이미지·요약을 보강합니다 (기사당 1회)
    with ThreadPoolExecutor(max_workers=5) as ex:
        metas = list(ex.map(lambda x: fetch_meta(x["link"], name), top))
    for n, meta in zip(top, metas):
        n["image"] = n["image"] or meta["image"]
        # RSS 요약과 og 요약 중 더 긴 쪽을 씁니다
        n["summary"] = max([n.get("summary", ""), meta["desc"]], key=len)

    # 이미지를 내려받아 줄이고 흑백으로 바꿔 저장합니다 (사이니지 성능 핵심)
    if want_image:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for n, local in zip(top, ex.map(lambda x: prepare_image(x["image"]), top)):
                n["image"] = local

    for i, n in enumerate(top):
        n["press"] = name
        n["ago"] = ago_text(n.pop("min"))
        n["hot"] = (i == 0)

    img_ok = sum(1 for n in top if n["image"])
    sum_ok = sum(1 for n in top if len(n["summary"]) > 40)
    log("📰", f"{name:6} {len(top)}건 · {mode} · 이미지 {img_ok}/{len(top)} · 요약 {sum_ok}/{len(top)}"
              + (f" · 낡은 기사 {len(old)}건 제외" if old else ""))
    return {"name": name, "desc": press.get("설명", ""), "items": top}


def collect_news(cfg: dict) -> list:
    _seen_items.clear()
    c = cfg.get("뉴스", {})
    plist = c.get("언론사", [])
    drop = c.get("제외키워드", [])
    want_img = c.get("대표이미지", True)
    max_min = int(cfg.get("뉴스", {}).get("최대경과분", 1440))
    out = []
    for p in plist:
        r = collect_press(p, drop, want_img, max_min)
        if r and r["items"]:
            out.append(r)
    return out


# ────────────────────────────── 날씨 ──────────────────────────────
# 네이버 날씨 화면과 같은 구성으로 모읍니다.
#   · 지금       초단기실황 (매시 정시 관측값, 40분 뒤 공개)
#   · 오늘 시간별 단기예보  (1시간 간격, 오늘~모레)
#   · 주간       내일부터 6일. 앞 이틀은 단기예보로, 나머지는 중기예보로 채웁니다.
#
# 중기예보는 공공데이터포털에서 따로 신청해야 하는 별개 서비스입니다.
# 아직 승인 전이면 채울 수 있는 날짜까지만 넣고 조용히 넘어갑니다.
VILAGE = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
MIDFCST = "https://apis.data.go.kr/1360000/MidFcstInfoService"

DOW = ["월", "화", "수", "목", "금", "토", "일"]
VEC16 = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
         "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]

# 중기예보는 숫자가 아니라 "구름많고 비" 같은 말로 옵니다.
MID_ICON = [("눈", "❄️"), ("소나기", "🌦"), ("비", "🌧"), ("흐림", "☁️"),
            ("구름많", "⛅"), ("맑음", "☀️")]

# 사이니지 폰트에 이모지가 없을 수 있어, 글자 표기를 함께 보냅니다.
SHORT = {"맑음": "맑음", "구름많음": "구름", "흐림": "흐림",
         "비": "비", "비/눈": "비눈", "눈": "눈", "소나기": "소나기"}


def short_of(icon: str, desc: str = "") -> str:
    if desc:
        return SHORT.get(desc, desc)
    for word, ic in MID_ICON:
        if ic == icon:
            return SHORT.get(word, "맑음" if word == "맑음" else word.replace("구름많", "구름"))
    return "맑음"


def kma(base: str, path: str, params: dict, key: str):
    """기상청 API 한 번 호출. 실패하면 None 을 돌려줍니다."""
    qs = urllib.parse.urlencode(
        {**params, "dataType": "JSON", "numOfRows": 1000, "pageNo": 1, "serviceKey": key}, safe="%")
    try:
        data = json.loads(get(f"{base}/{path}?{qs}", timeout=60).read().decode("utf-8"))
        head = data["response"]["header"]
        if head["resultCode"] != "00":
            log("⏭", f"{path}: {head['resultMsg']}")
            return None
        return data["response"]["body"]["items"]["item"]
    except Exception as e:
        log("⏭", f"{path}: {str(e)[:60]}")
        return None


def vilage_base():
    """단기예보 발표시각 — 02·05·08·11·14·17·20·23시 (10분 뒤 공개)"""
    now = datetime.now() - timedelta(minutes=45)
    for h in (23, 20, 17, 14, 11, 8, 5, 2):
        if now.hour >= h:
            return now.strftime("%Y%m%d"), f"{h:02d}00"
    return (now - timedelta(days=1)).strftime("%Y%m%d"), "2300"


def ncst_bases():
    """
    초단기실황 발표시각 후보 — 매시 정시에 관측해 40분쯤 뒤에 올라옵니다.
    막 40분을 넘긴 시점에는 아직 안 올라와 있을 수 있어, 한 시간 전 것도 함께 준비합니다.
    """
    now = datetime.now() - timedelta(minutes=41)
    return [((now - timedelta(hours=k)).strftime("%Y%m%d"),
             f"{(now - timedelta(hours=k)).hour:02d}00") for k in (0, 1)]


def mid_bases():
    """중기예보 발표시각 후보 — 06시·18시. 최신 것부터 차례로 시도합니다."""
    now = datetime.now() - timedelta(minutes=30)
    y = (now - timedelta(days=1)).strftime("%Y%m%d")
    t = now.strftime("%Y%m%d")
    if now.hour >= 18:
        return [t + "1800", t + "0600", y + "1800"]
    if now.hour >= 6:
        return [t + "0600", y + "1800", y + "0600"]
    return [y + "1800", y + "0600"]


def sky_icon(pty: str, sky: str):
    """강수형태가 있으면 그쪽이 우선입니다."""
    return PTY.get(pty or "0") or SKY.get(sky or "1", ("맑음", "☀️"))


def mid_icon(text: str) -> str:
    for word, ic in MID_ICON:
        if word in (text or ""):
            return ic
    return "☀️"


def pcp_mm(v: str) -> float:
    """'강수없음' · '1.0mm' · '30.0~50.0mm' 를 숫자로 바꿉니다."""
    nums = re.findall(r"\d+\.?\d*", str(v or ""))
    return round(sum(map(float, nums)) / len(nums), 1) if nums else 0.0


def feels_like(t: float, rh: float, wind: float) -> float:
    """
    체감온도 (기상청 산출식)
      5~9월  : 습도를 반영한 여름철 체감 (습구온도 이용)
      10~4월 : 바람을 반영한 겨울철 체감. 10°C 초과거나 바람이 약하면 기온 그대로.
    """
    import math
    if 5 <= datetime.now().month <= 9:
        tw = (t * math.atan(0.151977 * (rh + 8.313659) ** 0.5)
              + math.atan(t + rh) - math.atan(rh - 1.67633)
              + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)
        return round(-0.2442 + 0.55399 * tw + 0.45535 * t
                     - 0.0022 * tw ** 2 + 0.00278 * tw * t + 3.0, 1)
    if t > 10 or wind < 1.3:
        return round(t, 1)
    v = wind * 3.6
    return round(13.12 + 0.6215 * t - 11.37 * v ** 0.16 + 0.3965 * v ** 0.16 * t, 1)


def sun_times(lat: float, lon: float):
    """
    일출·일몰 (NOAA 근사식). 별도 API 없이 계산합니다.
    """
    import math
    d = datetime.now()
    n = d.timetuple().tm_yday
    lng_hour = lon / 15.0
    out = []
    for rising in (True, False):
        t = n + ((6 if rising else 18) - lng_hour) / 24.0
        m = 0.9856 * t - 3.289
        l = (m + 1.916 * math.sin(math.radians(m))
             + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360
        ra = (ra + (math.floor(l / 90) * 90 - math.floor(ra / 90) * 90)) / 15.0
        sin_dec = 0.39782 * math.sin(math.radians(l))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = ((math.cos(math.radians(90.833)) - sin_dec * math.sin(math.radians(lat)))
                 / (cos_dec * math.cos(math.radians(lat))))
        if abs(cos_h) > 1:
            out.append("--:--")
            continue
        h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
        ut = (h / 15.0 + ra - 0.06571 * t - 6.622 - lng_hour) % 24
        kst = (ut + 9) % 24
        out.append(f"{int(kst):02d}:{int(round((kst % 1) * 60)):02d}")
    return out[0], out[1]


def collect_weather(cfg: dict):
    portal = cfg.get("공공데이터포털", {})
    c = portal.get("날씨", {})
    key = portal.get("인증키", "")
    if not key or key.startswith("여기에"):
        log("⏭", "기상청 인증키가 아직 없습니다 — 날씨는 예시 데이터를 씁니다")
        return None

    nx, ny = c.get("격자X", 58), c.get("격자Y", 127)
    today = datetime.now().strftime("%Y%m%d")

    # ① 단기예보 — 시간별 예보와 오늘·내일·모레 요약의 재료
    bd, bt = vilage_base()
    rows = kma(VILAGE, "getVilageFcst", {"base_date": bd, "base_time": bt, "nx": nx, "ny": ny}, key)
    if not rows:
        log("❌", "날씨 실패: 단기예보를 받지 못했습니다")
        return None

    slots: dict = {}
    for it in rows:
        slots.setdefault(it["fcstDate"] + it["fcstTime"], {})[it["category"]] = it["fcstValue"]
    keys = sorted(slots)
    if not keys:
        return None

    # ② 초단기실황 — 지금 이 순간의 관측값 (예보보다 정확)
    obs, nt = {}, ""
    for nd, t in ncst_bases():
        ncst = kma(VILAGE, "getUltraSrtNcst", {"base_date": nd, "base_time": t, "nx": nx, "ny": ny}, key)
        if ncst:
            obs = {x["category"]: x["obsrValue"] for x in ncst}
            nt = t
            break

    near = slots[keys[0]]
    temp = float(obs.get("T1H") or near.get("TMP") or 0)
    hum = int(float(obs.get("REH") or near.get("REH") or 0))
    wind = float(obs.get("WSD") or near.get("WSD") or 0)
    vec = int(float(obs.get("VEC") or near.get("VEC") or 0))
    pty = obs.get("PTY") or near.get("PTY", "0")
    desc, icon = sky_icon(pty, near.get("SKY", "1"))

    # ③ 시간별 예보 — 지금 이후만 담습니다.
    #    밤늦게는 오늘 남은 칸이 몇 개 없으므로 다음 날 새벽까지 이어 담아
    #    화면이 비지 않게 합니다. (오늘이 끝나는 지점은 tomorrow 표시로 구분)
    now_key = datetime.now().strftime("%Y%m%d%H00")
    hours = []
    for k in keys:
        if k < now_key or not slots[k].get("TMP") or len(hours) >= 24:
            continue
        s = slots[k]
        d2, i2 = sky_icon(s.get("PTY", "0"), s.get("SKY", "1"))
        hours.append({"h": int(k[8:10]), "i": i2, "s": SHORT.get(d2, d2), "t": round(float(s["TMP"])),
                      "d": f"{int(k[4:6])}.{int(k[6:8])}",
                      "pop": int(float(s.get("POP", 0))), "mm": pcp_mm(s.get("PCP")),
                      "rh": int(float(s.get("REH", 0))), "ws": round(float(s.get("WSD", 0))),
                      "next": k[:8] != today})

    # 오늘 최저·최고 — 예보에 없으면(하루가 지난 뒤) 남은 시간대에서 뽑습니다
    tmn = tmx = None
    for k in keys:
        if k[:8] != today:
            continue
        if slots[k].get("TMN"):
            tmn = round(float(slots[k]["TMN"]))
        if slots[k].get("TMX"):
            tmx = round(float(slots[k]["TMX"]))
    pool = [h["t"] for h in hours] or [round(temp)]
    tmn = tmn if tmn is not None else min(pool)
    tmx = tmx if tmx is not None else max(pool)

    # ④ 주간 — 내일부터 6일
    def day_box(ymd: str):
        """단기예보 하루치를 오전·오후로 접어 요약합니다."""
        am, pm, lo, hi = [], [], [], []
        for k in keys:
            if k[:8] != ymd:
                continue
            s = slots[k]
            if not s.get("TMP"):
                continue
            hh = int(k[8:10])
            box = am if hh < 12 else pm
            dd, ii = sky_icon(s.get("PTY", "0"), s.get("SKY", "1"))
            box.append((ii, int(float(s.get("POP", 0))), SHORT.get(dd, dd)))
            lo.append(float(s["TMP"]))
            hi.append(float(s["TMP"]))
        # 단기예보는 사흘째 이후를 새벽 몇 칸만 주기도 합니다.
        # 그런 날은 하루 최저·최고를 알 수 없으므로 비워 두고 중기예보에 넘깁니다.
        lo = [float(slots[k]["TMN"]) for k in keys if k[:8] == ymd and slots[k].get("TMN")]
        hi = [float(slots[k]["TMX"]) for k in keys if k[:8] == ymd and slots[k].get("TMX")]
        if not lo or not hi:
            return None
        def pick(box):
            """그 반나절에 가장 많이 나온 하늘 상태와, 가장 높은 강수확률"""
            if not box:
                return "", 0, ""
            top = max(set(x[0] for x in box), key=[x[0] for x in box].count)
            word = next(x[2] for x in box if x[0] == top)
            return top, max(x[1] for x in box), word
        ai, ap, aw = pick(am or pm)
        pi, pp, pw = pick(pm or am)
        return {"amI": ai, "amP": ap, "amS": aw, "pmI": pi, "pmP": pp, "pmS": pw,
                "min": round(min(lo)), "max": round(max(hi))}

    week = []
    for i in range(1, 7):
        d = datetime.now() + timedelta(days=i)
        week.append({"d": f"{d.month}.{d.day}.", "w": DOW[d.weekday()], "sun": d.weekday() == 6,
                     **(day_box(d.strftime("%Y%m%d")) or
                        {"amI": "", "amP": 0, "amS": "", "pmI": "", "pmP": 0, "pmS": "",
                         "min": None, "max": None})})

    # 단기예보가 닿지 않는 뒷날은 중기예보로 채웁니다.
    #
    # 주의: 중기예보의 항목 번호(wf3Am · taMin5 …)는 "오늘"이 아니라
    #       "발표일" 기준 며칠 뒤인지를 뜻합니다. 새벽에는 전날 18시 발표를 쓰므로
    #       오늘 기준으로 세면 하루씩 밀립니다. 발표일과의 날짜 차이로 번호를 구합니다.
    if any(x["max"] is None for x in week):
        L = T = None
        for tmfc in mid_bases():
            L = kma(MIDFCST, "getMidLandFcst", {"regId": c.get("중기육상코드", "11B00000"), "tmFc": tmfc}, key)
            T = kma(MIDFCST, "getMidTa", {"regId": c.get("중기기온코드", "11B10101"), "tmFc": tmfc}, key)
            if L and T:
                break                      # 최신 발표가 아직이면 그 앞 발표를 씁니다
        if L and T:
            L, T = L[0], T[0]
            base_day = datetime.strptime(tmfc[:8], "%Y%m%d").date()
            filled = 0
            for i, x in enumerate(week, start=1):
                if x["max"] is not None:
                    continue
                n = ((datetime.now() + timedelta(days=i)).date() - base_day).days
                if not (3 <= n <= 10) or not T.get(f"taMax{n}"):
                    continue
                # 8일째부터는 오전·오후 구분 없이 하루 한 값만 옵니다
                am = L.get(f"wf{n}Am") or L.get(f"wf{n}") or ""
                pm = L.get(f"wf{n}Pm") or L.get(f"wf{n}") or ""
                x["amI"], x["pmI"] = mid_icon(am), mid_icon(pm)
                x["amS"], x["pmS"] = short_of(x["amI"]), short_of(x["pmI"])
                x["amP"] = int(L.get(f"rnSt{n}Am") or L.get(f"rnSt{n}") or 0)
                x["pmP"] = int(L.get(f"rnSt{n}Pm") or L.get(f"rnSt{n}") or 0)
                x["min"], x["max"] = int(T[f"taMin{n}"]), int(T[f"taMax{n}"])
                filled += 1
            log("📅", f"중기예보로 {filled}일 채움 (발표 {tmfc[4:6]}.{tmfc[6:8]} {tmfc[8:10]}시)")
        else:
            log("⏭", "중기예보를 받지 못했습니다 — 주간은 단기예보 범위까지만 나옵니다")

    use_sample = bool(portal.get("샘플데이터", False))
    air = collect_air(cfg) or (sample_air() if use_sample else None)
    uv = collect_uv(cfg) or (sample_uv() if use_sample else None)

    rise, set_ = sun_times(c.get("위도", 37.5794), c.get("경도", 126.8895))
    tail = ""
    if air:
        tail += f" · 미세 {air['pm10g']}{'(샘플)' if air.get('sample') else ''}"
    if uv:
        tail += f" · 자외선 {uv['word']}{'(샘플)' if uv.get('sample') else ''}"
    log("🌤", f"날씨 수집 완료 ({desc} {temp}° · 시간별 {len(hours)}칸 · "
              f"주간 {sum(1 for x in week if x['max'] is not None)}/6일{tail})")

    return {
        "location": c.get("지역명", ""),
        "temp": round(temp, 1), "desc": desc, "icon": icon,
        "feel": feels_like(temp, hum, wind), "humidity": hum,
        "wind": round(wind, 1), "windDir": VEC16[int((vec + 11.25) % 360 // 22.5)],
        "min": tmn, "max": tmx, "sunrise": rise, "sunset": set_,
        "date": f"{datetime.now().month:02d}.{datetime.now().day:02d}",
        "hours": hours, "week": week,
        "obsAt": f"{nt[:2]}:00" if obs else "",
        "air": air, "uv": uv,
    }


# ────────────────────────────── 버스 ──────────────────────────────
# 서울시 버스도착정보. 도착 시간은 1~2분이면 바뀌므로
# 뉴스(10분)와 따로, 짧은 주기로 콘텐츠/버스.json 에만 씁니다.
BUS_URL = "http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid"
# 한 정류소의 한 노선만 묻는 창구. 정류소 전체를 받아오는 것보다 호출이 적습니다.
ROUTE_URL = "http://ws.bus.go.kr/api/rest/arrive/getArrInfoByRoute"
BUSOUT = BASE / "콘텐츠" / "버스.json"
BOOST = BASE / "콘텐츠" / ".버스집중"     # 화면에서 확인 버튼을 누르면 서버가 만드는 표시 파일
ONCE = BASE / "콘텐츠" / ".버스지금"      # "지금 한 번만 받아와" 신호
PIDFILE = BASE / ".수집기.pid"           # 지금 돌고 있는 수집기의 번호


def claim_single():
    """
    수집기를 두 개 이상 돌리지 않게 합니다.
    예전에 켜 둔 수집기가 남아 있으면 옛 설정·옛 코드로 데이터를 덮어써서,
    날씨나 버스가 갑자기 사라지는 일이 생깁니다. 그래서 먼저 정리합니다.
    """
    import os, signal
    try:
        old = int(PIDFILE.read_text().strip())
        if old != os.getpid():
            os.kill(old, signal.SIGTERM)
            log("🧹", f"먼저 돌고 있던 수집기(번호 {old})를 정리했습니다")
            time.sleep(1)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass
    except PermissionError:
        log("⚠️", "먼저 돌던 수집기를 정리하지 못했습니다 — 직접 종료해 주세요")
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
_bus_calls = [0]      # 오늘 몇 건 썼는지 (하루 1,000건 제한 확인용)
_bus_day = [datetime.now().day]
_boost_said = [0]     # 같은 안내를 반복해서 찍지 않기 위한 표시
_last_bus = [0.0]     # 마지막으로 버스를 부른 시각


def nap(sec: float):
    """
    다음 차례까지 잡니다. 다만 '지금 갱신' 신호가 오면 곧바로 깨어납니다.
    (5분을 통째로 자면 버튼을 눌러도 한참 뒤에야 반응하기 때문입니다)
    """
    end = time.time() + sec
    while time.time() < end:
        time.sleep(min(3, max(0.2, end - time.time())))
        if ONCE.exists():
            return


def boost_read():
    """
    집중 갱신 표시 파일을 읽습니다.
    첫 줄은 누른 시각, 둘째 줄은 무엇을 볼지입니다.
      all · ars:14112 · route:9711@14112
    """
    try:
        parts = BOOST.read_text(encoding="utf-8").strip().splitlines()
        return float(parts[0]), (parts[1].strip() if len(parts) > 1 else "all")
    except Exception:
        return 0.0, "all"


def boost_left(hold_min: int) -> float:
    """집중 갱신이 몇 분 남았는지. 해당 없으면 0."""
    pressed, _ = boost_read()
    if not pressed:
        return 0.0
    left = hold_min - (time.time() - pressed) / 60
    return left if left > 0 else 0.0


def bus_period(cfg: dict) -> int:
    """지금 버스를 몇 초마다 부를지 정합니다."""
    conf = (cfg.get("공공데이터포털", {}).get("버스", {}) or {})
    left = boost_left(int(conf.get("집중유지분", 30)))
    if not left:
        return int(conf.get("갱신초", 300))
    _, tg = boost_read()
    # 노선 하나만 보는 중이면 호출이 1건뿐이라 더 자주 물어봐도 됩니다.
    sec = int(conf.get("노선집중갱신초", 10)) if tg.startswith("route:") \
        else int(conf.get("집중갱신초", 30))
    if time.time() - _boost_said[0] > 60:
        _boost_said[0] = time.time()
        log("⚡", f"버스 집중 갱신 중 ({boost_label(tg)} · {sec}초 간격) — {left:.0f}분 남음")
    return sec


def boost_label(target: str) -> str:
    if target.startswith("route:"):
        no, _, ars = target[6:].partition("@")
        return f"{no}번만"
    if target.startswith("ars:"):
        return f"{target[4:]} 정류소만"
    return "정류소 전체"


def bus_msg(raw: str) -> dict:
    """
    '3분13초후[2번째 전]' 처럼 붙어 오는 문구를 갈라 놓습니다.
      · when  화면에 크게 쓸 도착 시간
      · where 몇 정거장 전인지
    """
    t = (raw or "").strip()
    if not t or t in ("출발대기", "운행종료"):
        return {"when": t or "정보 없음", "where": "", "soon": False}
    m = re.match(r"(.*?)\s*\[(.*?)\]", t)
    when, where = (m.group(1).strip(), m.group(2).strip()) if m else (t, "")
    where = re.sub(r"(\d+)\s*번째\s*전", r"\1정거장", where)
    soon = "곧" in when or when.startswith("1분") or when.startswith("0분")
    when = re.sub(r"(\d+)분(\d+)초후", r"\1분 \2초", when)
    when = re.sub(r"(\d+)분후", r"\1분", when)
    return {"when": when, "where": where, "soon": soon}


def in_service_hours(span: str) -> bool:
    """'07:00-21:00' 같은 운영시간 안인지 봅니다. 자정을 넘는 구간도 됩니다."""
    try:
        a, b = [x.strip() for x in (span or "").split("-")]
        now = datetime.now().strftime("%H:%M")
        return (a <= now < b) if a <= b else (now >= a or now < b)
    except Exception:
        return True


def refresh_one_route(cfg: dict, no: str, ars: str):
    """
    노선 하나만 다시 조회합니다 (호출 1건).
    정류소 전체 조회 때 저장해 둔 stId·노선ID·순번을 열쇠로 씁니다.
    """
    portal = cfg.get("공공데이터포털", {})
    key = portal.get("인증키", "")
    try:
        data = json.loads(BUSOUT.read_text(encoding="utf-8"))
    except Exception:
        return None

    stop = next((x for x in data.get("stops", []) if x["ars"] == ars), None)
    line = next((l for l in (stop or {}).get("lines", []) if l["no"] == no), None)
    if not line:
        return None

    if data.get("sample"):                       # 승인 전에는 시각만 새로 찍습니다
        data["at"] = datetime.now().strftime("%H:%M:%S")
        BUSOUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        log("🚌", f"{no}번만 갱신 (예시값 — 승인되면 호출 1건으로 실제 조회)")
        return data["stops"]

    if not (line.get("stId") and line.get("rtId") and line.get("ord")):
        log("⏭", f"{no}번: 노선 조회에 필요한 정보가 아직 없습니다 (정류소 전체 조회가 한 번 돌아야 합니다)")
        return None
    try:
        qs = urllib.parse.urlencode({"stId": line["stId"], "busRouteId": line["rtId"],
                                     "ord": line["ord"], "serviceKey": key}, safe="%")
        raw = get(f"{ROUTE_URL}?{qs}", timeout=20).read().decode("utf-8", "ignore")
    except Exception as e:
        log("⏭", f"{no}번 조회: {str(e)[:50]}")
        return None

    code = (re.search(r"<headerCd>(.*?)</headerCd>", raw) or [0, "?"])[1]
    if code != "0":
        msg = (re.search(r"<headerMsg>(.*?)</headerMsg>", raw) or [0, "?"])[1]
        log("⏭", f"{no}번 조회: {msg[:50]}")
        return None

    pick = lambda t: (re.search(f"<{t}>(.*?)</{t}>", raw, re.S) or [0, ""])[1].strip()
    line["a"] = bus_msg(pick("arrmsg1"))
    line["b"] = bus_msg(pick("arrmsg2"))
    try:
        line["sec"] = int(pick("traTime1") or line.get("sec", 9999))
    except ValueError:
        pass
    data["at"] = datetime.now().strftime("%H:%M:%S")
    BUSOUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _bus_calls[0] += 1
    log("🚌", f"{no}번만 갱신 — {line['a']['when']} (오늘 {_bus_calls[0]}건)")
    return data["stops"]


def collect_bus(cfg: dict):
    portal = cfg.get("공공데이터포털", {})
    key = portal.get("인증키", "")
    conf = portal.get("버스", {}) or {}
    stops = conf.get("정류장", [])
    if not key or key.startswith("여기에") or not stops:
        return None

    # "지금 한 번만" 신호가 있으면 대상과 무관하게 정류소 전체를 새로 받습니다.
    once = ONCE.exists()
    if once:
        ONCE.unlink(missing_ok=True)
        log("🔄", "버스 1회 갱신 요청 — 정류소 전체를 지금 받습니다")

    # 시작 직후 뉴스 수집과 반복 루프가 잇달아 부르는 일이 있어,
    # 몇 초 안에 두 번 부르는 것은 건너뜁니다 (한도를 아끼기 위해)
    if not once and time.time() - _last_bus[0] < 5:
        return None
    _last_bus[0] = time.time()

    def save_sample():
        out = sample_bus(cfg)
        if not out:
            return None
        BUSOUT.write_text(json.dumps(
            {"stops": out, "at": datetime.now().strftime("%H:%M:%S"), "sample": True},
            ensure_ascii=False, indent=1), encoding="utf-8")
        log("🚌", "버스 예시값 사용 (승인 전) — " +
                  " · ".join(f"{x['name']} {len(x['lines'])}개" for x in out))
        return out

    # 집중 갱신 중이고 대상이 정해져 있으면 그것만 봅니다.
    # (1회 갱신 요청이 왔을 때는 대상과 상관없이 전체를 받습니다)
    only_ars = ""
    if not once and boost_left(int(conf.get("집중유지분", 30))):
        _, target = boost_read()
        if target.startswith("route:"):
            no, _, ars = target[6:].partition("@")
            got = refresh_one_route(cfg, no, ars or (stops[0].get("ARS") if stops else ""))
            if got is not None:
                return got
        elif target.startswith("ars:"):
            only_ars = target[4:]

    # 하루 1,000건 제한이 있어 운영시간 밖에는 부르지 않습니다.
    # (예시값을 쓰는 동안에는 화면이 비지 않도록 시간과 무관하게 채웁니다)
    if not once and not in_service_hours(conf.get("운영시간", "")):
        return save_sample() if portal.get("샘플데이터", False) else None

    out = []
    for st in stops:
        ars = str(st.get("ARS", "")).strip()
        if not ars or ars.startswith("여기에"):
            continue
        if only_ars and ars != only_ars:      # 정류소 하나만 보라고 했을 때
            continue
        try:
            qs = urllib.parse.urlencode({"arsId": ars, "serviceKey": key}, safe="%")
            raw = get(f"{BUS_URL}?{qs}", timeout=20).read().decode("utf-8", "ignore")
        except Exception as e:
            log("⏭", f"버스 {ars}: {str(e)[:50]}")
            continue

        code = (re.search(r"<headerCd>(.*?)</headerCd>", raw) or [0, "?"])[1]
        if code != "0":
            msg = (re.search(r"<headerMsg>(.*?)</headerMsg>", raw) or [0, "?"])[1]
            log("⏭", f"버스 {ars}: {msg[:60]}")
            continue

        pick = lambda blk, tag: (re.search(f"<{tag}>(.*?)</{tag}>", blk, re.S) or [0, ""])[1].strip()
        lines = []
        for blk in re.findall(r"<itemList>(.*?)</itemList>", raw, re.S):
            no = pick(blk, "rtNm")
            if not no:
                continue
            try:
                sec = int(pick(blk, "traTime1") or 9999)
            except ValueError:
                sec = 9999
            lines.append({"no": no, "to": pick(blk, "adirection"), "sec": sec,
                          # 아래 셋은 이 노선 하나만 따로 조회할 때 쓰는 열쇠입니다
                          "stId": pick(blk, "stId"), "rtId": pick(blk, "busRouteId"),
                          "ord": pick(blk, "staOrd"),
                          "a": bus_msg(pick(blk, "arrmsg1")),
                          "b": bus_msg(pick(blk, "arrmsg2"))})

        # 지정한 노선을 맨 위로, 나머지는 빨리 오는 순서로
        top = [x.strip() for x in st.get("우선노선", [])]
        lines.sort(key=lambda x: (top.index(x["no"]) if x["no"] in top else len(top), x["sec"]))
        out.append({"ars": ars, "name": st.get("이름", ""), "dir": st.get("방면", ""),
                    "pin": top, "lines": lines[: int(st.get("표시개수", 10))]})

    if not out:
        return save_sample() if portal.get("샘플데이터", False) else None
    _bus_calls[0] += len(out)

    # 정류소 하나만 본 경우, 나머지 정류소가 사라지지 않도록 그 자리만 바꿉니다.
    if only_ars:
        try:
            old = json.loads(BUSOUT.read_text(encoding="utf-8")).get("stops", [])
        except Exception:
            old = []
        if old:
            fresh = {x["ars"]: x for x in out}
            out = [fresh.get(x["ars"], x) for x in old]

    BUSOUT.write_text(json.dumps(
        {"stops": out, "at": datetime.now().strftime("%H:%M:%S")}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    log("🚌", "버스 " + " · ".join(f"{x['name']} {len(x['lines'])}개" for x in out)
             + f" (오늘 {_bus_calls[0]}건)")
    return out


# ─────────────────────── 대기질 · 자외선 ───────────────────────
# 둘 다 기상청·환경공단의 별개 서비스라 각각 활용신청이 필요합니다.
# 승인 전에는 설정의 "샘플데이터"가 켜져 있으면 예시값을 쓰고,
# 화면에는 '샘플' 표시가 붙습니다.
AIR_URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty"
# 생활기상지수는 버전이 올라가면 예전 주소가 폐기됩니다.
# (V4 는 "해당 오픈API 서비스가 없거나 폐기됨" 을 돌려줍니다 — 지금은 V5)
UV_URL = "https://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5"

GRADE = {"1": "좋음", "2": "보통", "3": "나쁨", "4": "매우나쁨"}


def uv_word(v: int) -> str:
    """자외선지수를 말로 바꿉니다 (기상청 구간 기준)"""
    if v >= 11: return "위험"
    if v >= 8: return "매우높음"
    if v >= 6: return "높음"
    if v >= 3: return "보통"
    return "낮음"


def collect_air(cfg: dict):
    """미세먼지 · 초미세먼지"""
    portal = cfg.get("공공데이터포털", {})
    key = portal.get("인증키", "")
    conf = portal.get("대기질", {}) or {}
    st = conf.get("측정소", "")
    if not key or not st:
        return None
    # 에어코리아 서버가 이따금 응답하지 않아 몇 번 다시 시도합니다.
    qs = urllib.parse.urlencode(
        {"stationName": st, "dataTerm": "DAILY", "returnType": "json",
         "numOfRows": 1, "pageNo": 1, "ver": "1.0", "serviceKey": key}, safe="%")
    last = ""
    for _ in range(3):
        try:
            j = json.loads(get(f"{AIR_URL}?{qs}", timeout=25).read().decode("utf-8"))
            it = j["response"]["body"]["items"][0]
            pm10, pm25 = it.get("pm10Value"), it.get("pm25Value")
            if not pm10 or pm10 == "-":
                raise ValueError(f"{st} 측정소에 값이 없습니다")
            return {"pm10": int(pm10), "pm10g": GRADE.get(str(it.get("pm10Grade")), "—"),
                    "pm25": int(pm25), "pm25g": GRADE.get(str(it.get("pm25Grade")), "—"),
                    "st": st, "at": (it.get("dataTime") or "")[-5:], "sample": False}
        except IndexError:
            last = f"'{st}' 측정소를 찾지 못했습니다 (구 단위 이름인지 확인하세요)"
            break
        except Exception as e:
            last = str(e)[:55]
            time.sleep(2)
    log("⏭", f"미세먼지: {last}")
    return None


def collect_uv(cfg: dict):
    """자외선지수 — 3시간 간격 발표라 지금 시각에 가장 가까운 값을 씁니다."""
    portal = cfg.get("공공데이터포털", {})
    key = portal.get("인증키", "")
    conf = portal.get("자외선", {}) or {}
    area = conf.get("지역코드", "")
    if not key or not area:
        return None
    now = datetime.now()
    for back in (0, 3, 6):
        base = now - timedelta(hours=back)
        h = (base.hour // 3) * 3
        try:
            qs = urllib.parse.urlencode(
                {"areaNo": area, "time": base.strftime("%Y%m%d") + f"{h:02d}",
                 "dataType": "JSON", "numOfRows": 10, "pageNo": 1, "serviceKey": key}, safe="%")
            j = json.loads(get(f"{UV_URL}?{qs}", timeout=25).read().decode("utf-8"))
            it = j["response"]["body"]["items"]["item"][0]
            # h0 가 지금, h3·h6… 은 세 시간 간격의 예보입니다.
            # 밤에는 0 이라, 오늘 남은 시간 중 가장 높은 값도 같이 보냅니다.
            v = int(it.get("h0") or 0)
            rest = [int(it[k]) for k in ("h3", "h6", "h9", "h12", "h15", "h18")
                    if str(it.get(k, "")).isdigit()]
            top = max(rest) if rest else v
            return {"uv": v, "word": uv_word(v),
                    "max": top, "maxWord": uv_word(top), "sample": False}
        except Exception:
            continue
    log("⏭", "자외선: 받지 못했습니다")
    return None


def sample_air():
    return {"pm10": 21, "pm10g": "좋음", "pm25": 12, "pm25g": "좋음",
            "st": "공덕동", "at": "", "sample": True}


def sample_uv():
    h = datetime.now().hour
    v = 0 if h < 7 or h > 19 else (2 if h < 10 or h > 17 else 6)
    return {"uv": v, "word": uv_word(v), "max": 6, "maxWord": uv_word(6), "sample": True}


def sample_bus(cfg: dict):
    """버스 승인 전에 화면을 확인하기 위한 예시입니다."""
    stops = ((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("정류장", []))
    demo = {
        "14112": [("9711", "연신내", 193), ("7011", "서울역", 72), ("271", "중랑", 302),
                  ("7730", "김포", 455), ("7715", "은평", 880), ("7016", "구파발", 512),
                  ("7013", "은평", 640), ("마포08", "공덕", 725), ("673", "강서", 1010),
                  ("171", "하계", 1180)],
        "14335": [("710", "수색", 150), ("7727", "상암", 410), ("마포08", "공덕", 600),
                  ("7013", "은평", 745), ("271", "중랑", 933), ("7019", "digital", 288),
                  ("673", "강서", 1055), ("마포16", "망원", 360), ("6716", "김포", 820),
                  ("7711", "은평", 1240)],
    }
    out = []
    for st in stops:
        ars = str(st.get("ARS", ""))
        rows = demo.get(ars) or demo["14112"]
        top = [x.strip() for x in st.get("우선노선", [])]
        lines = []
        for no, to, sec in rows:
            m, ss = divmod(sec, 60)
            lines.append({"no": no, "to": to, "sec": sec,
                          "a": {"when": "곧 도착" if sec < 90 else f"{m}분 {ss:02d}초",
                                "where": f"{max(1, sec // 120)}정거장", "soon": sec < 90},
                          "b": {"when": f"{m + 9}분 {ss:02d}초", "where": "", "soon": False}})
        lines.sort(key=lambda x: (top.index(x["no"]) if x["no"] in top else len(top), x["sec"]))
        out.append({"ars": ars, "name": st.get("이름", ""), "dir": st.get("방면", ""),
                    "pin": top, "lines": lines[: int(st.get("표시개수", 10))], "sample": True})
    return out or None


# ─────────────────────── 하단바 흐름(속보) ───────────────────────
# 속보 전용 RSS 는 사실상 없어서, 제목에 붙는 [속보]·[1보] 표시로 골라냅니다.
# 속보가 하나도 없는 시간대가 더 많으므로, 그때 무엇을 흘릴지는 설정에서 정합니다.
def collect_ticker(cfg: dict):
    """
    하단바에 흘릴 속보를 고릅니다.
    카드로 도는 언론사 전부에서 찾습니다 — 속보는 어디서 나왔든 알아야 하니까요.
    이미 카드용으로 받아 둔 기사를 다시 쓰므로 API 를 더 부르지 않습니다.
    (설정의 하단바.피드 에 주소를 적으면 그것만 따로 봅니다)
    """
    conf = cfg.get("하단바", {}) or {}
    feeds = conf.get("피드", [])

    if feeds:
        with ThreadPoolExecutor(max_workers=4) as ex:
            items = [x for r in ex.map(parse_feed, feeds) for x in r]
    else:
        items = list(_seen_items)
    if not items:
        return []

    drop = (cfg.get("뉴스", {}) or {}).get("제외키워드", [])
    if drop:
        items = [n for n in items if not any(w in n["title"] for w in drop)]

    marks = conf.get("속보표시", ["[속보]"])
    hot_fresh = int(conf.get("속보최근분", 240))     # 이 시간이 지난 속보는 버립니다
    fresh = int(conf.get("최근분", 120))
    limit = int(conf.get("건수", 6))

    def strip_mark(t: str) -> str:
        for m in marks:
            t = t.replace(m, "")
        return re.sub(r"\s+", " ", t).strip()

    seen, hot, new = set(), [], []
    for n in sorted(items, key=lambda x: x["min"]):
        key = re.sub(r"[^\w가-힣]", "", n["title"])[:20]
        if key in seen:
            continue
        seen.add(key)
        who = n.get("press", "")
        row = {"t": strip_mark(n["title"]), "ago": ago_text(n["min"]),
               "p": SHORT_PRESS.get(who, who)}
        if any(m in n["title"] for m in marks):
            if n["min"] <= hot_fresh:
                hot.append({**row, "hot": True})
        elif n["min"] <= fresh:
            new.append({**row, "hot": False})

    out = hot[:limit]
    if not out and conf.get("속보없을때", "최신") == "최신":
        out = new[:limit]
    kind = "속보" if hot else ("최신" if out else "없음")
    who = " · ".join(sorted({x["p"] for x in out if x.get("p")}))
    log("📢", f"하단바 {len(out)}건 · {kind}" + (f" ({who})" if who else "") +
              (f" (속보는 {hot_fresh}분 안에 없었습니다)" if not hot and out else ""))
    return out


# ────────────────────────────── 저장 ──────────────────────────────
def save(press, weather, bus=None, ticker=None):
    # 버스는 짧은 주기로 따로 돌기 때문에, 이번 차례에 건너뛰었을 수 있습니다.
    # 그럴 때는 방금 받아 둔 버스.json 을 그대로 씁니다 (화면에서 카드가 사라지지 않도록).
    if not bus:
        try:
            bus = json.loads(BUSOUT.read_text(encoding="utf-8")).get("stops", [])
        except Exception:
            bus = []
    total = sum(len(p["items"]) for p in press)
    sweep_images({n["image"] for p in press for n in p["items"] if n.get("image")})
    used = sum(f.stat().st_size for f in IMGDIR.glob("*.jpg")) if IMGDIR.exists() else 0
    if used:
        log("🖼", f"이미지 폴더 {used / 1024 / 1024:.1f} MB")
    payload = {"press": press, "weather": weather, "bus": bus or [],
               "busAt": datetime.now().strftime("%H:%M:%S") if bus else "",
               "ticker": ticker or [],
               "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    OUT.write_text(
        "/* 수집기.py 가 자동 생성합니다. 직접 고치지 마세요. */\n"
        "window.SIGNAGE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    log("💾", f"저장 완료 → 콘텐츠/데이터.js (언론사 {len(press)}곳 · 기사 {total}건 · 날씨 {'○' if weather else '×'})")


def collect_once(cfg=None):
    # 켜 둔 채로 설정을 고치는 일이 잦아, 수집할 때마다 다시 읽습니다.
    cfg = load_conf()
    _frame_cache.clear()
    press = collect_news(cfg)

    # 클라우드(GitHub Actions)에서는 공공데이터포털이 해외라 응답하지 않습니다.
    # 날씨·버스는 화면이 Cloudflare 중계소에서 직접 받아오므로 여기서는 건너뜁니다.
    if "--뉴스만" in sys.argv or os.environ.get("NEWS_ONLY"):
        if press:
            save(press, None, None, collect_ticker(cfg))
        else:
            log("⚠️", "수집된 기사가 없습니다.")
        return

    weather = collect_weather(cfg)
    bus = collect_bus(cfg)
    ticker = collect_ticker(cfg)
    if not press and not weather:
        log("⚠️", "수집된 데이터가 없습니다.")
        return
    save(press, weather, bus, ticker)


def next_slot_at(period_min: int) -> datetime:
    """다음 수집 시각(매시 00·10·20…)을 돌려줍니다."""
    now = datetime.now()
    nxt = (now.minute // period_min + 1) * period_min
    if nxt >= 60:
        return now.replace(minute=0, second=5, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=nxt, second=5, microsecond=0)


def sleep_until_next_slot(period_min: int) -> None:
    """
    다음 정각 구간까지 기다립니다.
    period_min=10 이면 매시 00·10·20·30·40·50분에 수집합니다.
    (화면 새로고침 주기와 어긋나지 않도록 시각을 고정합니다)
    """
    now = datetime.now()
    nxt = (now.minute // period_min + 1) * period_min
    if nxt >= 60:
        target = now.replace(minute=0, second=5, microsecond=0) + timedelta(hours=1)
    else:
        target = now.replace(minute=nxt, second=5, microsecond=0)
    wait = (target - now).total_seconds()
    log("⏳", f"다음 수집 {target:%H:%M} (약 {int(wait // 60)}분 {int(wait % 60)}초 후)")
    time.sleep(max(1, wait))


def main():
    cfg = load_conf()
    if "--반복" in sys.argv or "--loop" in sys.argv:
        claim_single()
        period = int(cfg.get("수집주기분", 10))
        bus_sec = int((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("갱신초", 300))
        stops = len((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("정류장", []))
        span = (cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("운영시간", "종일")
        boost_sec = int((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("집중갱신초", 30))
        hold = int((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("집중유지분", 30))
        log("🔁", f"뉴스·날씨는 매시 {period}분 단위(00·{period:02d}·{period*2:02d}…), "
                  f"버스는 {span} 사이 {bus_sec}초마다 수집합니다. 종료는 Ctrl+C")
        log("⚡", f"버스 카드에서 리모컨 확인 버튼을 누르면 {hold}분 동안 {boost_sec}초마다 갱신합니다")
        if stops:
            hours = 24
            try:
                a, b = [x.strip() for x in span.split("-")]
                hours = (int(b[:2]) - int(a[:2])) % 24 or 24
            except Exception:
                pass
            base = int(3600 / bus_sec * hours * stops)
            once = int(60 / boost_sec * hold * stops)
            rsec = int((cfg.get("공공데이터포털", {}).get("버스", {}) or {}).get("노선집중갱신초", 10))
            one = int(60 / boost_sec * hold)          # 정류소 하나만 볼 때
            route = int(60 / rsec * hold)             # 노선 하나만 볼 때
            log("📊", f"예상 사용량 하루 약 {base:,}건 · 집중 갱신 1회당 "
                      f"노선 하나 {route}건({rsec}초), 정류소 하나 {one}건, 전체 {once}건 "
                      f"(개발계정 한도 1,000건)")
        try:
            collect_once(cfg)          # 시작하자마자 한 번
            while True:
                # 다음 뉴스 수집 시각까지, 버스만 짧게 여러 번 갱신합니다
                nxt = next_slot_at(period)
                while datetime.now() < nxt:
                    if datetime.now().day != _bus_day[0]:      # 날짜가 바뀌면 사용량 초기화
                        _bus_day[0], _bus_calls[0] = datetime.now().day, 0
                    cfg = load_conf()          # 설정 변경을 바로 반영
                    collect_bus(cfg)
                    wait = bus_period(cfg)        # 확인 버튼을 누르면 여기서 짧아집니다
                    nap(min(wait, max(1, (nxt - datetime.now()).total_seconds())))
                collect_once(cfg)
        except KeyboardInterrupt:
            print()
            log("⏹", "종료합니다.")
    else:
        collect_once(cfg)


if __name__ == "__main__":
    main()
