import pdfplumber
import requests
from datetime import datetime
import io
import os
from bidi.algorithm import get_display
import csv
from yeela import get_yeela_data


# --- הגדרות ---
Haifa_streets = 'streer_to_neigh.csv'
TELEGRAM_TOKEN = "REVOKED - see .env"
CHAT_ID = "-1003612026467"
HISTORY_FILE = "sent_licenses.txt"
PDF_URL = "https://www3.haifa.muni.il/trees/%D7%A8%D7%A9%D7%99%D7%9E%D7%AA%20%D7%91%D7%A7%D7%A9%D7%95%D7%AA.pdf"
today = datetime.now()



# --- פונקציות ניהול היסטוריה ושליחה ---

def load_sent_history():
    if not os.path.exists(HISTORY_FILE): return set()
    with open(HISTORY_FILE, "r") as f:
        return set(line.strip() for line in f)


def save_to_history(license_id):
    with open(HISTORY_FILE, "a") as f:
        f.write(f"{license_id}\n")


def send_telegram_msg(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"שגיאה בשליחה: {e}")

def send_action_links():
    """שולחת הודעת עזר עם קישורים שימושיים"""
    action_message = (
        "🔗 <b>קישורים ומידע נוסף :</b>\n\n"
        "📋 <a href='https://www3.haifa.muni.il/trees/%D7%A8%D7%A9%D7%99%D7%9E%D7%AA%20%D7%91%D7%A7%D7%A9%D7%95%D7%AA.pdf'>לטבלה המלאה באתר העירייה</a>\n\n"
        "🌳 <a href='https://yeela-trees.moag.gov.il/FoPublic/FoLicence'>מערכת יעלה (משרד החקלאות)</a>\n"
        "<i>(באתר יעלה יש לבחור 'חיפה' בשם הישוב)</i>\n\n"
        "📄 <b>טופס הגשת השגה:</b>\n"
        "<a href='https://yeela-trees.moag.gov.il/api/Fo/doc/documents/GetFileForPublic?folder=documents&name=%D7%94%D7%92%D7%A9%D7%AA%20%D7%94%D7%A9%D7%92%D7%94%20%D7%9C%D7%A4%D7%A7%D7%99%D7%93%20%D7%94%D7%99%D7%A2%D7%A8%D7%95%D7%AA.docx'>לחצו כאן להורדת טופס ההשגה הרשמי (Word)</a>\n\n"
        "📧 <b>לאן שולחים?</b>\n"
        "יש לשלוח את הטופס המלא למייל פקיד היערות הארצי:\n"
        "<code>trees@moag.gov.il</code>\n\n"
        "יש להגיש את ההשגה תוך 14 יום ממועד פרסום הרישיון.\n\n")

    send_telegram_msg(action_message)


# --- פונקציות עיבוד טקסט ושכונות ---

def fix_hebrew(text):
    return get_display(str(text))


def normalize_street(text):
    if not text: return ""
    text = str(text).replace('\n', ' ').strip()
    noise_words = ["רחוב", "נתיב", "שדרות", "סמטת", "דרך"]
    for word in noise_words:
        text = text.replace(word, "")
    return " ".join(text.split())


def load_neighborhood_mapping(csv_path):
    mapping = {}
    try:
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                mapping[row['STREET_NAM'].strip()] = row['NEIGH'].strip()
    except Exception as e:
        print(f"שגיאה בטעינת ה-CSV: {e}")
    return mapping


neighborhood_map = load_neighborhood_mapping(Haifa_streets)


def find_neighborhood_smart(street, mapping):
    street_clean = normalize_street(street)
    street_words = set(street_clean.split())

    best_match = "שכונה לא ידועה"
    max_intersection = 0

    for csv_street, neighborhood in mapping.items():
        csv_clean = normalize_street(csv_street)
        csv_words = set(csv_clean.split())

        if not csv_words: continue

        # בדיקה: כמה מילים משותפות יש בין השם ב-CSV לשם שקיבלנו?
        # למשל: {'אינשטיין', 'אלברט'} ו- {'אינשטיין', '46'} -> מילה אחת משותפת ('אינשטיין')
        common_words = street_words.intersection(csv_words)
        intersection_count = len(common_words)

        # אם מצאנו התאמה טובה יותר מהקודמת
        if intersection_count > max_intersection:
            max_intersection = intersection_count
            best_match = neighborhood

        # במקרה של שוויון (למשל רחוב 'חנה' ורחוב 'חנה רובינא' ושניהם תואמים למילה אחת)
        # נעדיף את הרחוב הקצר יותר ב-CSV כדי למנוע התאמות שווא רחוקות
        elif intersection_count > 0 and intersection_count == max_intersection:
            if len(csv_words) < len(
                    set(normalize_street(best_match).split())):  # פשוט נשמור על הראשון או נבצע לוגיקה עדינה יותר
                pass

    return best_match
# --- פונקציות שליפת נתונים ---

def get_relevant_trees_from_pdf(pdf_url):
    print("סורק PDF עירייה...")
    try:
        response = requests.get(pdf_url)
        response.raise_for_status()
        rows = []
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for page in pdf.pages:
                table = page.extract_table()
                if not table: continue
                for row in table:
                    clean_row = [str(cell).strip() if cell else "" for cell in row]
                    dates = []
                    for cell in clean_row:
                        try:
                            dates.append(datetime.strptime(cell, "%d/%m/%Y"))
                        except:
                            continue
                    if len(dates) >= 2 and max(dates) >= today:
                        # ב-PDF עמודה 1 היא כמות (לפי הצילום שלך)
                        rows.append({
                            'id': clean_row[14],
                            'street': fix_hebrew(clean_row[6]),
                            'house': clean_row[5],
                            'type': fix_hebrew(clean_row[2]),
                            'count': int(clean_row[1]) if clean_row[1].isdigit() else 1,
                            'appeal': clean_row[12], 'reason': clean_row[9], 'pub_date': min(dates),
                            'source': 'עיריית חיפה', 'fix': True
                        })
        return rows
    except Exception as e:
        print(f"שגיאה ב-PDF: {e}");
        return []


