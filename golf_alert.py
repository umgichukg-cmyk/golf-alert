"""원더클럽(onetheclub.com) + 마에스트로CC(maestrocc.co.kr) 빈자리 알림.

각 사이트 메인 화면에 공개된 '골프장별 잔여 팀 수'를 주기적으로 확인하고,
마감이던 곳에 자리가 생기거나 팀 수가 늘면 텔레그램으로 알려줍니다.
예약 자체는 하지 않습니다. 알림을 받으면 링크를 눌러 직접 예약하세요.

사용법:
    python golf_alert.py                   # PC에서 계속 감시
    python golf_alert.py --once            # 한 번만 조회해서 현황 출력
    python golf_alert.py --cloud           # 한 번 실행(텔레그램 명령 처리 + 조회 + 알림). GitHub Actions용
    python golf_alert.py --telegram-setup  # 텔레그램 chat_id 찾기 + 테스트 메시지

텔레그램 토큰과 chat_id는 환경변수(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)나
secrets.json에서 읽습니다. secrets.json은 절대 GitHub에 올리지 마세요.
"""
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.onetheclub.com"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SECRETS_PATH = HERE / "secrets.json"
STATE_PATH = HERE / "state.json"
WEEKDAYS = "월화수목금토일"
MIN_INTERVAL = 5  # 서버 부담을 줄이기 위한 최소 간격(분)
REQUEST_GAP = 1.5  # 날짜별 요청 사이 대기(초)
KST = dt.timezone(dt.timedelta(hours=9))  # 클라우드 서버는 UTC라서 한국 시간을 따로 씁니다

ALL_COURSES = [
    "클럽72CC 레이크", "클럽72CC 클래식", "클럽72CC 오션", "클럽72CC 하늘+연습장",
    "신라CC", "파주CC", "듄스", "제주아덴힐", "해피니스CC", "해피니스CC(회원제)",
    "하이원CC", "세이지우드 홍천", "세이지우드 여수", "파인힐스", "사우스스프링스", "크리스밸리",
]
MAESTRO_COURSES = ["마에스트로CC 레이크", "마에스트로CC 밸리"]
ALL_COURSES += MAESTRO_COURSES

ITEM_RE = re.compile(
    r'<li class="(result_\w+)">\s*<a href="([^"]+)">\s*'
    r'<span class="c_name">([^<]+)</span>.*?<span class="c_team"[^>]*>([^<]*)</span>',
    re.S,
)

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (personal tee-time alert; checks every few minutes)"),
    ("Referer", BASE + "/"),
]


def fetch(url, data=None, encoding="utf-8"):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        req.add_header("X-Requested-With", "XMLHttpRequest")
    with opener.open(req, timeout=20) as res:
        return res.read().decode(encoding, "replace")


def today():
    return dt.datetime.now(KST).date()


# ---------------------------------------------------------------- 사이트 조회

def parse_teams(text):
    m = re.search(r"(\d+)\s*팀", text)
    return int(m.group(1)) if m else 0


def check_date(day):
    """해당 날짜의 {골프장 이름: (잔여 팀 수, 예약 링크)}를 돌려줍니다."""
    ymd = day.strftime("%Y%m%d")
    page = fetch(
        BASE + "/reservation/ajax/mainGolfDetail",
        {"workMonth": ymd[:6], "workDate": ymd, "days": "on"},
    )
    result = {}
    for _cls, href, name, team in ITEM_RE.findall(page):
        result[html.unescape(name).strip()] = (parse_teams(html.unescape(team)), BASE + href)

    # 듄스는 별도 API로 잔여 팀 수를 줍니다.
    if "듄스" in result:
        iso = day.isoformat()
        try:
            rows = json.loads(fetch(f"{BASE}/api/dunes?minDate={iso}&maxDate={iso}"))
            teams = int(rows[0]["available_teams"]) if rows and rows[0].get("is_open") else 0
        except Exception:
            teams = 0
        result["듄스"] = (teams, result["듄스"][1])
    return result


MAESTRO = "https://www.maestrocc.co.kr"
MAESTRO_RE = re.compile(r"mainDivCalendrShow\(\d+,\s*'([\d-]+)','([^']*)',\s*'([^']*)'")


