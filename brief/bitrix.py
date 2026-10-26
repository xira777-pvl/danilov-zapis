"""Read-only Bitrix24 REST collector for the morning brief."""
import json
import os
import re
import ssl
import time
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import certifi

from brief.dates import OMSK, omsk_day_range, omsk_today

Call = Callable[[str, dict], dict]
DEFAULT_FUNNEL = {"ID": "0", "NAME": "Клиенты"}
# The macOS file bundle (/etc/ssl/cert.pem) lacks newer roots such as
# GlobalSign Root R46, which signs *.bitrix24.ru; certifi keeps them current.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


# Pauses before the 2nd and 3rd attempt. A VPN or proxy on this Mac sometimes breaks
# the TLS handshake for a short while; without a retry one such blip fails the whole run.
RETRY_PAUSES = (15, 45)


def make_call(base_url: str) -> Call:
    def call(method: str, params: dict) -> dict:
        url = f"{base_url}{method}.json?{urlencode(params, doseq=True)}"
        for pause in (*RETRY_PAUSES, None):
            try:
                with urlopen(url, timeout=20, context=SSL_CONTEXT) as resp:
                    return json.loads(resp.read())
            except HTTPError:
                raise  # Bitrix answered; asking again would not change the answer
            except OSError:  # no connection, TLS failure, timeout
                if pause is None:
                    raise
                time.sleep(pause)
    return call


def fetch_all(call: Call, method: str, params: dict) -> list[dict]:
    rows, start = [], 0
    while True:
        page = call(method, {**params, "start": start})
        if "error" in page:
            # Bitrix error text never contains the webhook URL.
            raise RuntimeError(f"{page['error']}: {page.get('error_description', '')}")
        rows.extend(page.get("result", []))
        if "next" not in page:
            return rows
        if page["next"] <= start:
            raise RuntimeError(f"pagination did not advance at {start}")
        start = page["next"]


def _safe_error(exc: Exception) -> str:
    # Strip anything that looks like a webhook URL so the secret never leaks.
    return re.sub(r"https?://\S+", "<url>", str(exc))[:200]


def _no_data(source: str, exc: Exception) -> dict:
    return {"status": "no_data", "source": source, "error": _safe_error(exc)}


def _deals(call: Call, today: date) -> dict:
    start, end = omsk_day_range(today - timedelta(days=1))
    funnels = [DEFAULT_FUNNEL] + fetch_all(call, "crm.dealcategory.list", {})
    deals = fetch_all(call, "crm.deal.list", {
        "filter[>=DATE_CREATE]": start, "filter[<DATE_CREATE]": end,
        "select[]": ["ID", "CATEGORY_ID", "STAGE_ID"]})
    out = []
    for f in funnels:
        # Stage outcome lives in crm.status.list EXTRA.SEMANTICS:
        # process / success / failure / apology.
        entity = "DEAL_STAGE" if str(f["ID"]) == "0" else f"DEAL_STAGE_{f['ID']}"
        sem = {s["STATUS_ID"]: (s.get("EXTRA") or {}).get("SEMANTICS") for s in
               fetch_all(call, "crm.status.list", {"filter[ENTITY_ID]": entity})}
        mine = [d for d in deals if str(d.get("CATEGORY_ID", "0")) == str(f["ID"])]
        won = sum(1 for d in mine if sem.get(d["STAGE_ID"]) == "success")
        lost = sum(1 for d in mine if sem.get(d["STAGE_ID"]) in ("failure", "apology"))
        out.append({"name": f["NAME"], "new": len(mine), "in_progress": len(mine) - won - lost,
                    "won": won, "lost": lost})
    return {"status": "ok", "source": "Битрикс", "funnels": out}


BARTER = "Бартер"  # payment method of a visit given for advertising: no money moves
F_VISIT = "UF_CRM_1789564039392"  # «Время записи»: visit start; midnight Omsk means the time is not set
F_HOURS = "UF_CRM_VISIT_HOURS"    # «Длительность, часов»
F_PREPAID, F_SURCHARGE = "UF_CRM_1790612405079", "UF_CRM_SURCHARGE"
F_PREPAY_METHOD, F_SURCHARGE_METHOD = "UF_CRM_PREPAY_METHOD", "UF_CRM_SURCHARGE_METHOD"
VISIT_SELECT = ["ID", "TITLE", "CONTACT_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "OPPORTUNITY",
                F_VISIT, F_HOURS, F_PREPAID, F_SURCHARGE, F_PREPAY_METHOD, F_SURCHARGE_METHOD]
