/**
 * 사이니지용 공공데이터 중계소
 *
 * 왜 필요한가
 *   공공데이터포털(apis.data.go.kr, ws.bus.go.kr)은 해외에서 응답하지 않습니다.
 *   GitHub Actions 는 서버가 미국에 있어 날씨조차 3분을 기다려도 받지 못했습니다.
 *   Cloudflare Workers 는 부르는 쪽과 가까운 곳에서 실행되므로,
 *   사이니지(한국)가 부르면 서울에서 돌면서 공공데이터를 국내 속도로 받아옵니다.
 *
 * 인증키는 Secret(DATA_GO_KR_KEY)에 있습니다. 이 코드에도, 화면에도 들어가지 않습니다.
 *
 * 창구
 *   GET /weather                지금 기온·시간별·주간·대기질·자외선
 *   GET /bus?ars=14112,14335    정류소별 버스 도착정보
 *   GET /health                 살아 있는지 확인
 */

const VILAGE = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0";
const MIDFCST = "https://apis.data.go.kr/1360000/MidFcstInfoService";
const UV_URL = "https://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5";
const AIR_URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty";
const BUS_URL = "http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid";

// 같은 값을 여러 번 받아오지 않도록 잠깐 담아 둡니다 (공공데이터 하루 한도를 아끼려고)
const CACHE_WEATHER = 600;   // 10분
const CACHE_BUS = 8;         // 8초 — 화면이 10초마다 물어보므로 대개 새 값이 옵니다

const SKY = { "1": ["맑음", "☀️"], "3": ["구름많음", "⛅"], "4": ["흐림", "☁️"] };
const PTY = { "1": ["비", "🌧"], "2": ["비/눈", "🌨"], "3": ["눈", "❄️"], "4": ["소나기", "🌦"] };
const SHORT = { "맑음": "맑음", "구름많음": "구름", "흐림": "흐림", "비": "비", "비/눈": "비눈", "눈": "눈", "소나기": "소나기" };
const GRADE = { "1": "좋음", "2": "보통", "3": "나쁨", "4": "매우나쁨" };
const DOW = ["일", "월", "화", "수", "목", "금", "토"];
const VEC16 = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
               "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"];