def check_maestro():
    """마에스트로CC 메인 달력(예약 가능한 3주 전체)을 한 번에 읽어
    {날짜: {골프장 이름: (잔여 팀 수, 예약 링크)}}를 돌려줍니다."""
    page = fetch(
        MAESTRO + "/GolfRes/mainpage/main_calendar_line_ajax_view.asp",
        {"golfrestype": "real", "schDate": "", "usrmemcd": "90", "toDay": "", "calnum": "1"},
        encoding="cp949",
    )
    link = MAESTRO + "/pagesite/reservation/live.asp"
    result = {}
    for iso, names, teams in MAESTRO_RE.findall(page):
        day = result.setdefault(dt.date.fromisoformat(iso), {})
        for name, n in zip(names.split("|"), teams.split("|")):
            course = "마에스트로CC " + name.replace("코스", "")
            day[course] = (int(n) if n.isdigit() else 0, link)
    return result


def target_dates(cfg):
    base = today()
    days = {dt.date.fromisoformat(d) for d in cfg.get("dates", [])}
    wanted = {WEEKDAYS.index(w) for w in cfg.get("weekdays", []) if w in WEEKDAYS}
    for i in range(1, int(cfg.get("days_ahead", 14)) + 1):
        d = base + dt.timedelta(days=i)
        if d.weekday() in wanted:
            days.add(d)
    return sorted(d for d in days if d > base)


def course_wanted(name, cfg):
    keys = cfg.get("courses", [])
    return not keys or any(k in name for k in keys)


def day_label(day):
    return f"{day:%m/%d}({WEEKDAYS[day.weekday()]})"


def scan(cfg):
    """감시 대상 날짜·골프장을 조회해서 [(날짜, 이름, 팀 수, 링크)]를 돌려줍니다."""
    fetch(BASE + "/")  # 세션 쿠키 받기
    maestro = {}
    if any(course_wanted(c, cfg) for c in MAESTRO_COURSES):
        try:
            maestro = check_maestro()
        except Exception as e:
            print(f"마에스트로CC 조회 실패: {e}")
    rows = []
    for day in target_dates(cfg):
        try:
            courses = check_date(day)
        except Exception as e:
            print(f"{day_label(day)} 조회 실패: {e}")
            courses = {}
        courses.update(maestro.get(day, {}))
        for name, (teams, link) in courses.items():
            if course_wanted(name, cfg):
                rows.append((day, name, teams, link))
        time.sleep(REQUEST_GAP)
    return rows


def find_new_openings(cfg, rows, counts):
    """이전 조회보다 자리가 늘어난 곳을 찾고, 새 counts를 돌려줍니다."""
    new_counts, found = {}, []
    for day, name, teams, link in rows:
        key = f"{day.isoformat()}|{name}"
        new_counts[key] = teams
        before = counts.get(key)
        if teams >= cfg.get("min_teams", 1) and before is not None and teams > before:
            found.append(f"⛳ {day_label(day)} {name}: {before}팀 → {teams}팀\n{link}")
    return new_counts, found


def print_rows(rows):
    for day, name, teams, _link in rows:
        print(f"{day_label(day)} {name:<20} {f'{teams}팀' if teams else '마감'}")


def format_status(rows):
    if not rows:
        return "감시 중인 날짜·골프장이 없습니다. '설정'으로 조건을 확인하세요."
    lines, last = [], None
    for day, name, teams, _link in rows:
        if day != last:
            lines.append(f"\n[{day_label(day)}]")
            last = day
        lines.append(f"  {name} {f'{teams}팀' if teams else '마감'}")
    return "원더클럽 현재 잔여 팀" + "\n".join(lines)


# ---------------------------------------------------------------- 텔레그램