NO_TIME = "время не указано"


def _hours(value) -> float | None:
    try:
        hours = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return hours if 0 < hours <= 24 else None


def visit_time(start: datetime, end: datetime | None, timed: bool) -> str:
    if not timed:
        return NO_TIME
    return f"{start:%H:%M}–{end:%H:%M}" if end else f"с {start:%H:%M}"


def visits(call: Call, first: date, last: date) -> list[dict]:
    """Visits on Omsk days [first, last): deals of any funnel that are not lost, by «Время записи».

    The deal is the only place a visit is entered; Bitrix online booking is not read.
    """
    deals = fetch_all(call, "crm.deal.list", {
        f"filter[>={F_VISIT}]": omsk_day_range(first)[0], f"filter[<{F_VISIT}]": omsk_day_range(last)[0],
        "filter[!STAGE_SEMANTIC_ID]": "F", "select[]": VISIT_SELECT})
    out = []
    for d in deals:
        if not d.get(F_VISIT) or d.get("STAGE_SEMANTIC_ID") == "F":
            continue
        start = datetime.fromisoformat(d[F_VISIT]).astimezone(OMSK)
        if not first <= start.date() < last:
            continue
        timed = (start.hour, start.minute) != (0, 0)
        hours = _hours(d.get(F_HOURS)) if timed else None
        end = start + timedelta(hours=hours) if hours else None
        out.append({"deal": d, "start": start, "end": end, "timed": timed,
                    "time": visit_time(start, end, timed)})
    return sorted(out, key=lambda v: (v["start"], int(v["deal"]["ID"])))


def barter_ids(call: Call) -> dict[str, set]:
    """Ids of the «Бартер» option of each payment-method field."""
    fields = call("crm.deal.fields", {})
    if "error" in fields:
        raise RuntimeError(fields["error"])
    fields = fields.get("result") or {}
    return {f: {str(i["ID"]) for i in (fields.get(f) or {}).get("items", []) if i.get("VALUE") == BARTER}
            for f in (F_PREPAY_METHOD, F_SURCHARGE_METHOD)}


def due_and_barter(amount, paid: list[tuple]) -> tuple[float | None, bool]:
    """Money still to pay and whether the visit is (partly) barter.

    `paid` is [(amount, is_barter)] for the prepayment and the surcharge. «Бартер» with no
    amount next to it means the whole deal sum is barter, so nothing is left to pay.
    """
    barter = any(is_barter for _, is_barter in paid)
    if any(is_barter and not part for part, is_barter in paid):
        return 0, True
    due = amount - sum(part or 0 for part, _ in paid) if amount else None
    if due is not None and float(due).is_integer():
        due = int(due)
    return due, barter


def _name_due_barter(call: Call, deal: dict, barter: dict) -> tuple[str, float | None, bool]:
    """Guest name, amount still to pay and the barter mark for a deal."""
    due, is_barter = due_and_barter(_amount(deal.get("OPPORTUNITY")), [
        (_amount(deal.get(a)), str(deal.get(m)) in barter[m])
        for a, m in ((F_PREPAID, F_PREPAY_METHOD), (F_SURCHARGE, F_SURCHARGE_METHOD))])
    name = deal.get("TITLE") or "сделка без названия"
    if deal.get("CONTACT_ID") and str(deal["CONTACT_ID"]) != "0":
        contact = call("crm.contact.get", {"id": deal["CONTACT_ID"]}).get("result") or {}
        name = " ".join(filter(None, (contact.get("NAME"), contact.get("LAST_NAME")))) or name
    return name, due, is_barter


