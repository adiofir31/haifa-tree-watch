"""
diag_yeela.py — בדיקת אבחון ל-API של יעלה.
מטרה: לראות את ה-JSON הגולמי, לזהות את השדה שמבדיל בין כריתה/העתקה/שימור,
      ולבדוק אם יש פאגינציה (יותר מ-100 תוצאות).

הרצה:  python diag_yeela.py
פלט:   yeela_raw.json  +  הדפסה לקונסולה

תלויות: requests
"""

import json
from collections import defaultdict

import requests

URL = "https://yeela-trees.moag.gov.il/api/Fo/FOServiceRequest/getFOGridPublicityLicenses"
CITY_ID = 4000  # חיפה
PAGE_SIZE = 100
OUT_JSON = "yeela_raw.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "userOrgRoleId": "",
    "Origin": "https://yeela-trees.moag.gov.il",
    "Referer": "https://yeela-trees.moag.gov.il/FoPublic/FoLicence",
}


def fetch(page_number: int) -> dict:
    payload = {
        "orderDetails": None,
        "pageDetails": {"pageNumber": page_number, "pageSize": PAGE_SIZE},
        "parameters": {"cityId": CITY_ID, "appealLastDate": None},
    }
    r = requests.post(URL, json=payload, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def profile(items, label):
    """מדפיס לכל שדה: כמה פעמים הופיע ואילו ערכים ייחודיים (עד 12)."""
    vals = defaultdict(set)
    counts = defaultdict(int)
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            counts[k] += 1
            if isinstance(v, (str, int, float, bool)) or v is None:
                if len(vals[k]) < 40:
                    vals[k].add(v)
    print(f"\n=== שדות ב-{label} (מתוך {len(items)} רשומות) ===")
    for k in sorted(counts):
        uniq = vals.get(k, set())
        sample = list(uniq)[:12]
        print(f"  {k:<28} n={counts[k]:<4} uniq={len(uniq):<4} {sample}")


def main():
    print("שולף עמוד 1...")
    data = fetch(1)
    print("מפתחות ברמה העליונה:", list(data.keys()))
    for k, v in data.items():
        if not isinstance(v, (list, dict)):
            print(f"  {k} = {v!r}")

    items = data.get("result", []) or []
    print(f"\nהתקבלו {len(items)} רישיונות בעמוד 1.")

    all_pages = [data]
    if len(items) == PAGE_SIZE:
        print("העמוד מלא -> בודק עמוד 2 (סימן שיש פאגינציה שמתפספסת היום)")
        try:
            d2 = fetch(2)
            i2 = d2.get("result", []) or []
            print(f"עמוד 2: {len(i2)} רישיונות.")
            all_pages.append(d2)
            items = items + i2
        except Exception as e:
            print(f"שגיאה בעמוד 2: {e}")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_pages, f, ensure_ascii=False, indent=1)
    print(f"נשמר: {OUT_JSON}")

    # פרופיל שדות ברמת הרישיון
    profile(items, "רמת הרישיון")

    # פרופיל שדות ברמת expandRows
    expand = []
    for it in items:
        expand.extend(it.get("expandRows") or [])
    profile(expand, "expandRows")

    # התפלגות מספר השורות לרישיון
    dist = defaultdict(int)
    for it in items:
        dist[len(it.get("expandRows") or [])] += 1
    print("\n=== כמה expandRows יש לרישיון ===")
    for k in sorted(dist):
        print(f"  {k} שורות: {dist[k]} רישיונות")

    # דוגמה מלאה: רישיון עם הכי הרבה שורות
    if items:
        worst = max(items, key=lambda it: len(it.get("expandRows") or []))
        print(f"\n=== רישיון הכי מפורט (licenseId={worst.get('licenseId')}) ===")
        print(json.dumps(worst, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