def telegram_creds():
    secrets = load_json(SECRETS_PATH, {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or secrets.get("telegram_bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or secrets.get("telegram_chat_id", "")
    return token, str(chat)


def send_telegram(text):
    token, chat = telegram_creds()
    if not (token and chat):
        return
    try:
        fetch(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"},
        )
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")


def notify(message):
    print("\n" + "=" * 50 + "\n" + message + "\n" + "=" * 50, flush=True)
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass
    send_telegram(message)


def setup_telegram():
    """봇에게 보낸 메시지에서 chat_id를 찾아 secrets.json에 저장하고 테스트 메시지를 보냅니다."""
    token, _ = telegram_creds()
    if not token:
        sys.exit("secrets.json의 telegram_bot_token에 BotFather가 준 토큰을 먼저 넣어주세요.")
    try:
        me = json.loads(fetch(f"https://api.telegram.org/bot{token}/getMe"))["result"]
    except Exception as e:
        sys.exit(f"텔레그램 접속 실패(토큰을 확인하세요): {e}")
    print(f"봇 @{me['username']} 에게 텔레그램으로 아무 메시지나 보내주세요 (60초 대기)...", flush=True)
    chats = []
    for _ in range(4):  # 15초짜리 롱폴링 네 번 (fetch 타임아웃 20초 안쪽)
        res = json.loads(fetch(f"https://api.telegram.org/bot{token}/getUpdates?timeout=15"))
        chats = [u["message"]["chat"]["id"] for u in res.get("result", []) if "message" in u]
        if chats:
            break
    if not chats:
        sys.exit("봇에게 온 메시지가 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
    secrets = load_json(SECRETS_PATH, {})
    secrets.update({"telegram_bot_token": token, "telegram_chat_id": str(chats[-1])})
    save_json(SECRETS_PATH, secrets)
    print(f"chat_id {chats[-1]} 저장 완료")
    notify("원더클럽 빈자리 알림이 연결되었습니다 ⛳")


HELP = """⛳ 원더클럽 빈자리 알림 명령어

현황 — 지금 잔여 팀 수 보기
설정 — 현재 감시 조건 보기
골프장 파주 신라 — 감시할 골프장 (이름 일부만, '전체' 가능)
요일 토 일 — 감시할 요일 ('없음' 가능)
날짜 11/15 11/22 — 특정 날짜 추가 ('없음'으로 비우기)
기간 21 — 앞으로 며칠까지 볼지
기준 3 — 몇 팀 이상일 때만 알릴지
중지 / 재개 — 알림 잠시 멈추기 / 다시 켜기

※ 클라우드에서 약 10분마다 실행되므로 답장이 몇 분 늦을 수 있습니다."""


def format_config(cfg):
    dates = ", ".join(cfg.get("dates", [])) or "없음"
    return (
        "현재 감시 조건\n"
        f"골프장: {', '.join(cfg.get('courses', [])) or '전체'}\n"
        f"요일: {' '.join(cfg.get('weekdays', [])) or '없음'} (앞으로 {cfg.get('days_ahead')}일)\n"
        f"특정 날짜: {dates}\n"
        f"알림 기준: {cfg.get('min_teams', 1)}팀 이상\n"
        f"상태: {'⏸ 중지됨' if cfg.get('paused') else '▶ 감시 중'}"
    )


def parse_date(text):
    m = re.fullmatch(r"(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2})", text)
    if not m:
        raise ValueError(text)
    year = int(m.group(1)) if m.group(1) else today().year
    d = dt.date(year, int(m.group(2)), int(m.group(3)))
    if not m.group(1) and d < today():
        d = d.replace(year=year + 1)
    return d.isoformat()


def handle_command(cfg, text):
    """명령을 처리하고 (답장, 현황 요청 여부)를 돌려줍니다. cfg는 그 자리에서 바뀝니다."""
    words = text.strip().lstrip("/").split()
    if not words:
        return None, False
    cmd, args = words[0].lower(), words[1:]
    if cmd in ("도움말", "help", "start", "명령어"):
        return HELP, False
    if cmd in ("현황", "status"):
        return None, True
    if cmd in ("설정", "config"):
        return format_config(cfg), False
    if cmd in ("중지", "정지", "stop"):
        cfg["paused"] = True
        return "⏸ 알림을 멈췄습니다. '재개'를 보내면 다시 켜집니다.", False
    if cmd in ("재개", "시작", "resume"):
        cfg["paused"] = False
        return "▶ 알림을 다시 켰습니다.", False
    if cmd == "골프장":
        if not args:
            return "예) 골프장 파주 신라\n고를 수 있는 곳:\n" + "\n".join(ALL_COURSES), False
        cfg["courses"] = [] if args == ["전체"] else args
        return format_config(cfg), False
    if cmd == "요일":
        days = [] if args == ["없음"] else [c for c in "".join(args) if c in WEEKDAYS]
        if not days and args != ["없음"]:
            return "예) 요일 토 일  /  요일 금토일  /  요일 없음", False
        cfg["weekdays"] = sorted(set(days), key=WEEKDAYS.index)
        return format_config(cfg), False
    if cmd == "날짜":
        if args == ["없음"]:
            cfg["dates"] = []
            return format_config(cfg), False
        try:
            new = [parse_date(a) for a in args]
        except ValueError:
            return "날짜 형식을 모르겠어요.\n예) 날짜 11/15 11/22", False
        if not new:
            return "예) 날짜 11/15 11/22  /  날짜 없음", False
        cfg["dates"] = sorted(set(cfg.get("dates", [])) | set(new))
        return format_config(cfg), False
    if cmd in ("기간", "기준"):
        if not args or not args[0].isdigit():
            return f"예) {cmd} {'21' if cmd == '기간' else '3'}", False
        n = int(args[0])
        if cmd == "기간":
            cfg["days_ahead"] = max(1, min(n, 60))
        else:
            cfg["min_teams"] = max(1, n)
        return format_config(cfg), False
    return "모르는 명령이에요. '도움말'을 보내보세요.", False


def process_commands(cfg, state):
    """새로 온 텔레그램 메시지를 처리합니다. 현황 요청이 있었는지 돌려줍니다."""
    token, chat = telegram_creds()
    if not (token and chat):
        return False
    offset = state.get("update_offset", 0)
    try:
        res = json.loads(fetch(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0"))
    except Exception as e:
        print(f"[텔레그램 명령 확인 실패] {e}")
        return False
    want_status = False
    for upd in res.get("result", []):
        state["update_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat or "text" not in msg:
            continue  # 내 대화방이 아닌 메시지는 무시
        print(f"[명령] {msg['text']}")
        reply, status = handle_command(cfg, msg["text"])
        want_status |= status
        if reply:
            send_telegram(reply)
    return want_status


# ---------------------------------------------------------------- 실행

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state():
    state = load_json(STATE_PATH, {})
    if "counts" not in state:  # 예전 형식(평평한 dict) 변환
        state = {"counts": state}
    return state


def run_cloud(cfg):
    """한 번 실행: 텔레그램 명령 처리 → 조회 → 알림 → 저장."""
    state = load_state()
    want_status = process_commands(cfg, state)
    save_json(CONFIG_PATH, cfg)
    if cfg.get("paused") and not want_status:
        print("중지 상태라 조회를 건너뜁니다.")
    else:
        rows = scan(cfg)
        print_rows(rows)
        new_counts, found = find_new_openings(cfg, rows, state.get("counts", {}))
        state["counts"] = new_counts
        if want_status:
            send_telegram(format_status(rows))
        if found and not cfg.get("paused"):
            notify("원더클럽 빈자리 알림\n\n" + "\n\n".join(found))
    save_json(STATE_PATH, state)


def main():
    sys.stdout.reconfigure(errors="replace")  # 콘솔이 이모지를 못 찍어도 죽지 않게
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit(f"설정 파일을 읽을 수 없습니다: {CONFIG_PATH}")
    if "--telegram-setup" in sys.argv:
        setup_telegram()
        return
    if "--once" in sys.argv:
        print_rows(scan(cfg))
        return
    if "--cloud" in sys.argv:
        run_cloud(cfg)
        return
    interval = max(MIN_INTERVAL, int(cfg.get("interval_minutes", 10)))
    print(f"감시 시작: {interval}분 간격 (Ctrl+C로 종료)")
    while True:
        print(f"\n--- {dt.datetime.now(KST):%Y-%m-%d %H:%M:%S} ---")
        try:
            run_cloud(cfg)
        except Exception as e:
            print(f"조회 오류: {e}")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