# --- הפונקציה הראשית לאיחוד ושליחה ---

def main():
    print("מתחיל סריקה משולבת ואגרגציה...")

    # איסוף מ-PDF
    pdf_data = get_relevant_trees_from_pdf(PDF_URL)

    # איסוף מיעלה (כאן אנחנו צריכים לוודא שאנחנו לוקחים רק כריתה)
    # הערה: get_yeela_data כבר מחזירה רשימה בפורמט Row. נמפה אותה לאובייקט.
    yeela_raw = get_yeela_data()
    combined = {}
    for r in yeela_raw:
        lic_id = r[14]
        # בדיקה אם ניתן לערער (סטטוס)
        can_app = "פתוח להגשת" in str(r[9]) or "מושהה" in str(r[9])
        if lic_id not in combined:
            combined[lic_id] = {'street': r[6], 'house': r[5], 'types': {r[2]}, 'count': r[1], 'source': 'מערכת יעלה',
                                'appeal': r[12], 'pub_date': r[15], 'can_appeal': can_app, 'fix': False}
        else:
            combined[lic_id]['types'].add(str(r[2]) if r[2] else "לא ידוע");
            combined[lic_id]['count'] += r[1]

    for r in pdf_data:
        lic_id = r['id']
        can_app = "מסוכן" not in r['reason'] and "לא ניתן" not in r['reason']
        if lic_id not in combined:
            combined[lic_id] = {'street': r['street'], 'house': r['house'], 'types': {r['type']}, 'count': r['count'],
                                'source': r['source'], 'appeal': r['appeal'], 'pub_date': r['pub_date'],
                                'can_appeal': can_app, 'fix': True}
        else:
            combined[lic_id]['count'] = max(combined[lic_id]['count'], r['count'])
            combined[lic_id]['types'].add(str(r['type']) if r['type'] else "לא ידוע")


    history = load_sent_history()
    new_licenses = {k: v for k, v in combined.items() if k not in history}
    if not new_licenses: print("אין חדש."); return


    # קיבוץ לפי שכונות לצורך הודעה
    by_neigh = {}
    for lic_id, info in new_licenses.items():
        neigh = find_neighborhood_smart(info['street'], neighborhood_map)
        if neigh not in by_neigh: by_neigh[neigh] = []
        by_neigh[neigh].append(info)
        save_to_history(lic_id)

    # בניית הודעה
    msg = "🌳 <b>עדכון רישיונות כריתה חדשים בחיפה</b> 🌳\n\n"
    alerts = ""
    found_alerts = False
    for neigh, items in by_neigh.items():
        msg += f"📍 <b>{neigh}</b>:\n"
        for item in items:
            # זיהוי השתלה בדיעבד (מעל 5 ימים)
            days_diff = (today - item['pub_date']).days
            if days_diff > 5:
                if not found_alerts:
                    alerts = "🚨 <b>זוהו רישיונות שהוכנסו למערכת בדיעבד!</b>\n\n"
                    found_alerts = True

                    # ניקוי כתובת להתראה
                st_alert, hs_alert = str(item['street']).strip(), str(item['house']).strip()
                addr_alert = f"{st_alert} {hs_alert}" if hs_alert and hs_alert.lower() != 'none' and hs_alert not in st_alert else st_alert
                alerts += f"🚩 רישיון חדש מספר {lic_id} בכתובת {addr_alert}\nעלה היום עם התאריך-{item['pub_date'].strftime('%d/%m/%Y')}\n\n"

            trees_str = ", ".join([str(t) for t in item['types'] if t]).replace('\n', ' ')

            count_txt = f"<b>{item['count']} עצים</b>" if item['count'] > 1 else "עץ אחד"
            street_part = str(item['street']).strip()
            house_val = str(item['house']).strip()
            appeal = (item['appeal'])

            # בדיקה אם מספר הבית קיים, אינו "None", ולא מופיע כבר בתוך שם הרחוב
            if house_val and house_val.lower() != 'none' and house_val not in street_part:
                full_address = f"{street_part} {house_val}"
            else:
                full_address = street_part

            # הסרת רווחים כפולים (אם היו)
            full_address = " ".join(full_address.split())

            # עכשיו אפשר להוסיף לשורת ההודעה
            msg += f"• {full_address} | {count_txt} ({trees_str}) | תאריך אחרון לערעור: <b>{appeal}</b> | <i>מקור: {item['source']}</i>\n"
        msg += "\n"

    msg += f"📅 <i>עדכון: {datetime.now().strftime('%d/%m/%Y')}</i>"
    print(msg)
    send_telegram_msg(msg)

    if found_alerts:
        print("--- הודעת התראות בדיעבד ---")
        alerts += f"📅 <i>עדכון: {datetime.now().strftime('%d/%m/%Y')}</i>"
        print(alerts)
        send_telegram_msg(alerts)

    send_action_links()
    print("הודעה נשלחה!")


if __name__ == "__main__":
    main()