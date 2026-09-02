"""
diag_pdf.py — בדיקת אבחון לטבלת ה-PDF של עיריית חיפה.
מטרה: לראות את הטקסט הגולמי *לפני* get_display, ולאמת את אינדקסי העמודות.

הרצה:  python diag_pdf.py
פלט:   diag_pdf_raw.json  +  הדפסה לקונסולה

תלויות: requests, pdfplumber  (כבר מותקנות אצלך)
"""

import io
import json
import unicodedata

import pdfplumber
import requests

PDF_URL = (
    "https://www3.haifa.muni.il/trees/"
    "%D7%A8%D7%A9%D7%99%D7%9E%D7%AA%20%D7%91%D7%A7%D7%A9%D7%95%D7%AA.pdf"
)
OUT_JSON = "diag_pdf_raw.json"
MAX_PRINT_ROWS = 6


def bidi_report(s: str) -> str:
    """מחזיר את סדר סוגי ה-BiDi של התווים, לזיהוי כיוון האחסון."""
    kinds = []
    for ch in s:
        if ch.isspace():
            continue
        b = unicodedata.bidirectional(ch)
        kinds.append({"R": "R", "AL": "R", "L": "L", "EN": "N", "AN": "N"}.get(b, b))
    # דחיסה: RRRR NNN -> R*4 N*3
    out, prev, cnt = [], None, 0
    for k in kinds:
        if k == prev:
            cnt += 1
        else:
            if prev:
                out.append(f"{prev}*{cnt}")
            prev, cnt = k, 1
    if prev:
        out.append(f"{prev}*{cnt}")
    return " ".join(out)


def main():
    print("מוריד PDF...")
    resp = requests.get(PDF_URL, timeout=60)
    resp.raise_for_status()
    print(f"גודל: {len(resp.content)} bytes")

    pages = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        print(f"מספר עמודים: {len(pdf.pages)}")
        for pi, page in enumerate(pdf.pages):
            table = page.extract_table()
            if not table:
                pages.append({"page": pi, "table": None})
                continue
            rows = [[("" if c is None else str(c)) for c in row] for row in table]
            pages.append({"page": pi, "n_rows": len(rows), "table": rows})

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=1)
    print(f"\nנשמר: {OUT_JSON}")

    # --- הדפסה של העמוד הראשון ---
    first = next((p for p in pages if p.get("table")), None)
    if not first:
        print("לא נמצאה טבלה בכלל.")
        return

    rows = first["table"]
    print(f"\n=== עמוד {first['page']} : {len(rows)} שורות, "
          f"{max(len(r) for r in rows)} עמודות ===\n")

    for ri, row in enumerate(rows[:MAX_PRINT_ROWS]):
        print(f"--- שורה {ri} ---")
        for ci, cell in enumerate(row):
            if not cell.strip():
                continue
            flat = cell.replace("\n", "\\n")
            print(f"  [{ci:>2}] {flat!r}")
            print(f"       הפוך: {flat[::-1]!r}")
            print(f"       bidi: {bidi_report(cell)}")
        print()

    # --- חיפוש עמודות שמכילות מילות פעולה ---
    print("=== סריקת מילות פעולה לפי עמודה ===")
    keywords = ["כריתה", "העתקה", "שימור", "גיזום", "מסוכן", "העתק"]
    hits = {}
    for p in pages:
        for row in (p.get("table") or []):
            for ci, cell in enumerate(row):
                for kw in keywords:
                    if kw in str(cell) or kw[::-1] in str(cell):
                        hits.setdefault(ci, {}).setdefault(kw, 0)
                        hits[ci][kw] += 1
    if hits:
        for ci in sorted(hits):
            print(f"  עמודה [{ci}]: {hits[ci]}")
    else:
        print("  לא נמצאו מילות פעולה בשום עמודה.")


if __name__ == "__main__":
    main()
