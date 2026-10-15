import sys
from datetime import datetime, date, timedelta
import zoneinfo
import requests
from icalendar import Calendar, Event

SCHOOL = "hs-albstadt"
BASE_URL = "https://hs-albstadt.webuntis.com/WebUntis"
TARGET_NAME = "DPM_1 (WiSe)"
ICS_FILENAME = "dpm1_wiese.ics"
TZ = zoneinfo.ZoneInfo("Europe/Berlin")

def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/?school={SCHOOL}"
    })
    # Wichtig: Einmal die Hauptseite laden, damit WebUntis die Session-Cookies setzt!
    try:
        s.get(f"{BASE_URL}/?school={SCHOOL}", timeout=20)
    except Exception as e:
        print(f"[Warnung] Initialer Cookie-Abruf: {e}")
    return s

def resolve_target_id(session):
    # Suche über alle relevanten Typen (1 = Klasse/Studiengang)
    for t in [1, 2, 3, 4]:
        url = f"{BASE_URL}/api/public/timetable/weekly/pageconfig"
        params = {"type": t, "school": SCHOOL}
        try:
            res = session.get(url, params=params, timeout=20)
            if res.status_code == 200:
                elements = res.json().get("data", {}).get("elements", [])
                for elem in elements:
                    name = str(elem.get("name", ""))
                    long_name = str(elem.get("longName", ""))
                    display_name = str(elem.get("displayname", ""))
                    combined = f"{name} {long_name} {display_name}".lower()
                    
                    if "dpm_1" in combined or "dpm1" in combined:
                        print(f"[OK] Gefunden (Typ {t}): '{name}' / '{long_name}' (ID: {elem.get('id')})")
                        return elem.get("id"), t
        except Exception as e:
            print(f"[Warnung] Pageconfig Typ {t} fehlgeschlagen: {e}")

    # Fallback über JSON-RPC API
    rpc_url = f"{BASE_URL}/jsonrpc.do?school={SCHOOL}"
    payload = {"id": "1", "method": "getKlassen", "params": {}, "jsonrpc": "2.0"}
    try:
        res = session.post(rpc_url, json=payload, timeout=20)
        if res.status_code == 200:
            for k in res.json().get("result", []):
                name = str(k.get("name", ""))
                long_name = str(k.get("longName", ""))
                if "dpm_1" in name.lower() or "dpm_1" in long_name.lower() or "dpm1" in name.lower():
                    print(f"[OK] Über RPC gefunden: '{name}' (ID: {k.get('id')})")
                    return k.get("id"), 1
    except Exception as e:
        print(f"[Warnung] RPC fehlgeschlagen: {e}")

    return None, 1

def fetch_timetable_data(session, element_id, element_type):
    # Starte am Montag der aktuellen Woche
    today = date.today()
    start_date = today - timedelta(days=today.weekday())
    all_lessons = []

    if not element_id:
        print("[FEHLER] Klassen-ID für DPM_1 (WiSe) konnte nicht gefunden werden!")
        return all_lessons

    # 18 Wochen im Voraus abrufen (deckt das komplette Semester ab)
    for w in range(0, 18):
        cur_date = start_date + timedelta(weeks=w)
        date_str = cur_date.strftime("%Y-%m-%d")
        url = f"{BASE_URL}/api/public/timetable/weekly/data"
        params = {
            "elementType": element_type,
            "elementId": element_id,
            "date": date_str,
            "formatId": 1
        }
        try:
            res = session.get(url, params=params, timeout=25)
            if res.status_code == 200:
                result_data = res.json().get("data", {}).get("result", {}).get("data", {})
                
                # Mapping aller IDs zu lesbaren Namen (Fächer, Räume, Dozenten)
                elements_map = {}
                for el in result_data.get("elements", []):
                    eid = el.get("id")
                    etype = el.get("type")
                    ename = el.get("longName") or el.get("displayname") or el.get("name") or ""
                    elements_map[(etype, eid)] = ename

                element_periods = result_data.get("elementPeriods", {})
                periods = element_periods.get(str(element_id), [])
                if not periods:
                    for p_list in element_periods.values():
                        if isinstance(p_list, list):
                            periods.extend(p_list)

                for item in periods:
                    # Überspringe komplett ausgefallene Stunden
                    if item.get("cellState") == "CANCEL":
                        continue

                    raw_date = str(item.get("date"))
                    raw_start = str(item.get("startTime")).zfill(4)
                    raw_end = str(item.get("endTime")).zfill(4)

                    dt_start = datetime.strptime(f"{raw_date}{raw_start}", "%Y%m%d%H%M").replace(tzinfo=TZ)
                    dt_end = datetime.strptime(f"{raw_date}{raw_end}", "%Y%m%d%H%M").replace(tzinfo=TZ)

                    subjects, teachers, rooms = [], [], []
                    for el in item.get("elements", []):
                        etype = el.get("type")
                        eid = el.get("id")
                        name_val = elements_map.get((etype, eid), "")
                        if name_val:
                            if etype == 3:
                                subjects.append(name_val)
                            elif etype == 2:
                                teachers.append(name_val)
                            elif etype == 4:
                                rooms.append(name_val)

                    subject_str = ", ".join(dict.fromkeys(subjects)) or item.get("lessonText") or "Vorlesung"
                    teacher_str = ", ".join(dict.fromkeys(teachers))
                    room_str = ", ".join(dict.fromkeys(rooms))
                    notes_str = item.get("periodText") or item.get("lessonText") or ""

                    all_lessons.append({
                        "id": item.get("id"),
                        "start": dt_start,
                        "end": dt_end,
                        "subject": subject_str,
                        "teacher": teacher_str,
                        "room": room_str,
                        "notes": notes_str
                    })
        except Exception as err:
            print(f"[Fehler] Abruf Woche {date_str}: {err}")

    return all_lessons

def build_ics(lessons):
    cal = Calendar()
    cal.add("prodid", "-//WebUntis Sync HS Albstadt//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"{TARGET_NAME} - Stundenplan")
    cal.add("x-wr-timezone", "Europe/Berlin")

    seen_uids = set()

    for item in lessons:
        # Eindeutige ID pro Terminblock
        uid = f"untis-{item['start'].strftime('%Y%m%dT%H%M')}-{item['subject'][:12].replace(' ', '')}@hs-albstadt"
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        ev = Event()
        summary = item["subject"]
        if item["room"]:
            summary += f" [{item['room']}]"

        desc = []
        if item["teacher"]:
            desc.append(f"Dozent: {item['teacher']}")
        if item["room"]:
            desc.append(f"Raum: {item['room']}")
        if item["notes"]:
            desc.append(f"Info: {item['notes']}")

        ev.add("summary", summary)
        ev.add("description", "\n".join(desc))
        if item["room"]:
            ev.add("location", item["room"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        ev.add("dtstamp", datetime.now(TZ))
        ev.add("uid", uid)

        cal.add_component(ev)

    with open(ICS_FILENAME, "wb") as f:
        f.write(cal.to_ical())
    print(f"[ERFOLG] {len(seen_uids)} Vorlesungstermine erfolgreich in '{ICS_FILENAME}' gespeichert!")

if __name__ == "__main__":
    s = get_session()
    cid, ctype = resolve_target_id(s)
    lessons = fetch_timetable_data(s, cid, ctype)
    build_ics(lessons)