def _guests(call: Call, today: date) -> dict:
    items, barter = [], None
    for v in visits(call, today, today + timedelta(days=1)):
        barter = barter or barter_ids(call)
        name, due, is_barter = _name_due_barter(call, v["deal"], barter)
        items.append({"time": v["time"], "name": name, "program": "", "due": due, "barter": is_barter})
    return {"status": "ok", "source": "Битрикс, сделки", "items": items}


MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
          "Октябрь", "Ноябрь", "Декабрь")


def _occupancy(call: Call, today: date) -> dict:
    """Busy days (at least one visit): the next 7 days and the whole calendar month, past days included."""
    first = today.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    week_end = today + timedelta(days=7)
    days = {v["start"].date() for v in visits(call, first, max(following, week_end))}
    week = sorted(d for d in days if today <= d < week_end)
    return {"status": "ok", "source": "Битрикс, сделки", "week_busy": len(week), "week_days": 7,
            "week_dates": [d.strftime("%d.%m") for d in week], "month": MONTHS[today.month - 1],
            "month_busy": sum(1 for d in days if first <= d < following), "month_days": (following - first).days}


CHANNELS = {"inst": "Instagram", "telega": "Telegram", "vk": "VK", "авито": "Авито", "max": "Max"}
SUBJECT = re.compile(r'"(?P<client>.+) - (?P<line>[^"]+)"')