/* 한국 시각 — Workers 는 UTC 로 돕니다 */
function kst(offsetMin = 0) {
  return new Date(Date.now() + 9 * 3600e3 + offsetMin * 60e3);
}
const p2 = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getUTCFullYear()}${p2(d.getUTCMonth() + 1)}${p2(d.getUTCDate())}`;

async function callJson(url, params, key, timeoutMs = 12000) {
  const q = new URLSearchParams({ ...params, dataType: "JSON", numOfRows: "1000", pageNo: "1" });
  const full = `${url}?${q}&serviceKey=${key}`;      // 키는 이미 인코딩된 형태라 그대로 붙입니다
  const res = await fetch(full, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const j = await res.json();
  const code = j?.response?.header?.resultCode;
  if (code !== "00") throw new Error(j?.response?.header?.resultMsg || "no data");
  return j.response.body.items.item;
}

function skyOf(pty, sky) {
  return PTY[pty] || SKY[sky] || ["맑음", "☀️"];
}

function pcpMm(v) {
  const nums = String(v ?? "").match(/\d+\.?\d*/g);
  if (!nums) return 0;
  return Math.round((nums.reduce((a, b) => a + +b, 0) / nums.length) * 10) / 10;
}

/** 체감온도 — 여름은 습도, 겨울은 바람 (기상청 산출식) */
function feelsLike(t, rh, wind) {
  const m = kst().getUTCMonth() + 1;
  if (m >= 5 && m <= 9) {
    const tw = t * Math.atan(0.151977 * Math.sqrt(rh + 8.313659))
      + Math.atan(t + rh) - Math.atan(rh - 1.67633)
      + 0.00391838 * Math.pow(rh, 1.5) * Math.atan(0.023101 * rh) - 4.686035;
    return Math.round((-0.2442 + 0.55399 * tw + 0.45535 * t
      - 0.0022 * tw * tw + 0.00278 * tw * t + 3.0) * 10) / 10;
  }
  if (t > 10 || wind < 1.3) return Math.round(t * 10) / 10;
  const v = Math.pow(wind * 3.6, 0.16);
  return Math.round((13.12 + 0.6215 * t - 11.37 * v + 0.3965 * v * t) * 10) / 10;
}

/** 일출·일몰 (NOAA 근사식) — 별도 API 없이 계산합니다 */
function sunTimes(lat, lon) {
  const d = kst();
  const start = Date.UTC(d.getUTCFullYear(), 0, 0);
  const n = Math.floor((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - start) / 864e5);
  const rad = Math.PI / 180, deg = 180 / Math.PI;
  const lngHour = lon / 15;
  const out = [];
  for (const rising of [true, false]) {
    const t = n + ((rising ? 6 : 18) - lngHour) / 24;
    const M = 0.9856 * t - 3.289;
    let L = (M + 1.916 * Math.sin(M * rad) + 0.020 * Math.sin(2 * M * rad) + 282.634) % 360;
    if (L < 0) L += 360;
    let RA = (Math.atan(0.91764 * Math.tan(L * rad)) * deg) % 360;
    if (RA < 0) RA += 360;
    RA = (RA + (Math.floor(L / 90) * 90 - Math.floor(RA / 90) * 90)) / 15;
    const sinDec = 0.39782 * Math.sin(L * rad);
    const cosDec = Math.cos(Math.asin(sinDec));
    const cosH = (Math.cos(90.833 * rad) - sinDec * Math.sin(lat * rad)) / (cosDec * Math.cos(lat * rad));
    if (Math.abs(cosH) > 1) { out.push("--:--"); continue; }
    const H = rising ? 360 - Math.acos(cosH) * deg : Math.acos(cosH) * deg;
    let UT = (H / 15 + RA - 0.06571 * t - 6.622) % 24;
    UT = (UT - lngHour + 24) % 24;
    const k = (UT + 9) % 24;
    out.push(`${p2(Math.floor(k))}:${p2(Math.round((k % 1) * 60) % 60)}`);
  }
  return out;
}

function uvWord(v) {
  if (v >= 11) return "위험";
  if (v >= 8) return "매우높음";
  if (v >= 6) return "높음";
  if (v >= 3) return "보통";
  return "낮음";
}

/* ───────────────────────── 날씨 ───────────────────────── */
async function getWeather(env) {
  const key = env.DATA_GO_KR_KEY;
  const nx = env.GRID_X || "58", ny = env.GRID_Y || "127";
  const now = kst();
  const today = ymd(now);

  // ① 단기예보 — 시간별과 주간 앞부분의 재료
  const back = kst(-45);
  const vilH = [23, 20, 17, 14, 11, 8, 5, 2].find((h) => back.getUTCHours() >= h);
  const base = vilH === undefined
    ? { d: ymd(kst(-45 - 1440)), t: "2300" }
    : { d: ymd(back), t: `${p2(vilH)}00` };

  const rows = await callJson(`${VILAGE}/getVilageFcst`,
    { base_date: base.d, base_time: base.t, nx, ny }, key, 15000);

  const slots = {};
  for (const it of rows) {
    const k = it.fcstDate + it.fcstTime;
    (slots[k] ||= {})[it.category] = it.fcstValue;
  }
  const keys = Object.keys(slots).sort();
  if (!keys.length) throw new Error("예보 없음");

  // ② 초단기실황 — 지금 이 순간의 관측값 (예보보다 정확)
  let obs = {}, obsAt = "";
  for (const b of [kst(-41), kst(-101)]) {
    try {
      const n = await callJson(`${VILAGE}/getUltraSrtNcst`,
        { base_date: ymd(b), base_time: `${p2(b.getUTCHours())}00`, nx, ny }, key, 12000);
      obs = Object.fromEntries(n.map((x) => [x.category, x.obsrValue]));
      obsAt = `${p2(b.getUTCHours())}:00`;
      break;
    } catch { /* 아직 안 올라왔으면 한 시간 전 것으로 */ }
  }

  const near = slots[keys[0]];
  const temp = +(obs.T1H ?? near.TMP ?? 0);
  const hum = Math.round(+(obs.REH ?? near.REH ?? 0));
  const wind = +(obs.WSD ?? near.WSD ?? 0);
  const vec = Math.round(+(obs.VEC ?? near.VEC ?? 0));
  const [desc, icon] = skyOf(obs.PTY || near.PTY || "0", near.SKY || "1");

  // ③ 시간별 — 지금 이후 24칸
  const nowKey = `${today}${p2(now.getUTCHours())}00`;
  const hours = [];
  for (const k of keys) {
    if (k < nowKey || !slots[k].TMP || hours.length >= 24) continue;
    const s = slots[k];
    const [d2, i2] = skyOf(s.PTY || "0", s.SKY || "1");
    hours.push({
      h: +k.slice(8, 10), i: i2, s: SHORT[d2] || d2, t: Math.round(+s.TMP),
      d: `${+k.slice(4, 6)}.${+k.slice(6, 8)}`,
      pop: Math.round(+(s.POP || 0)), mm: pcpMm(s.PCP),
      rh: Math.round(+(s.REH || 0)), ws: Math.round(+(s.WSD || 0)),
      next: k.slice(0, 8) !== today,
    });
  }

  let tmn = null, tmx = null;
  for (const k of keys) {
    if (!k.startsWith(today)) continue;
    if (slots[k].TMN) tmn = Math.round(+slots[k].TMN);
    if (slots[k].TMX) tmx = Math.round(+slots[k].TMX);
  }
  const pool = hours.length ? hours.map((h) => h.t) : [Math.round(temp)];
  tmn ??= Math.min(...pool);
  tmx ??= Math.max(...pool);

  // ④ 주간 — 내일부터 6일. 앞 이틀은 단기예보, 나머지는 중기예보
  const dayBox = (d8) => {
    const lo = [], hi = [], am = [], pm = [];
    for (const k of keys) {
      if (!k.startsWith(d8) || !slots[k].TMP) continue;
      const s = slots[k];
      const [dd, ii] = skyOf(s.PTY || "0", s.SKY || "1");
      (+k.slice(8, 10) < 12 ? am : pm).push([ii, Math.round(+(s.POP || 0)), SHORT[dd] || dd]);
      if (s.TMN) lo.push(+s.TMN);
      if (s.TMX) hi.push(+s.TMX);
    }
    if (!lo.length || !hi.length) return null;
    const pick = (box) => {
      if (!box.length) return ["", 0, ""];
      const counts = {};
      box.forEach((x) => (counts[x[0]] = (counts[x[0]] || 0) + 1));
      const top = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
      return [top, Math.max(...box.map((x) => x[1])), box.find((x) => x[0] === top)[2]];
    };
    const [ai, ap, aw] = pick(am.length ? am : pm);
    const [pi, pp, pw] = pick(pm.length ? pm : am);
    return { amI: ai, amP: ap, amS: aw, pmI: pi, pmP: pp, pmS: pw,
             min: Math.round(Math.min(...lo)), max: Math.round(Math.max(...hi)) };
  };

  const week = [];
  for (let i = 1; i <= 6; i++) {
    const d = kst(i * 1440);
    week.push({
      d: `${d.getUTCMonth() + 1}.${d.getUTCDate()}.`, w: DOW[d.getUTCDay()],
      sun: d.getUTCDay() === 0,
      ...(dayBox(ymd(d)) || { amI: "", amP: 0, amS: "", pmI: "", pmP: 0, pmS: "", min: null, max: null }),
    });
  }

  if (week.some((x) => x.max === null)) {
    // 중기예보 항목 번호는 "오늘"이 아니라 "발표일" 기준입니다.
    // 새벽에는 전날 18시 발표를 쓰므로, 날짜 차이로 번호를 구해야 하루씩 밀리지 않습니다.
    const h = now.getUTCHours();
    const cands = h >= 18 ? [ymd(now) + "1800", ymd(now) + "0600", ymd(kst(-1440)) + "1800"]
      : h >= 6 ? [ymd(now) + "0600", ymd(kst(-1440)) + "1800"]
        : [ymd(kst(-1440)) + "1800", ymd(kst(-1440)) + "0600"];
    for (const tmFc of cands) {
      try {
        const L = (await callJson(`${MIDFCST}/getMidLandFcst`,
          { regId: env.MID_LAND || "11B00000", tmFc }, key, 12000))[0];
        const T = (await callJson(`${MIDFCST}/getMidTa`,
          { regId: env.MID_TA || "11B10101", tmFc }, key, 12000))[0];
        const baseDay = Date.UTC(+tmFc.slice(0, 4), +tmFc.slice(4, 6) - 1, +tmFc.slice(6, 8));
        for (let i = 1; i <= 6; i++) {
          const x = week[i - 1];
          if (x.max !== null) continue;
          const tgt = kst(i * 1440);
          const n = Math.round((Date.UTC(tgt.getUTCFullYear(), tgt.getUTCMonth(), tgt.getUTCDate()) - baseDay) / 864e5);
          if (n < 3 || n > 10 || !T[`taMax${n}`]) continue;
          const am = L[`wf${n}Am`] || L[`wf${n}`] || "";
          const pm = L[`wf${n}Pm`] || L[`wf${n}`] || "";
          const word = (s) => s.includes("눈") ? "눈" : s.includes("소나기") ? "소나기"
            : s.includes("비") ? "비" : s.includes("흐") ? "흐림" : s.includes("구름") ? "구름" : "맑음";
          x.amS = word(am); x.pmS = word(pm);
          x.amP = +(L[`rnSt${n}Am`] ?? L[`rnSt${n}`] ?? 0);
          x.pmP = +(L[`rnSt${n}Pm`] ?? L[`rnSt${n}`] ?? 0);
          x.min = +T[`taMin${n}`]; x.max = +T[`taMax${n}`];
        }
        break;
      } catch { /* 그 발표가 아직이면 이전 발표로 */ }
    }
  }

  // ⑤ 대기질·자외선 — 없어도 화면은 뜨도록 실패를 삼킵니다
  let air = null, uv = null;
  // 에어코리아는 이따금 응답하지 않아 몇 번 다시 물어봅니다.
  for (let i = 0; i < 3 && !air; i++) {
    try {
      const q = new URLSearchParams({
        stationName: env.AIR_STATION || "마포구", dataTerm: "DAILY",
        returnType: "json", numOfRows: "1", pageNo: "1", ver: "1.0",
      });
      const r = await fetch(`${AIR_URL}?${q}&serviceKey=${key}`, { signal: AbortSignal.timeout(20000) });
      const it = (await r.json())?.response?.body?.items?.[0];
      if (it && it.pm10Value && it.pm10Value !== "-") {
        air = { pm10: +it.pm10Value, pm10g: GRADE[it.pm10Grade] || "—",
                pm25: +it.pm25Value, pm25g: GRADE[it.pm25Grade] || "—",
                st: env.AIR_STATION || "마포구", at: (it.dataTime || "").slice(-5), sample: false };
      }
    } catch { /* 잠시 뒤 다시 */ }
  }

  try {
    for (const b of [now, kst(-180), kst(-360)]) {
      const t = ymd(b) + p2(Math.floor(b.getUTCHours() / 3) * 3);
      try {
        const it = (await callJson(UV_URL, { areaNo: env.UV_AREA || "1144000000", time: t }, key, 12000))[0];
        const v = +(it.h0 || 0);
        const rest = ["h3", "h6", "h9", "h12", "h15", "h18"].map((k) => +it[k]).filter((x) => !isNaN(x));
        const top = rest.length ? Math.max(...rest) : v;
        uv = { uv: v, word: uvWord(v), max: top, maxWord: uvWord(top), sample: false };
        break;
      } catch { /* 이전 발표로 */ }
    }
  } catch { /* 자외선도 없어도 그만 */ }

  const [rise, set] = sunTimes(+(env.LAT || 37.5794), +(env.LON || 126.8895));
  return {
    location: env.LOCATION || "서울 마포구 상암동",
    temp: Math.round(temp * 10) / 10, desc, icon,
    feel: feelsLike(temp, hum, wind), humidity: hum,
    wind: Math.round(wind * 10) / 10, windDir: VEC16[Math.floor(((vec + 11.25) % 360) / 22.5)],
    min: tmn, max: tmx, sunrise: rise, sunset: set,
    date: `${p2(now.getUTCMonth() + 1)}.${p2(now.getUTCDate())}`,
    hours, week, obsAt, air, uv,
    at: `${p2(now.getUTCHours())}:${p2(now.getUTCMinutes())}:${p2(now.getUTCSeconds())}`,
  };
}

/* ───────────────────────── 버스 ───────────────────────── */
function busMsg(raw) {
  const t = (raw || "").trim();
  if (!t || t === "출발대기" || t === "운행종료") return { when: t || "정보 없음", where: "", soon: false };
  const m = t.match(/(.*?)\s*\[(.*?)\]/);
  let when = m ? m[1].trim() : t;
  let where = m ? m[2].trim() : "";
  where = where.replace(/(\d+)\s*번째\s*전/, "$1정거장");
  const soon = when.includes("곧") || /^[01]분/.test(when);
  when = when.replace(/(\d+)분(\d+)초후/, "$1분 $2초").replace(/(\d+)분후/, "$1분");
  return { when, where, soon };
}

const pick = (blk, tag) => (blk.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`)) || [, ""])[1].trim();

