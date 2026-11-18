"""Public booking page: a read-only availability calendar over the Bitrix CRM form.

Busy time comes from CRM deals («Время записи» + «Длительность, часов»); only start and
end times leave Bitrix — no names, sums or deal ids. A guest sees two fixed daily slots,
chooses a compatible program, then leaves a request in the embedded Bitrix form;
the manager confirms by phone.

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
OPEN, CLOSE = "10:00", "22:00"  # conservative bounds for visits with unknown times
FORM_ID, FORM_SEC = 29, "49w4ka"  # CRM form «Заявка на запись (страница с календарём)»
FORM_LOADER = "https://cdn-ru.bitrix24.ru/b38758868/crm/form/loader_29.js"
OFERTA = "https://danilovhouse.ru/p/oferta/"
# Public catalog order; IDs refer to the existing Bitrix «Желаемая программа» options.
PROGRAMS = (
    ("na-dvoih-2-5-chasa", "147", "На двоих — 2,5 часа", 150),
    ("na-dvoih-3-chasa", "149", "На двоих — 3 часа", 180),
    ("na-dvoih-4-chasa", "151", "На двоих — 4 часа", 240),
    ("na-dvoih-5-chasov", "153", "На двоих — 5 часов", 300),
    ("legkiy-par", "141", "Лёгкий пар (4–5 часов, до 12 гостей)", 300),
    ("stihiya", "143", "Стихия (4–5 часов, до 8 гостей)", 300),
    ("deviche-teplo", "119", "Девичье тепло (5–6 часов, 2–12 гостей)", 360),
    ("zhenskaya-stihiya", "121", "Женская стихия (5–6 часов, до 8 гостей)", 360),
    ("sila-ognya", "123", "Сила огня (5–6 часов, 2–12 гостей)", 360),
    ("muzhskaya-zakalka", "125", "Мужская закалка (5–6 часов, 2–8 гостей)", 360),
)


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
        # Public spans have minute precision: round outward so no occupied second
        # (or the cleanup following it) can accidentally become available.
        if end.second or end.microsecond:
            end = (end + timedelta(minutes=1)).replace(second=0, microsecond=0)
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


CLEANUP = 60  # minutes after every visit before the next guests can arrive
SLOTS = (("morning", "Утренний", "10:00", "13:00"),
         ("daytime", "Дневной", "15:00", "21:00"))


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def day_slots(spans: list[list[str]]) -> list[dict]:
    """Fixed slots, allowing cleanup after both existing and requested visits.

    Half-open intervals permit the next visit to start exactly when cleanup ends.
    """
    busy = [(_minutes(f), _minutes(t) + CLEANUP) for f, t in spans]
    return [{"id": slot_id, "label": label, "start": start, "end": end,
             "available": not any(_minutes(start) < t and f < _minutes(end) + CLEANUP
                                  for f, t in busy)}
            for slot_id, label, start, end in SLOTS]


def availability(busy: dict[str, list[list[str]]], today: date) -> dict[str, dict]:
    """Per day of the public horizon: {"slots": [{id, label, start, end, available}]}.

    Only anonymized slot availability leaves this module; the client also rejects
    slots whose start time has passed in Asia/Omsk since this static page was built.
    """
    out = {}
    for i in range(DAYS_AHEAD):
        day = today + timedelta(days=i)
        spans = busy.get(day.isoformat(), [])
        out[day.isoformat()] = {"slots": day_slots(spans)}
    return out


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
.program{margin:0 0 16px}.program label{display:block;font-weight:600;margin-bottom:5px}
.program select{width:100%;border:1px solid var(--line);border-radius:8px;background:#fff;
font:inherit;color:var(--ink);padding:9px 8px}
.program select:focus-visible{outline:2px solid var(--green);outline-offset:2px}
#form-status:empty{display:none}
#booking-crm{border:0;margin:0;padding:0;min-inline-size:0}
#booking-crm [data-booking-synced]{display:none!important}
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
.slots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:10px}
.slot{font:inherit;text-align:left;border:2px solid var(--line);border-radius:12px;padding:12px;background:var(--free);color:var(--ink);cursor:pointer}
.slot span,.slot small{display:block}
.slot-name{font-weight:600}.slot-time{font-size:19px;margin:4px 0}.slot small{font-size:13px;color:var(--free-ink)}
.slot:disabled{background:var(--past);color:var(--muted);cursor:default}.slot:disabled small{color:var(--muted)}
.slot.sel{border-color:var(--green)}.slot:focus-visible{outline:2px solid var(--green);outline-offset:2px}
.selection{padding:14px;border:2px solid var(--green);border-radius:12px;background:var(--free);margin-bottom:14px}
.selection strong{display:block;font-size:20px;margin:4px 0}.selection p{font-size:14px;margin:6px 0 0}
.note{font-size:13px;color:var(--muted);margin:8px 0 0}
.terms{font-size:13px;color:var(--muted);background:#f7f5f0;border-radius:10px;padding:10px 12px;margin-bottom:12px}
.terms ul{margin:6px 0 0;padding-left:18px}
a{color:var(--green)}
"""

