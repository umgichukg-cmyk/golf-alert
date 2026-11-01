// 원더클럽 + 마에스트로CC 빈자리 알림 (Cloudflare Workers 버전)
//
// - scheduled: 5분마다 잔여 팀 수를 확인하고, 늘어난 곳이 있으면 텔레그램으로 알림
// - fetch(/telegram): 텔레그램 웹훅. 명령에 바로 답장
// 설정과 이전 조회 결과는 KV(GOLF)에 저장합니다.
// 예약 자체는 하지 않습니다. 알림의 링크를 눌러 직접 예약하세요.

const BASE = "https://www.onetheclub.com";
const MAESTRO = "https://www.maestrocc.co.kr";
const WEEKDAYS = "월화수목금토일";
const MAX_DATES = 20; // 무료 플랜은 실행 1회당 외부 요청 50개 제한 → 날짜 수를 제한
const UA = "Mozilla/5.0 (personal tee-time alert; checks every few minutes)";

const ALL_COURSES = [
  "클럽72CC 레이크", "클럽72CC 클래식", "클럽72CC 오션", "클럽72CC 하늘+연습장",
  "신라CC", "파주CC", "듄스", "제주아덴힐", "해피니스CC", "해피니스CC(회원제)",
  "하이원CC", "세이지우드 홍천", "세이지우드 여수", "파인힐스", "사우스스프링스", "크리스밸리",
  "마에스트로CC 레이크", "마에스트로CC 밸리",
];
const ONETHECLUB_COURSES = ALL_COURSES.filter((c) => !c.startsWith("마에스트로"));
const MAESTRO_COURSES = ALL_COURSES.filter((c) => c.startsWith("마에스트로"));

const DEFAULT_CONFIG = {
  courses: ["클럽72", "신라", "파주", "마에스트로"],
  dates: [],
  weekdays: ["토", "일"],
  days_ahead: 21,
  min_teams: 1,
  paused: false,
};

// ---------------------------------------------------------------- 날짜 (한국 시간)

function todayKST() {
  const now = new Date(Date.now() + 9 * 3600 * 1000);
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}
const iso = (d) => d.toISOString().slice(0, 10);
const addDays = (d, n) => new Date(d.getTime() + n * 86400000);
const weekdayKo = (d) => WEEKDAYS[(d.getUTCDay() + 6) % 7]; // 월=0 … 일=6
const dayLabel = (d) =>
  `${String(d.getUTCMonth() + 1).padStart(2, "0")}/${String(d.getUTCDate()).padStart(2, "0")}(${weekdayKo(d)})`;

function targetDates(cfg) {
  const base = todayKST();
  const set = new Set(cfg.dates || []);
  const wanted = new Set(cfg.weekdays || []);
  for (let i = 1; i <= Number(cfg.days_ahead || 14); i++) {
    const d = addDays(base, i);
    if (wanted.has(weekdayKo(d))) set.add(iso(d));
  }
  return [...set]
    .filter((s) => s > iso(base))
    .sort()
    .slice(0, MAX_DATES)
    .map((s) => new Date(s + "T00:00:00Z"));
}

const courseWanted = (name, cfg) => !cfg.courses?.length || cfg.courses.some((k) => name.includes(k));

// ---------------------------------------------------------------- 사이트 조회

async function fetchText(url, { form, encoding = "utf-8" } = {}) {
  const init = { headers: { "User-Agent": UA, Referer: url.split("/").slice(0, 3).join("/") + "/" } };
  if (form) {
    init.method = "POST";
    init.body = new URLSearchParams(form).toString();
    init.headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8";
    init.headers["X-Requested-With"] = "XMLHttpRequest";
  }
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return new TextDecoder(encoding).decode(await res.arrayBuffer());
}

const unescapeHtml = (s) =>
  s.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").trim();
const parseTeams = (s) => Number((s.match(/(\d+)\s*팀/) || [])[1] || 0);

async function checkOnetheclub(day) {
  const ymd = iso(day).replace(/-/g, "");
  const page = await fetchText(BASE + "/reservation/ajax/mainGolfDetail", {
    form: { workMonth: ymd.slice(0, 6), workDate: ymd, days: "on" },
  });
  const re =
    /<li class="(result_\w+)">\s*<a href="([^"]+)">\s*<span class="c_name">([^<]+)<\/span>[\s\S]*?<span class="c_team"[^>]*>([^<]*)<\/span>/g;
  const result = {};
  for (const [, , href, name, team] of page.matchAll(re)) {
    result[unescapeHtml(name)] = [parseTeams(unescapeHtml(team)), BASE + href];
  }
  // 듄스는 별도 API로 잔여 팀 수를 줍니다.
  if (result["듄스"]) {
    let teams = 0;
    try {
      const rows = JSON.parse(await fetchText(`${BASE}/api/dunes?minDate=${iso(day)}&maxDate=${iso(day)}`));
      if (rows?.[0]?.is_open) teams = Number(rows[0].available_teams) || 0;
    } catch {}
    result["듄스"][0] = teams;
  }
  return result;
}