async function getBus(env, arsList, pinMap) {
  const key = env.DATA_GO_KR_KEY;
  const stops = [];
  for (const ars of arsList) {
    const res = await fetch(`${BUS_URL}?arsId=${encodeURIComponent(ars)}&serviceKey=${key}`,
      { signal: AbortSignal.timeout(12000) });
    const raw = await res.text();
    const code = (raw.match(/<headerCd>(.*?)<\/headerCd>/) || [, "?"])[1];
    if (code !== "0") {
      const msg = (raw.match(/<headerMsg>(.*?)<\/headerMsg>/) || [, "?"])[1];
      stops.push({ ars, name: "", dir: "", pin: [], lines: [], error: msg });
      continue;
    }
    const lines = [];
    let stNm = "";
    for (const blk of raw.match(/<itemList>[\s\S]*?<\/itemList>/g) || []) {
      const no = pick(blk, "rtNm");
      if (!no) continue;
      stNm ||= pick(blk, "stNm");
      // sec·sec2 는 화면에서 1초씩 세어 내려가는 데 씁니다 (첫차·둘째차 각각)
      lines.push({
        no, to: pick(blk, "adirection"),
        sec: +(pick(blk, "traTime1") || 0) || null,
        sec2: +(pick(blk, "traTime2") || 0) || null,
        stId: pick(blk, "stId"), rtId: pick(blk, "busRouteId"), ord: pick(blk, "staOrd"),
        a: busMsg(pick(blk, "arrmsg1")), b: busMsg(pick(blk, "arrmsg2")),
      });
    }
    const top = pinMap[ars] || [];
    lines.sort((x, y) => {
      const a = top.indexOf(x.no), b = top.indexOf(y.no);
      return (a < 0 ? top.length : a) - (b < 0 ? top.length : b)
        || (x.sec ?? 99999) - (y.sec ?? 99999);
    });
    stops.push({ ars, name: stNm, dir: "", pin: top, lines });
  }
  const now = kst();
  return { stops, ms: Date.now(),
           at: `${p2(now.getUTCHours())}:${p2(now.getUTCMinutes())}:${p2(now.getUTCSeconds())}` };
}