BODY = """
<div class="wrap">
  <h1>Форма бронирования</h1>
  <p class="lead">«В гостях у Даниловых»</p>

  <div class="card">
    <div class="program">
      <label for="program">Программа</label>
      <select id="program" aria-describedby="program-help">
        <option value="">Выберите программу</option>
      </select>
      <p class="note" id="program-help" aria-live="polite"></p>
    </div>
    <h2>Свободные даты</h2>
    <div class="cal-head">
      <button id="prev" aria-label="Предыдущий месяц">‹</button>
      <div class="month" id="month"></div>
      <button id="next" aria-label="Следующий месяц">›</button>
    </div>
    <div class="grid" id="grid"></div>
    <div class="legend">
      <span><i style="background:var(--free)"></i>2 доступных слота</span>
      <span><i style="background:var(--part)"></i>1 доступный слот</span>
      <span><i style="background:var(--busy)"></i>нет доступных слотов</span>
    </div>
    <div class="info" id="info" aria-live="polite">Выберите дату, затем утренний или дневной слот.</div>
    <div class="slots" id="slots" aria-label="Слоты на выбранную дату"></div>
    <p class="note">Время указано по Омску.</p>
    <p class="note">Календарь для ориентира, обновлён __UPDATED__ (Омск). Записаться в нём нельзя:
      оставьте заявку ниже, менеджер свяжется и подтвердит дату.</p>
  </div>

  <div class="card" id="form">
    <h2>Заявка</h2>
    <div class="selection" id="selection" aria-live="polite">Выберите дату и слот в календаре выше.</div>
    <div class="terms">
      Заявка не бронирует дату: менеджер свяжется, подтвердит её и пришлёт ссылку на оплату.
      <ul>
        <li>Дата держится за вами 24 часа после получения ссылки на оплату.</li>
        <li>Бронь подтверждается оплатой, это принятие <a href="__OFERTA__" target="_blank" rel="noopener">договора-оферты</a>.</li>
        <li>Отмена не позднее чем за 72 часа до визита — деньги возвращаются полностью.</li>
      </ul>
    </div>
    <p class="note" id="form-status" role="status"></p>
    <fieldset id="booking-crm" aria-label="Контактные данные" disabled>
      <script data-b24-form="inline/__FORM_ID__/__FORM_SEC__" data-skip-moving="true"></script>
    </fieldset>
  </div>
</div>
"""

