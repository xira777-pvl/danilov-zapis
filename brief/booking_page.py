"""Public booking page: a read-only availability calendar over the Bitrix CRM form.

Busy time comes from CRM deals («Время записи» + «Длительность, часов»); only start and
end times leave Bitrix — no names, sums or deal ids. A guest sees free days and windows,
then leaves a request in the embedded Bitrix form; the manager confirms by phone.

Usage: python -m brief.booking_page <out.html>   (needs BITRIX_WEBHOOK_URL)
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from urllib.error import HTTPError

from brief.bitrix import make_call, visits
from brief.dates import OMSK, omsk_today

DAYS_AHEAD = 62
OPEN, CLOSE = "10:00", "22:00"  # bath hours from the booking resource (Mondays open at 16:00)
FORM_ID, FORM_SEC = 29, "49w4ka"  # CRM form «Заявка на запись (страница с календарём)»
FORM_LOADER = "https://cdn-ru.bitrix24.ru/b38758868/crm/form/loader_29.js"
OFERTA = "https://danilovhouse.ru/p/oferta/"


def busy_days(vs: list[dict]) -> dict[str, list[list[str]]]:
    """{"YYYY-MM-DD": [["HH:MM", "HH:MM"], ...]} sorted and merged.

    A visit without a time or a duration is treated as busy until closing: an unknown
    end must not show a guest a window that is in fact taken.
    """
    raw: dict[str, list[tuple[str, str]]] = {}
    for v in vs:
        start, end = v["start"], v["end"]
        day = start.date().isoformat()
        if not v["timed"]:
            raw.setdefault(day, []).append((OPEN, CLOSE))
            continue
        if end is None:
            raw.setdefault(day, []).append((f"{start:%H:%M}", CLOSE))
            continue
        # A visit past midnight occupies each day it touches until its end.
        cur = start
        while cur.date() < end.date():
            raw.setdefault(cur.date().isoformat(), []).append((f"{cur:%H:%M}", "24:00"))
            cur = datetime.combine(cur.date() + timedelta(days=1), datetime.min.time(), tzinfo=OMSK)
        if end > cur:
            raw.setdefault(cur.date().isoformat(), []).append((f"{cur:%H:%M}", f"{end:%H:%M}"))
    out = {}
    for day, spans in raw.items():
        merged: list[list[str]] = []
        for f, t in sorted(spans):
            if merged and f <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], t)
            else:
                merged.append([f, t])
        out[day] = merged
    return dict(sorted(out.items()))


CSS = """
:root{--bg:#f4f1ea;--card:#fff;--ink:#1f2a24;--muted:#6b746e;--line:#e2ddd2;--green:#133a2e;
--free:#e3f1e6;--free-ink:#1d6b3a;--part:#fbefd5;--part-ink:#8a5a00;--busy:#f3dcdc;--busy-ink:#9b2c2c;--past:#f0eee9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:16px}
h1{font-size:24px;margin:0 0 4px;color:var(--green)}
.lead{color:var(--muted);margin:0 0 16px}
.card{background:var(--card);border-radius:14px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
h2{font-size:17px;margin:0 0 12px}
.cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.cal-head button{border:1px solid var(--line);background:#fff;border-radius:8px;width:36px;height:32px;font-size:18px;cursor:pointer;color:var(--ink)}
.cal-head button:disabled{opacity:.35;cursor:default}
.month{font-weight:600;text-transform:capitalize}
.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.dow{text-align:center;font-size:12px;color:var(--muted);padding:2px 0}
.day{aspect-ratio:1;border-radius:8px;display:flex;flex-direction:column;align-items:center;justify-content:center;
font:inherit;font-size:15px;border:2px solid transparent;cursor:pointer;padding:0}
.day small{font-size:9px;line-height:1;margin-top:2px}
.day.free{background:var(--free);color:var(--free-ink)}
.day.part{background:var(--part);color:var(--part-ink)}
.day.busy{background:var(--busy);color:var(--busy-ink)}
.day.past{background:var(--past);color:#b9b4aa;cursor:default}
.day.empty{background:transparent;cursor:default}
.day.sel{border-color:var(--green)}
.day:focus-visible{outline:2px solid var(--green);outline-offset:1px}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:var(--muted);margin-top:10px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px}
.info{margin-top:10px;padding:10px 12px;border-radius:10px;background:#f7f5f0;font-size:14px;min-height:42px}
.note{font-size:13px;color:var(--muted);margin:8px 0 0}
.terms{font-size:13px;color:var(--muted);background:#f7f5f0;border-radius:10px;padding:10px 12px;margin-bottom:12px}
.terms ul{margin:6px 0 0;padding-left:18px}
a{color:var(--green)}
"""

BODY = """
<div class="wrap">
  <h1>Запись в баню</h1>
  <p class="lead">«В гостях у Даниловых» · частная баня с чаном и купелью</p>

  <div class="card">
    <h2>Свободные даты</h2>
    <div class="cal-head">
      <button id="prev" aria-label="Предыдущий месяц">‹</button>
      <div class="month" id="month"></div>
      <button id="next" aria-label="Следующий месяц">›</button>
    </div>
    <div class="grid" id="grid"></div>
    <div class="legend">
      <span><i style="background:var(--free)"></i>свободно</span>
      <span><i style="background:var(--part)"></i>есть свободное время</span>
      <span><i style="background:var(--busy)"></i>занято</span>
    </div>
    <div class="info" id="info" aria-live="polite">Нажмите на день, чтобы увидеть свободное время.</div>
    <p class="note">Календарь для ориентира, обновлён __UPDATED__ (Омск). Записаться в нём нельзя:
      оставьте заявку ниже, менеджер свяжется и подтвердит дату.</p>
  </div>

  <div class="card" id="form">
    <h2>Заявка</h2>
    <div class="terms">
      Заявка не бронирует дату: менеджер свяжется, подтвердит её и пришлёт ссылку на оплату.
      <ul>
        <li>Дата держится за вами 48 часов после получения ссылки на оплату.</li>
        <li>Бронь подтверждается оплатой, это принятие <a href="__OFERTA__" target="_blank" rel="noopener">договора-оферты</a>.</li>
        <li>Отмена не позднее чем за 72 часа до визита — деньги возвращаются полностью.</li>
      </ul>
    </div>
    <script data-b24-form="inline/__FORM_ID__/__FORM_SEC__" data-skip-moving="true">
      (function(w,d,u){var s=d.createElement('script');s.async=true;s.src=u+'?'+(Date.now()/180000|0);
      var h=d.getElementsByTagName('script')[0];h.parentNode.insertBefore(s,h);})(window,document,'__FORM_LOADER__');
    </script>
  </div>
</div>
"""

SCRIPT = """
const TODAY = __TODAY__, BUSY = __BUSY__, DAYS_AHEAD = __DAYS_AHEAD__;
const MIN_WINDOW = 120;  // minutes; a shorter gap is not shown as free time
const MONTHS = ["январь","февраль","март","апрель","май","июнь","июль","август","сентябрь","октябрь","ноябрь","декабрь"];
const MONTHS_OF = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
const DAYS = ["воскресенье","понедельник","вторник","среда","четверг","пятница","суббота"];
const pad = n => String(n).padStart(2, "0");
const iso = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const mins = s => { const [h, m] = s.split(":").map(Number); return h*60 + m; };
const hhmm = m => `${pad(Math.floor(m/60))}:${pad(m%60)}`;
const hours = d => d.getDay() === 1 ? [960, 1320] : [600, 1320];

function windows(d) {
  const [lo, hi] = hours(d), out = [];
  let cur = lo;
  for (const [f, t] of (BUSY[iso(d)] || []).map(([f, t]) => [mins(f), mins(t)])) {
    if (Math.min(f, hi) - cur >= MIN_WINDOW) out.push([cur, Math.min(f, hi)]);
    cur = Math.max(cur, t);
  }
  if (hi - cur >= MIN_WINDOW) out.push([cur, hi]);
  return out;
}
const start = new Date(TODAY + "T00:00");
const end = new Date(start); end.setDate(end.getDate() + DAYS_AHEAD);
function state(d) {
  if (d < start || d >= end) return "past";
  if (!BUSY[iso(d)]) return "free";
  return windows(d).length ? "part" : "busy";
}
const first = new Date(start.getFullYear(), start.getMonth(), 1);
const lastMonth = new Date(end.getFullYear(), end.getMonth(), 1);
let view = first, selected = null;

function render() {
  document.getElementById("month").textContent = `${MONTHS[view.getMonth()]} ${view.getFullYear()}`;
  document.getElementById("prev").disabled = view <= first;
  document.getElementById("next").disabled = view >= lastMonth;
  const g = document.getElementById("grid");
  g.innerHTML = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"].map(x => `<div class="dow">${x}</div>`).join("");
  for (let i = 0; i < (view.getDay() + 6) % 7; i++) g.insertAdjacentHTML("beforeend", '<div class="day empty"></div>');
  const n = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
  for (let k = 1; k <= n; k++) {
    const d = new Date(view.getFullYear(), view.getMonth(), k), s = state(d);
    const el = document.createElement(s === "past" ? "div" : "button");
    el.className = `day ${s}` + (selected === iso(d) ? " sel" : "");
    el.innerHTML = `${k}` + (d.getDay() === 2 && s !== "past" ? "<small>вторник</small>" : "");
    if (s !== "past") { el.type = "button"; el.onclick = () => pick(d); }
    g.appendChild(el);
  }
}
function pick(d) {
  selected = iso(d);
  const s = state(d), title = `${d.getDate()} ${MONTHS_OF[d.getMonth()]}, ${DAYS[d.getDay()]}`;
  let text = s === "busy" ? `<b>${title}</b>: день занят, выберите другую дату.`
    : `<b>${title}</b>: свободно ${windows(d).map(([f, t]) => `${hhmm(f)}–${hhmm(t)}`).join(", ")}. Укажите эту дату в заявке ниже.`;
  if (d.getDay() === 2) text += " По вторникам проходят программы «Банного вторника».";
  document.getElementById("info").innerHTML = text;
  render();
}
document.getElementById("prev").onclick = () => { view = new Date(view.getFullYear(), view.getMonth() - 1, 1); render(); };
document.getElementById("next").onclick = () => { view = new Date(view.getFullYear(), view.getMonth() + 1, 1); render(); };
render();
"""


def render(busy: dict, today: date, updated: datetime) -> str:
    body = (BODY.replace("__UPDATED__", f"{updated:%d.%m %H:%M}").replace("__OFERTA__", OFERTA)
            .replace("__FORM_ID__", str(FORM_ID)).replace("__FORM_SEC__", FORM_SEC)
            .replace("__FORM_LOADER__", FORM_LOADER))
    script = (SCRIPT.replace("__TODAY__", json.dumps(today.isoformat()))
              .replace("__BUSY__", json.dumps(busy)).replace("__DAYS_AHEAD__", str(DAYS_AHEAD)))
    return ("<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Запись в баню — В гостях у Даниловых</title>\n"
            "<meta name=\"robots\" content=\"noindex\">\n"
            f"<style>{CSS}</style>\n</head>\n<body>{body}<script>{script}</script>\n</body>\n</html>\n")


def main() -> int:
    out = sys.argv[1]
    call = make_call(os.environ["BITRIX_WEBHOOK_URL"])
    today = omsk_today()
    # From yesterday: a visit that started then may still occupy this morning.
    try:
        vs = visits(call, today - timedelta(days=1), today + timedelta(days=DAYS_AHEAD))
    except HTTPError as exc:
        # Bitrix's JSON error (e.g. insufficient_scope) never contains the webhook URL.
        print(f"Bitrix HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        return 1
    busy = busy_days(vs)
    busy = {d: spans for d, spans in busy.items() if d >= today.isoformat()}
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(busy, today, datetime.now(OMSK)))
    print(json.dumps({"days_busy": len(busy)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