async function checkMaestro() {
  // 메인 달력 한 번에 예약 가능한 3주치가 들어옵니다.
  const page = await fetchText(MAESTRO + "/GolfRes/mainpage/main_calendar_line_ajax_view.asp", {
    form: { golfrestype: "real", schDate: "", usrmemcd: "90", toDay: "", calnum: "1" },
    encoding: "euc-kr",
  });
  const link = MAESTRO + "/pagesite/reservation/live.asp";
  const result = {};
  for (const [, date, names, teams] of page.matchAll(/mainDivCalendrShow\(\d+,\s*'([\d-]+)','([^']*)',\s*'([^']*)'/g)) {
    const counts = teams.split("|");
    result[date] = {};
    names.split("|").forEach((n, i) => {
      result[date]["마에스트로CC " + n.replace("코스", "")] = [Number(counts[i]) || 0, link];
    });
  }
  return result;
}

async function scan(cfg) {
  const rows = [];
  const errors = [];
  const dates = targetDates(cfg);
  let maestro = {};
  if (MAESTRO_COURSES.some((c) => courseWanted(c, cfg))) {
    try {
      maestro = await checkMaestro();
    } catch (e) {
      errors.push(`마에스트로CC 조회 실패: ${e.message}`);
    }
  }
  const needOnetheclub = ONETHECLUB_COURSES.some((c) => courseWanted(c, cfg));
  for (const day of dates) {
    let courses = {};
    if (needOnetheclub) {
      try {
        courses = await checkOnetheclub(day);
      } catch (e) {
        errors.push(`${dayLabel(day)} 원더클럽 조회 실패: ${e.message}`);
      }
    }
    Object.assign(courses, maestro[iso(day)] || {});
    for (const [name, [teams, link]] of Object.entries(courses)) {
      if (courseWanted(name, cfg)) rows.push({ day, name, teams, link });
    }
  }
  return { rows, errors };
}

function findNewOpenings(cfg, rows, counts) {
  const newCounts = {};
  const found = [];
  for (const { day, name, teams, link } of rows) {
    const key = `${iso(day)}|${name}`;
    newCounts[key] = teams;
    const before = counts[key];
    if (teams >= (cfg.min_teams || 1) && before !== undefined && teams > before) {
      found.push(`⛳ ${dayLabel(day)} ${name}: ${before}팀 → ${teams}팀\n${link}`);
    }
  }
  return { newCounts, found };
}

function formatStatus(rows) {
  if (!rows.length) return "감시 중인 날짜·골프장이 없습니다. '설정'으로 조건을 확인하세요.";
  const lines = ["현재 잔여 팀"];
  let last = "";
  for (const { day, name, teams } of rows) {
    if (iso(day) !== last) {
      lines.push(`\n[${dayLabel(day)}]`);
      last = iso(day);
    }
    lines.push(`  ${name} ${teams ? teams + "팀" : "마감"}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------- 텔레그램

async function sendTelegram(env, text) {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: text.slice(0, 4000), disable_web_page_preview: true }),
  });
  if (!res.ok) console.log("텔레그램 전송 실패", res.status, await res.text());
}

const HELP = `⛳ 골프 빈자리 알림 명령어

현황 — 지금 잔여 팀 수 보기
설정 — 현재 감시 조건 보기
골프장 파주 신라 — 감시할 골프장 (이름 일부만, '전체' 가능)
요일 토 일 — 감시할 요일 ('없음' 가능)
날짜 11/15 11/22 — 특정 날짜 추가 ('없음'으로 비우기)
기간 21 — 앞으로 며칠까지 볼지
기준 3 — 몇 팀 이상일 때만 알릴지
중지 / 재개 — 알림 잠시 멈추기 / 다시 켜기`;

function formatConfig(cfg) {
  return [
    "현재 감시 조건",
    `골프장: ${cfg.courses?.length ? cfg.courses.join(", ") : "전체"}`,
    `요일: ${cfg.weekdays?.join(" ") || "없음"} (앞으로 ${cfg.days_ahead}일)`,
    `특정 날짜: ${cfg.dates?.join(", ") || "없음"}`,
    `알림 기준: ${cfg.min_teams || 1}팀 이상`,
    `상태: ${cfg.paused ? "⏸ 중지됨" : "▶ 감시 중 (5분 간격)"}`,
  ].join("\n");
}

function parseDate(text) {
  const m = text.match(/^(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2})$/);
  if (!m) throw new Error(text);
  const today = todayKST();
  let year = m[1] ? Number(m[1]) : today.getUTCFullYear();
  const make = (y) => new Date(Date.UTC(y, Number(m[2]) - 1, Number(m[3])));
  let d = make(year);
  if (d.getUTCMonth() !== Number(m[2]) - 1) throw new Error(text); // 13/40 같은 날짜
  if (!m[1] && d < today) d = make(year + 1);
  return iso(d);
}

// 명령을 처리하고 { reply, status, changed }를 돌려줍니다. cfg는 그 자리에서 바뀝니다.
function handleCommand(cfg, text) {
  const words = text.trim().replace(/^\//, "").split(/\s+/).filter(Boolean);
  if (!words.length) return {};
  const [cmd, ...args] = [words[0].toLowerCase(), ...words.slice(1)];
  const ok = () => ({ reply: formatConfig(cfg), changed: true });
  if (["도움말", "help", "start", "명령어"].includes(cmd)) return { reply: HELP };
  if (["현황", "status"].includes(cmd)) return { status: true };
  if (["설정", "config"].includes(cmd)) return { reply: formatConfig(cfg) };
  if (["중지", "정지", "stop"].includes(cmd)) {
    cfg.paused = true;
    return { reply: "⏸ 알림을 멈췄습니다. '재개'를 보내면 다시 켜집니다.", changed: true };
  }
  if (["재개", "시작", "resume"].includes(cmd)) {
    cfg.paused = false;
    return { reply: "▶ 알림을 다시 켰습니다.", changed: true };
  }
  if (cmd === "골프장") {
    if (!args.length) return { reply: "예) 골프장 파주 신라\n고를 수 있는 곳:\n" + ALL_COURSES.join("\n") };
    cfg.courses = args.length === 1 && args[0] === "전체" ? [] : args;
    return ok();
  }
  if (cmd === "요일") {
    const none = args.length === 1 && args[0] === "없음";
    const days = [...new Set([...args.join("")].filter((c) => WEEKDAYS.includes(c)))];
    if (!days.length && !none) return { reply: "예) 요일 토 일  /  요일 금토일  /  요일 없음" };
    cfg.weekdays = none ? [] : days.sort((a, b) => WEEKDAYS.indexOf(a) - WEEKDAYS.indexOf(b));
    return ok();
  }
  if (cmd === "날짜") {
    if (args.length === 1 && args[0] === "없음") {
      cfg.dates = [];
      return ok();
    }
    if (!args.length) return { reply: "예) 날짜 11/15 11/22  /  날짜 없음" };
    try {
      const add = args.map(parseDate);
      cfg.dates = [...new Set([...(cfg.dates || []), ...add])].sort();
    } catch {
      return { reply: "날짜 형식을 모르겠어요.\n예) 날짜 11/15 11/22" };
    }
    return ok();
  }
  if (cmd === "기간" || cmd === "기준") {
    if (!/^\d+$/.test(args[0] || "")) return { reply: `예) ${cmd} ${cmd === "기간" ? "21" : "3"}` };
    const n = Number(args[0]);
    if (cmd === "기간") cfg.days_ahead = Math.max(1, Math.min(n, 60));
    else cfg.min_teams = Math.max(1, n);
    return ok();
  }
  return { reply: "모르는 명령이에요. '도움말'을 보내보세요." };
}

// ---------------------------------------------------------------- 저장소(KV)

async function loadConfig(env) {
  return { ...DEFAULT_CONFIG, ...((await env.GOLF.get("config", "json")) || {}) };
}
async function loadCounts(env) {
  return (await env.GOLF.get("counts", "json")) || {};
}
async function saveCounts(env, before, after) {
  // KV 무료 쓰기 한도(하루 1,000회)를 아끼려고 바뀌었을 때만 저장
  if (JSON.stringify(before) !== JSON.stringify(after)) await env.GOLF.put("counts", JSON.stringify(after));
}

async function runCheck(env, { forceStatus = false } = {}) {
  const cfg = await loadConfig(env);
  if (cfg.paused && !forceStatus) return "paused";
  const counts = await loadCounts(env);
  const { rows, errors } = await scan(cfg);
  errors.forEach((e) => console.log(e));
  const { newCounts, found } = findNewOpenings(cfg, rows, counts);
  // 조회에 실패한 항목은 이전 값을 유지해서, 다음에 '0 → n'으로 잘못 알리지 않게
  await saveCounts(env, counts, rows.length || !errors.length ? newCounts : counts);
  if (forceStatus) await sendTelegram(env, formatStatus(rows) + (errors.length ? "\n\n⚠ " + errors.join("\n⚠ ") : ""));
  if (found.length && !cfg.paused) await sendTelegram(env, "빈자리 알림\n\n" + found.join("\n\n"));
  return `${rows.length} rows, ${found.length} alerts, ${errors.length} errors`;
}

export default {
  async scheduled(event, env, ctx) {
    console.log(await runCheck(env));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== "/telegram" || request.method !== "POST") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();
    const msg = update.message;
    if (!msg?.text || String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok");

    const cfg = await loadConfig(env);
    const { reply, status, changed } = handleCommand(cfg, msg.text);
    if (changed) await env.GOLF.put("config", JSON.stringify(cfg));
    if (reply) await sendTelegram(env, reply);
    if (status) {
      await sendTelegram(env, "조회 중입니다... ⏳");
      ctx.waitUntil(runCheck(env, { forceStatus: true }));
    }
    return new Response("ok");
  },
};