SCRIPT = r"""
const TODAY = __TODAY__, DAYS = __DAYS__;
const PROGRAMS = new Map(__PROGRAMS__.map(([slug, id, label, duration]) => [slug, {id, label, duration}]));
const CRM_FIELDS = {
  date: "DEAL_UF_CRM_WISH_DATE",
  program: "DEAL_UF_CRM_WISH_PROGRAM",
  comment: "DEAL_COMMENTS",
};
const programSelect = document.getElementById("program");
const crm = document.getElementById("booking-crm");
const formStatus = document.getElementById("form-status");
for (const [slug, program] of PROGRAMS) {
  programSelect.add(new Option(program.label, slug));
}
const requestedProgram = new URLSearchParams(location.search).get("program");
programSelect.value = PROGRAMS.has(requestedProgram) ? requestedProgram : "";
const currentProgram = () => PROGRAMS.get(programSelect.value);
const MONTHS = ["январь","февраль","март","апрель","май","июнь","июль","август","сентябрь","октябрь","ноябрь","декабрь"];
const MONTHS_OF = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
const WEEKDAYS = ["воскресенье","понедельник","вторник","среда","четверг","пятница","суббота"];
// Calendar dates use UTC as date-only values; slot instants use Asia/Omsk (UTC+06).
const iso = d => d.toISOString().slice(0, 10);
const start = new Date(TODAY + "T00:00:00Z");
const end = new Date(start); end.setUTCDate(end.getUTCDate() + Object.keys(DAYS).length - 1);
const slotStart = (day, slot) => Date.parse(`${day}T${slot.start}:00+06:00`);
const minutes = time => Number(time.slice(0, 2)) * 60 + Number(time.slice(3));
const fits = (program, slot) => !program || program.duration <= minutes(slot.end) - minutes(slot.start);
function slotStatus(day, slot, now) {
  if (!slot.available) return "Занят";
  if (slotStart(day, slot) <= now) return "Время прошло";
  if (!fits(currentProgram(), slot)) return "Для программ до 3 часов";
  return "";
}
const selectable = (day, slot, now) => !slotStatus(day, slot, now);
function state(d, now) {
  const day = iso(d), x = DAYS[day];
  if (!x || Date.parse(`${day}T00:00:00+06:00`) + 86400000 <= now) return "past";
  const count = x.slots.filter(slot => selectable(day, slot, now)).length;
  return count === 2 ? "free" : count === 1 ? "part" : "busy";
}
const first = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), 1));
const lastMonth = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), 1));
let view = first, selected = null, selectedSlot = null, nativeForm = null;
const activeSlot = () => DAYS[selected]?.slots.find(slot => slot.id === selectedSlot);

function crmSelection() {
  return {
    [CRM_FIELDS.date]: selected ? selected.split("-").reverse().join(".") : "",
    [CRM_FIELDS.program]: currentProgram()?.id || "",
  };
}

function syncForm(now) {
  if (!nativeForm) return;
  const expected = crmSelection();
  const current = nativeForm.values();
  if (Object.entries(expected).some(([name, value]) => (current[name]?.[0] || "") !== value)) {
    nativeForm.setValues(expected);
  }
  const actual = nativeForm.values();
  const fieldsMatch = Object.entries(expected).every(([name, value]) => (actual[name]?.[0] || "") === value);
  const chosen = activeSlot();
  crm.disabled = !fieldsMatch || !currentProgram() || !chosen || !selectable(selected, chosen, now);
  formStatus.textContent = fieldsMatch ? "" : "Не удалось передать выбор в заявку. Обновите страницу и попробуйте снова.";
}

function connectForm(event) {
  const form = event.detail.object;
  if (Number(form.identification.id) !== __FORM_ID__) return;
  const fields = new Map(form.getFields().map(field => [field.name, field]));
  if (Object.values(CRM_FIELDS).some(name => !fields.get(name)?.visible)) {
    formStatus.textContent = "Форма заявки сейчас недоступна. Обновите страницу и попробуйте снова.";
    return;
  }
  nativeForm = form;
  // The fields stay visible to Bitrix validation and serialization. The calendar
  // supplies their value, so guests can change date and program in one place.
  queueMicrotask(() => {
    const syncedLabels = [fields.get(CRM_FIELDS.date).label, fields.get(CRM_FIELDS.program).label];
    for (const label of crm.querySelectorAll(".b24-form-control-label")) {
      if (syncedLabels.includes(label.textContent.trim().replace(/\s*\*$/, ""))) {
        label.closest(".b24-form-field").dataset.bookingSynced = "";
      }
    }
  });
  let restoreComment = null;
  form.subscribe("submit", data => {
    refresh();
    if (crm.disabled) {
      data.promise = Promise.reject(new Error("Выберите программу, дату и доступный слот."));
      return;
    }
    const slot = activeSlot();
    const note = `Желаемый слот: ${slot.start}–${slot.end} (Омск)`;
    const comment = form.values()[CRM_FIELDS.comment]?.[0] || "";
    const submitted = comment ? `${note}\n\n${comment}` : note;
    form.setValues({[CRM_FIELDS.comment]: submitted});
    restoreComment = () => {
      form.setValues({[CRM_FIELDS.comment]: comment});
      restoreComment = null;
    };
  });
  // The native widget captures values before this event, then sends its own payload.
  form.subscribe("submit:post:before", () => restoreComment?.());
  form.subscribe("send:error", () => restoreComment?.());
  form.subscribe("send:success", () => restoreComment?.());
  refresh();
}
window.addEventListener("b24:form:init", connectForm);

function render(now) {
  document.getElementById("month").textContent = `${MONTHS[view.getUTCMonth()]} ${view.getUTCFullYear()}`;
  document.getElementById("prev").disabled = view <= first;
  document.getElementById("next").disabled = view >= lastMonth;
  const g = document.getElementById("grid");
  g.innerHTML = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"].map(x => `<div class="dow">${x}</div>`).join("");
  for (let i = 0; i < (view.getUTCDay() + 6) % 7; i++) g.insertAdjacentHTML("beforeend", '<div class="day empty"></div>');
  const n = new Date(Date.UTC(view.getUTCFullYear(), view.getUTCMonth() + 1, 0)).getUTCDate();
  for (let k = 1; k <= n; k++) {
    const d = new Date(Date.UTC(view.getUTCFullYear(), view.getUTCMonth(), k)), s = state(d, now);
    const el = document.createElement(s === "past" ? "div" : "button");
    el.className = `day ${s}` + (selected === iso(d) ? " sel" : "");
    el.textContent = k;
    if (s !== "past") {
      el.type = "button"; el.dataset.date = iso(d);
      el.setAttribute("aria-pressed", String(selected === iso(d)));
      el.onclick = () => pick(d);
    }
    g.appendChild(el);
  }
}
function pick(d) {
  if (state(d, Date.now()) === "past") return refresh();
  selected = iso(d); selectedSlot = null;
  refresh();
}
function refresh() {
  const now = Date.now();
  if (selected && state(new Date(selected + "T00:00:00Z"), now) === "past") {
    selected = null;
    selectedSlot = null;
  }
  const slots = DAYS[selected]?.slots || [];
  const previousSlot = activeSlot();
  if (previousSlot && !selectable(selected, previousSlot, now)) selectedSlot = null;
  const chosen = activeSlot();
  const program = currentProgram();
  for (const option of programSelect.options) {
    option.disabled = Boolean(chosen && !fits(PROGRAMS.get(option.value), chosen));
  }
  document.getElementById("program-help").textContent = chosen?.id === "morning"
    ? "Утром доступны программы до 3 часов. Для более долгой программы выберите дневной слот."
    : "Для программ на 4–6 часов подходит дневной слот 15:00–21:00.";
  render(now);
  syncForm(now);
  const panel = document.getElementById("slots"), summary = document.getElementById("selection");
  const info = document.getElementById("info");
  const programLine = program ? `<strong>${program.label}</strong>` : "";
  panel.innerHTML = "";
  if (!selected) {
    info.textContent = "Выберите дату, затем утренний или дневной слот.";
    summary.innerHTML = programLine + "<p>Выберите дату и слот в календаре выше.</p>";
    return;
  }
  const d = new Date(selected + "T00:00:00Z");
  const title = `${d.getUTCDate()} ${MONTHS_OF[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${WEEKDAYS[d.getUTCDay()]}`;
  const hasFree = slots.some(slot => selectable(selected, slot, now));
  info.innerHTML = `<b>${title}</b>. ` +
    (hasFree ? "Выберите свободный слот." : "Подходящих свободных слотов нет. Выберите другую дату.");
  for (const slot of slots) {
    const el = document.createElement("button"), chosen = slot.id === selectedSlot;
    el.type = "button"; el.dataset.slotId = slot.id;
    el.className = "slot" + (chosen ? " sel" : "");
    const unavailable = slotStatus(selected, slot, now);
    el.disabled = Boolean(unavailable);
    el.setAttribute("aria-pressed", String(chosen));
    const status = unavailable || (chosen ? "Выбран" : "Свободен");
    el.innerHTML = `<span class="slot-name">${slot.label}</span><span class="slot-time">${slot.start}–${slot.end}</span><small>${status}</small>`;
    el.onclick = () => {
      if (selectable(selected, slot, Date.now())) selectedSlot = slot.id;
      refresh();
    };
    panel.appendChild(el);
  }
  const slotLine = chosen ? `<strong>${chosen.label} ${chosen.start}–${chosen.end} (Омск)</strong>` : "";
  const nextStep = chosen && program ? "Оставьте контакты ниже. Менеджер подтвердит запись."
    : chosen ? "Выберите программу над календарём."
    : hasFree ? "Выберите доступный слот в календаре выше." : "Выберите другую дату в календаре.";
  summary.innerHTML = `Ваш выбор<strong>${title}</strong>${slotLine}${programLine}<p>${nextStep}</p>`;
}
programSelect.addEventListener("change", refresh);
document.getElementById("prev").onclick = () => { view = new Date(Date.UTC(view.getUTCFullYear(), view.getUTCMonth() - 1, 1)); refresh(); };
document.getElementById("next").onclick = () => { view = new Date(Date.UTC(view.getUTCFullYear(), view.getUTCMonth() + 1, 1)); refresh(); };
document.addEventListener("visibilitychange", refresh);
setInterval(refresh, 30000);
refresh();

const loader = document.createElement("script");
loader.async = true;
loader.src = "__FORM_LOADER__?" + Math.floor(Date.now() / 180000);
loader.onerror = () => { formStatus.textContent = "Форма заявки сейчас недоступна. Обновите страницу и попробуйте снова."; };
document.head.appendChild(loader);
"""


def render(busy: dict, today: date, updated: datetime) -> str:
    body = (BODY.replace("__UPDATED__", f"{updated:%d.%m %H:%M}").replace("__OFERTA__", OFERTA)
            .replace("__FORM_ID__", str(FORM_ID)).replace("__FORM_SEC__", FORM_SEC))
    script = (SCRIPT.replace("__TODAY__", json.dumps(today.isoformat()))
              .replace("__DAYS__", json.dumps(availability(busy, today)))
              .replace("__PROGRAMS__", json.dumps(PROGRAMS, ensure_ascii=False))
              .replace("__FORM_ID__", str(FORM_ID)).replace("__FORM_LOADER__", FORM_LOADER))
    return ("<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Форма бронирования — В гостях у Даниловых</title>\n"
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
