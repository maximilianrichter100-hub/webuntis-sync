import os
import re
import sys
from datetime import datetime, date, timedelta
import zoneinfo
import requests
from icalendar import Calendar, Event

# Konfiguration Hochschule Albstadt-Sigmaringen
SCHOOL = "hs-albstadt"
BASE_URL = "https://hs-albstadt.webuntis.com/WebUntis"
TARGET_NAME = "DPM1 (Wiese)"
ICS_FILENAME = "dpm1_wiese.ics"
TZ = zoneinfo.ZoneInfo("Europe/Berlin")

def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    })
    return s

def resolve_target_id(session):
    """Ermittelt dynamisch die interne WebUntis-Klassen-ID für DPM1 (Wiese)"""
    url = f"{BASE_URL}/api/public/timetable/weekly/pageconfig?type=1"
    params = {"school": SCHOOL}
    try:
        res = session.get(url, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        elements = data.get("data", {}).get("elements", [])
        for elem in elements:
            name = elem.get("name", "")
            display_name = elem.get("displayname", "")
            if TARGET_NAME.lower() in name.lower() or TARGET_NAME.lower() in display_name.lower() or "dpm1" in name.lower():
                print(f"[OK] Klasse gefunden: '{name}' (ID: {elem.get('id')})")
                return elem.get("id"), name
    except Exception as e:
        print(f"[Warnung] Pageconfig-Lookup fehlgeschlagen: {e}")

    # Fallback via JSON-RPC Klassenliste
    rpc_url = f"{BASE_URL}/jsonrpc.do?school={SCHOOL}"
    payload = {
        "id": "1",
        "method": "getKlassen",
        "params": {},
        "jsonrpc": "2.0"
    }
    try:
        res = session.post(rpc_url, json=payload, timeout=20)
        res.raise_for_status()
        result = res.json().get("result", [])
        for k in result:
            name = k.get("name", "")
            long_name = k.get("longName", "")
            if "dpm" in name.lower() or "dpm" in long_name.lower():
                print(f"[OK] Klasse über RPC gefunden: '{name}' (ID: {k.get('id')})")
                return k.get("id"), name
    except Exception as e:
        print(f"[Warnung] RPC-Lookup fehlgeschlagen: {e}")

    # Standard-Fallback falls API-Listing blockiert ist
    print("[Info] Verwende Standard-Fallback-ID.")
    return None, TARGET_NAME

def fetch_timetable_data(session, element_id):
    """Liest Termine über 12 Wochen ab heute aus"""
    start_date = date.today() - timedelta(days=date.today().weekday()) # Montag aktueller Woche
    all_lessons = []

    # Frage 12 Wochen in 2-Wochen-Schritten ab
    for w in range(0, 12, 2):
        cur_date = start_date + timedelta(weeks=w)
        date_str = cur_date.strftime("%Y-%m-%d")
        url = f"{BASE_URL}/api/public/timetable/weekly/data"
        params = {
            "school": SCHOOL,
            "elementType": 1,
            "elementId": element_id if element_id else 0,
            "date": date_str,
            "formatId": 1
        }
        try:
            res = session.get(url, params=params, timeout=25)
            if res.status_code == 200:
                payload = res.json()
                data = payload.get("data", {})
                elements_map = {el["id"]: el.get("displayname", el.get("name", "")) 
                                for el in data.get("result", {}).get("data", {}).get("elements", []) if "id" in el}
                
                element_periods = data.get("result", {}).get("data", {}).get("elementPeriods", {})
                # elementPeriods ist oft ein Dict mit ID als Key
                periods = []
                for p_list in element_periods.values():
                    if isinstance(p_list, list):
                        periods.extend(p_list)

                for item in periods:
                    # Untis liefert Datums-Integer z. B. 20261015 und Zeiten 800 (08:00) oder 1415 (14:15)
                    raw_date = str(item.get("date"))
                    raw_start = str(item.get("startTime")).zfill(4)
                    raw_end = str(item.get("endTime")).zfill(4)

                    dt_start = datetime.strptime(f"{raw_date}{raw_start}", "%Y%m%d%H%M").replace(tzinfo=TZ)
                    dt_end = datetime.strptime(f"{raw_date}{raw_end}", "%Y%m%d%H%M").replace(tzinfo=TZ)

                    # Zugehörige Fächer/Räume auflösen
                    subjects = [elements_map.get(el.get("id"), "") for el in item.get("elements", []) if el.get("type") == 3]
                    teachers = [elements_map.get(el.get("id"), "") for el in item.get("elements", []) if el.get("type") == 2]
                    rooms = [elements_map.get(el.get("id"), "") for el in item.get("elements", []) if el.get("type") == 4]

                    subject_str = ", ".join(filter(None, subjects)) or "Vorlesung"
                    teacher_str = ", ".join(filter(None, teachers))
                    room_str = ", ".join(filter(None, rooms))

                    all_lessons.append({
                        "id": item.get("id"),
                        "start": dt_start,
                        "end": dt_end,
                        "subject": subject_str,
                        "teacher": teacher_str,
                        "room": room_str,
                        "notes": item.get("lessonText", "") or ""
                    })
        except Exception as err:
            print(f"[Fehler] Abruf für Woche {date_str}: {err}")

    return all_lessons

def build_ics(lessons):
    cal = Calendar()
    cal.add("prodid", "-//WebUntis Sync Albstadt//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"{TARGET_NAME} - WebUntis")
    cal.add("x-wr-timezone", "Europe/Berlin")

    # Duplikate filtern nach eindeutiger UID
    seen_uids = set()

    for item in lessons:
        uid = f"untis-{item.get('id') or item['start'].strftime('%Y%m%d%H%M')}@hs-albstadt"
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        ev = Event()
        summary = item["subject"]
        if item["room"]:
            summary += f" ({item['room']})"
        
        desc_lines = []
        if item["teacher"]:
            desc_lines.append(f"Dozent: {item['teacher']}")
        if item["room"]:
            desc_lines.append(f"Raum: {item['room']}")
        if item["notes"]:
            desc_lines.append(f"Hinweis: {item['notes']}")
        desc_lines.append("Automatisch synchronisiert aus WebUntis.")

        ev.add("summary", summary)
        ev.add("description", "\n".join(desc_lines))
        if item["room"]:
            ev.add("location", item["room"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        ev.add("dtstamp", datetime.now(TZ))
        ev.add("uid", uid)

        cal.add_component(ev)

    with open(ICS_FILENAME, "wb") as f:
        f.write(cal.to_ical())
    print(f"[ERFOLG] {len(seen_uids)} Termine in '{ICS_FILENAME}' exportiert.")

if __name__ == "__main__":
    session = get_session()
    class_id, class_name = resolve_target_id(session)
    lessons = fetch_timetable_data(session, class_id)
    build_ics(lessons)
