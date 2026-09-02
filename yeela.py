import requests
import json
from datetime import datetime


def get_yeela_data():
    url = "https://yeela-trees.moag.gov.il/api/Fo/FOServiceRequest/getFOGridPublicityLicenses"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'userOrgRoleId': '',
        'Origin': 'https://yeela-trees.moag.gov.il',
        'Referer': 'https://yeela-trees.moag.gov.il/FoPublic/FoLicence'
    }

    # המבנה המדויק שחילצנו מה-cURL שלך
    payload = {
        "orderDetails": None,
        "pageDetails": {
            "pageNumber": 1,
            "pageSize": 100  # הגדלתי ל-50 כדי לקבל יותר תוצאות בבת אחת
        },
        "parameters": {
            "cityId": 4000,  # ה-ID של חיפה
            "appealLastDate": None
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            print(f"שגיאת שרת: {response.status_code}")
            return []

        data = response.json()
        # בשרת הזה התוצאות נמצאות תחת המפתח 'result'
        raw_items = data.get('result', [])
        processed_results = []

        for item in raw_items:
            # חילוץ נתונים
            license_id = str(item.get('licenseId', ''))
            tree_name = item.get('treeName', 'לא ידוע')
            status_desc = item.get('licenseStatusDesc', '')

            # תאריך אחרון לערעור
            raw_date = item.get('appealLastDate', '')
            appeal_date = "לא ידוע"
            if raw_date:
                appeal_date = datetime.strptime(raw_date.split('T')[0], '%Y-%m-%d').strftime('%d/%m/%Y')

            # תאריך פרסום (לצורך זיהוי השתלות בדיעבד)
            pub_date = datetime.now()
            raw_pub = item.get('approvedDate', '')
            if raw_pub:
                pub_date = datetime.strptime(raw_pub.split('T')[0], '%Y-%m-%d')

            # חילוץ רחוב מתוך expandRows
            street = "לא צוין"
            expand_rows = item.get('expandRows', [])
            if expand_rows:
                street = expand_rows[0].get('street', 'לא צוין').strip()

            unproot_count = item.get('unproot', 1)

            # בניית שורה בפורמט שהבוט שלך מכיר (רשימה של 15 איברים)
            row = [None] * 16
            row[1] = unproot_count  # כמות
            row[2] = tree_name  # סוג
            row[5] = ""  # מספר בית (ביעלה זה לרוב בתוך הרחוב)
            row[6] = street  # רחוב
            row[9] = status_desc  # סטטוס (אפשרות ערעור)
            row[12] = appeal_date  # תאריך ערעור
            row[14] = license_id  # מזהה
            row[15] = pub_date  # תאריך פרסום אובייקט

            processed_results.append(row)

        return processed_results

    except Exception as e:
        print(f"שגיאה בשליפת נתוני יעלה: {e}")
        return []


if __name__ == "__main__":
    print("בודק את החיבור החדש ליעלה...")
    results = get_yeela_data()
    print(f"נמצאו {len(results)} תוצאות.")
    for r in results[:5]:
        print(f"רישיון: {r[14]} | רחוב: {r[6]} | עץ: {r[2]} | תאריך: {r[12]}")