/* ───────────────────────── 입구 ───────────────────────── */
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

function json(body, seconds) {
  return new Response(JSON.stringify(body), {
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8",
               "Cache-Control": `public, max-age=${seconds}` },
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    if (url.pathname === "/health") {
      return json({ ok: true, at: kst().toISOString() }, 0);
    }

    // 같은 값을 반복해서 받아오지 않도록 잠깐 담아 둡니다.
    const cache = caches.default;
    const hit = await cache.match(request);
    if (hit) return hit;

    try {
      let res;
      if (url.pathname === "/weather") {
        res = json(await getWeather(env), CACHE_WEATHER);
      } else if (url.pathname === "/bus") {
        const arsList = (url.searchParams.get("ars") || env.BUS_ARS || "")
          .split(",").map((s) => s.trim()).filter(Boolean).slice(0, 5);
        if (!arsList.length) return json({ error: "ars 를 알려주세요" }, 0);
        const pinMap = JSON.parse(env.BUS_PIN || "{}");
        res = json(await getBus(env, arsList, pinMap), CACHE_BUS);
      } else {
        return json({ error: "없는 주소입니다", 창구: ["/weather", "/bus?ars=14112,14335", "/health"] }, 0);
      }
      ctx.waitUntil(cache.put(request, res.clone()));
      return res;
    } catch (e) {
      // 무엇이 잘못됐는지 화면에서도 볼 수 있게 그대로 돌려줍니다.
      return json({ error: String(e && e.message || e) }, 0);
    }
  },
};
