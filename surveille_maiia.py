#!/usr/bin/env python3
"""Veille Maiia à la demande pour Dr Stéphanie Berthelen (suivi)."""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ACTIVATION_FILE = "maiia_activation.json"
STATE_FILE = "maiia_etat.json"
CONFIG_FILE = "config.json"
CHECK_URL = "https://www.maiia.com/api/pat-public/availabilities"
BOOKING_URL = (
    "https://www.maiia.com/rhumatologue/67000-strasbourg/"
    "berthelen-stephanie/rdv?speciality=rhumatologue&locality=67000-strasbourg"
    "&slug=berthelen-stephanie&centerId=6144c2e17ba4ff01e813ac64"
)
PARAMS = {
    "centerId": "6144c2e17ba4ff01e813ac64",
    "practitionerId": "6144c3fe7ba4ff01e813ac78",
    "consultationReasonId": "614ad3b099a3717794eb55cc",
    "limit": "2016",
}
UA = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(value):
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def topic():
    value = load_json(CONFIG_FILE, {}).get("ntfy_topic", "").strip()
    if not value:
        raise RuntimeError("ntfy_topic absent de config.json")
    return value


def notify(title, body, priority="high"):
    request = urllib.request.Request(
        "https://ntfy.sh/" + topic(),
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "calendar,medical_symbol",
            "Click": BOOKING_URL,
        },
    )
    urllib.request.urlopen(request, timeout=30).read()


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def start():
    started = now_utc()
    activation = {
        "active": True,
        "started_at": iso(started),
        "from": iso(started + dt.timedelta(days=3)),
        "to": iso(started + dt.timedelta(days=23)),
    }
    save_json(ACTIVATION_FILE, activation)
    save_json(STATE_FILE, {"known": []})
    print("Veille Maiia activée : créneaux entre J+3 et J+23.")
    notify(
        "Veille Maiia activée",
        "Dr Stéphanie Berthelen — suivi : recherche entre J+3 et J+23.",
        priority="default",
    )
    return 0


def stop(expired=False):
    activation = load_json(ACTIVATION_FILE, {})
    activation["active"] = False
    save_json(ACTIVATION_FILE, activation)
    message = "Veille Maiia arrivée à J+23 : arrêt automatique." if expired else "Veille Maiia arrêtée."
    print(message)
    if expired:
        notify("Veille Maiia terminée", message, priority="default")
    return 0


def fetch_slots(date_from, date_to):
    params = dict(PARAMS)
    params.update({"from": iso(date_from), "to": iso(date_to)})
    request = urllib.request.Request(
        CHECK_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("items", [])


def check():
    activation = load_json(ACTIVATION_FILE, {})
    if not activation.get("active"):
        print("Veille Maiia inactive. Lancez maiia-start après votre rendez-vous.")
        return 2

    date_from = parse_time(activation["from"])
    date_to = parse_time(activation["to"])
    if now_utc() > date_to:
        stop(expired=True)
        return 2

    slots = fetch_slots(date_from, date_to)
    state = load_json(STATE_FILE, {"known": []})
    known = set(state.get("known", []))
    current = set()
    new_slots = []
    for slot in slots:
        start_time = slot.get("startDateTime")
        if not start_time:
            continue
        identifier = str(slot.get("id") or start_time)
        current.add(identifier)
        if identifier not in known:
            new_slots.append(start_time)

    if new_slots:
        paris = ZoneInfo("Europe/Paris")
        labels = [parse_time(value).astimezone(paris).strftime("%d/%m à %H:%M") for value in sorted(new_slots)]
        body = "Nouveau(x) créneau(x) de suivi : " + ", ".join(labels[:8])
        print(body)
        notify("Créneau Maiia disponible !", body)
    else:
        print(f"Maiia : RAS ({len(slots)} créneau(x) entre J+3 et J+23).")

    state["known"] = sorted(current)
    save_json(STATE_FILE, state)
    return 0


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "start":
        return start()
    if command == "stop":
        return stop()
    return check()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERREUR Maiia : {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