def _waiting(call: Call, today: date, now: datetime) -> dict:
    # An open-line chat waits for us when its last non-system message came
    # from the client (a connector user), not from an operator.
    since = omsk_day_range(today - timedelta(days=3))[0]
    acts = fetch_all(call, "crm.activity.list", {
        "filter[PROVIDER_ID]": "IMOPENLINES_SESSION", "filter[COMPLETED]": "N",
        "filter[>=LAST_UPDATED]": since,
        "select[]": ["ID", "SUBJECT", "ASSOCIATED_ENTITY_ID"]})
    items = []
    for act in acts:
        hist = call("imopenlines.session.history.get", {"SESSION_ID": act["ASSOCIATED_ENTITY_ID"]})
        if "error" in hist:
            raise RuntimeError(hist["error"])
        hist = hist.get("result") or {}
        users = hist.get("users") or {}
        msgs = [m for _, m in sorted((hist.get("message") or {}).items(), key=lambda kv: int(kv[0]))
                if str(m.get("senderid", 0)) != "0"]
        if not msgs or not (users.get(str(msgs[-1]["senderid"])) or {}).get("connector"):
            continue
        hours = int((now - datetime.fromisoformat(msgs[-1]["date"])).total_seconds() // 3600)
        match = SUBJECT.search(act.get("SUBJECT", ""))
        client, line = (match["client"], match["line"]) if match else ("—", "")
        items.append((hours, {"channel": CHANNELS.get(line.strip().lower(), line or "Битрикс"),
                              "client": client, "waiting": f"{hours} ч"}))
    items.sort(key=lambda pair: -pair[0])
    return {"status": "ok", "source": "Битрикс", "items": [item for _, item in items]}


# Stages where a visit is agreed, so the deal must carry its date, time and length.
BOOKED_STAGES = {"0": ["PREPAYMENT_INVOICE", "EXECUTING", "FINAL_INVOICE", "UC_E1TLGK"],
                 "1": ["C1:PREPAYMENT_INVOICE", "C1:FINAL_INVOICE"]}
CHECK_DAYS = 120


def _visit_check(call: Call, today: date, portal: str) -> dict:
    """What the team has to fix in deals now that the deal is the only record of a visit:
    booked-stage deals without a date, upcoming visits without a time or a length,
    and visits that overlap."""
    url = lambda deal_id: f"https://{portal}/crm/deal/details/{deal_id}/"
    title = lambda d: f"«{d.get('TITLE') or d['ID']}»"
    items = []
    for cat, stages in BOOKED_STAGES.items():
        deals = fetch_all(call, "crm.deal.list", {"filter[CATEGORY_ID]": cat, "filter[STAGE_ID][]": stages,
                                                  "filter[CLOSED]": "N", "select[]": ["ID", "TITLE", "STAGE_ID", F_VISIT]})
        items += [{"kind": "нет даты записи", "text": title(d), "url": url(d["ID"])}
                  for d in deals if d.get("STAGE_ID") in stages and not d.get(F_VISIT)]
    upcoming = visits(call, today, today + timedelta(days=CHECK_DAYS))
    for v in upcoming:
        missing = "нет времени" if not v["timed"] else "нет длительности" if not v["end"] else None
        if missing:
            items.append({"kind": missing, "text": f"{v['start']:%d.%m} — {title(v['deal'])}",
                          "url": url(v["deal"]["ID"])})
    known = [v for v in upcoming if v["end"]]
    for i, a in enumerate(known):
        for b in known[i + 1:]:
            if b["start"] < a["end"]:
                items.append({"kind": "пересечение по времени",
                              "text": f"{a['start']:%d.%m} — {title(a['deal'])} {a['time']} и {title(b['deal'])} {b['time']}",
                              "url": url(b["deal"]["ID"])})
    return {"status": "ok", "source": "Битрикс, сделки", "items": items}


PAY_SOURCES = (  # (date field, amount field, method field)
    ("UF_CRM_PREPAY_DATE", "UF_CRM_1790612405079", "UF_CRM_PREPAY_METHOD"),
    ("UF_CRM_SURCHARGE_DATE", "UF_CRM_SURCHARGE", "UF_CRM_SURCHARGE_METHOD"),
)


def _amount(value) -> float:
    try:
        return float(str(value or "0").split("|")[0] or 0)
    except ValueError:
        return 0.0


def _payments(call: Call, today: date) -> dict:
    """Money received yesterday (Omsk), grouped by payment method."""
    fields = call("crm.deal.fields", {})
    if "error" in fields:
        raise RuntimeError(fields["error"])
    fields = fields.get("result", {})
    day, next_day = (today - timedelta(days=1)).isoformat(), today.isoformat()
    groups: dict[str, list] = {}
    for date_f, amount_f, method_f in PAY_SOURCES:
        names = {str(i["ID"]): i["VALUE"] for i in (fields.get(method_f) or {}).get("items", [])}
        deals = fetch_all(call, "crm.deal.list", {f"filter[>={date_f}]": day, f"filter[<{date_f}]": next_day,
                                                  "select[]": ["ID", amount_f, method_f]})
        for d in deals:
            amount = _amount(d.get(amount_f))
            if not amount:
                continue
            method = names.get(str(d.get(method_f)), "способ не указан")
            if method == BARTER:
                continue
            g = groups.setdefault(method, [0.0, 0])
            g[0] += amount
            g[1] += 1
    num = lambda v: int(v) if float(v).is_integer() else v
    items = [{"method": m, "amount": num(a), "count": c}
             for m, (a, c) in sorted(groups.items(), key=lambda kv: -kv[1][0])]
    return {"status": "ok", "source": "Битрикс", "total": num(sum(g[0] for g in groups.values())), "items": items}


def collect(call: Call, today: date, now: datetime | None = None, portal: str = "") -> dict:
    now = now or datetime.now(OMSK)
    result = {}
    for key, source, fn in (
        ("deals", "Битрикс", lambda: _deals(call, today)),
        ("today_guests", "Битрикс", lambda: _guests(call, today)),
        ("waiting_reply_bitrix", "Битрикс", lambda: _waiting(call, today, now)),
        ("payments", "Битрикс", lambda: _payments(call, today)),
        ("link_check", "Битрикс", lambda: _visit_check(call, today, portal)),
        ("occupancy", "Битрикс", lambda: _occupancy(call, today)),
    ):
        try:
            result[key] = fn()
        except Exception as exc:  # any source failure must not break the brief
            result[key] = _no_data(source, exc)
    return result


if __name__ == "__main__":
    url = os.environ.get("BITRIX_WEBHOOK_URL", "")
    if not url:
        err = {"status": "no_data", "source": "Битрикс", "error": "вебхук не настроен"}
        print(json.dumps({k: err for k in ("deals", "today_guests", "waiting_reply_bitrix", "payments", "link_check",
                                                     "occupancy")},
                         ensure_ascii=False))
    else:
        from urllib.parse import urlparse
        print(json.dumps(collect(make_call(url), omsk_today(), portal=urlparse(url).hostname or ""),
                         ensure_ascii=False))
