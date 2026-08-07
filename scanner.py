#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
סורק קולות קוראים יומי — שרון | גרסה 2
זרימה: מקורות -> פריטים חדשים -> משיכת טקסט מהעמוד -> סינון איכות עם Claude
       -> מייל בוקר מעוצב + עדכון דשבורד (docs/index.html, GitHub Pages)
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import quote, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

SEEN_FILE = "seen.json"            # מזהים שכבר טופלו
DATA_FILE = "opencalls.json"       # קולות קוראים רלוונטיים פעילים (לדשבורד)
DASH_TEMPLATE = "dashboard_template.html"
DASH_OUT = "docs/index.html"
MAX_NEW_PER_SOURCE = 20
PAGE_TEXT_LIMIT = 2500             # תווים מכל עמוד שנשלחים ל-Claude
RELEVANCE_THRESHOLD = 7            # סף גבוה: רק איכותי ומקדם-קריירה
CLAUDE_MODEL = "claude-sonnet-4-6"

ARTIST_PROFILE = """
אמנית מדיה חדשה ישראלית + מומחית פורנזית מוסמכת (אודיו/וידאו/תמונה) — מיצוב ייחודי:
"האמנית כמומחית פורנזית". פרקטיקה: מיצבים טכנולוגיים אוטונומיים — רובוטיקה, סלילי
טסלה, אצות, אלקטרוניקה — תודעה וסימביוזה ביולוגית-מכנית. פורמטים: מיצב, מדיה-ארט,
ביו-ארט, lecture-performance.

אזרחות פורטוגלית/EU: זכאית מלאה לקולות קוראים המיועדים לאזרחי אירופה/EU —
אלה בעדיפות גבוהה. גם ישראל וגם בינלאומי פתוח.

מה מחפשים (בעדיפות יורדת):
1. הזדמנויות יוקרתיות שמהוות קפיצת מדרגה בקריירה: פרסים מוכרים, פסטיבלי מדיה-ארט
   מובילים (Ars Electronica, transmediale, ISEA, Sonar+D וכד'), מוזיאונים, ביאנלות,
   מענקים משמעותיים, פרסומים/כתבי עת נחשבים, תערוכות אוצרותיות במוסדות רציניים.
2. מענקי יצירה ומחקר, קרנות אירופיות (Creative Europe וכד').
3. רזידנסי — רק אם גמיש למשפחה: אפשר להגיע עם ילדים, או קצר מאוד (עד שבועיים),
   או היברידי/מרחוק. רזידנסי רגיל ללא גמישות משפחתית = לא רלוונטי.

מה לפסול (ציון 1-3):
- תערוכות ציור בלבד / מדיומים שאינם רלוונטיים לפרקטיקה.
- כל דבר מסחרי-דקורטיבי: מכירה לסלון, ירידי אמנות שיווקיים, "הזדמנות חשיפה" בתשלום,
  גלריות vanity שגובות דמי השתתפות גבוהים בלי ערך אוצרותי.
- כתבות, פרסומות, ואירועים שאינם קול קורא.
- כל דבר בלי חשיבות ממשית להתקדמות קריירה בעולם האמנות העכשווית.
"""

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenCallScanner/2.0)"}
TODAY = date.today()


# ---------------------------------------------------------------- אחסון
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def item_id(url, title):
    return hashlib.sha1(f"{url}|{title}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- איסוף
def fetch_source(src, debug=False):
    items = []
    try:
        r = requests.get(src["url"], headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [!] {src['name']}: שגיאת רשת — {e}")
        return items

    if src.get("type") == "rss":
        soup = BeautifulSoup(r.content, "xml")
        for it in soup.find_all("item"):
            t, l = it.find("title"), it.find("link")
            if t and l:
                items.append((t.get_text(strip=True), l.get_text(strip=True)))
    else:
        soup = BeautifulSoup(r.content, "html.parser")
        for a in soup.select(src.get("item_selector", "a")):
            title = a.get_text(" ", strip=True)
            href = a.get("href", "")
            if not title or not href or len(title) < 15:
                continue
            kws = src.get("keyword_prefilter")
            if kws and not any(k in title for k in kws):
                continue
            items.append((title, urljoin(src["url"], href)))

    dedup, out = set(), []
    for t, u in items:
        if u not in dedup:
            dedup.add(u)
            out.append((t, u))
    if debug:
        print(f"  --- {src['name']} ({len(out)}) ---")
        for t, u in out[:8]:
            print(f"    {t[:70]} | {u}")
    return out[:MAX_NEW_PER_SOURCE]


def fetch_page_text(url):
    """מושך את טקסט העמוד של קול קורא ספציפי, לניתוח מעמיק."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        return text[:PAGE_TEXT_LIMIT]
    except Exception:
        return ""


# ---------------------------------------------------------------- סינון Claude
def classify(new_items):
    """new_items: רשימת dict {title,url,source,page_text}. מחזיר פריטים רלוונטיים מלאים."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[!] אין ANTHROPIC_API_KEY — לא ניתן לסנן. עצירה.")
        return []

    blocks = []
    for i, it in enumerate(new_items):
        blocks.append(
            f"### פריט {i+1}\nמקור: {it['source']}\nכותרת: {it['title']}\n"
            f"לינק: {it['url']}\nתוכן העמוד (חלקי): {it['page_text'][:PAGE_TEXT_LIMIT]}\n"
        )

    prompt = f"""את עוזרת אוצרותית של אמנית. לפנייך פרופיל האמנית וקולות קוראים שנאספו היום.
נתחי כל פריט ודרגי בקפדנות. סף האיכות גבוה — עדיף לפסול מאשר להציף.

{ARTIST_PROFILE}

{chr(10).join(blocks)}

החזירי JSON בלבד (ללא markdown): מערך אובייקטים, אחד לכל פריט, עם השדות:
- index: מספר הפריט
- score: 1-10 (רלוונטיות + איכות/יוקרה + פוטנציאל קפיצת מדרגה, לפי כללי הפסילה)
- name: שם ההזדמנות בעברית (תרגמי אם צריך, שמרי שם מוסד במקור)
- category: פרס / מענק / רזידנסי / תערוכה / פסטיבל / פרסום / אחר
- description: 2-3 משפטים בעברית — מה זה ומי עומד מאחורי זה
- fit: משפט-שניים — למי זה מתאים ולמה זה רלוונטי (או לא) לפרופיל שלה
- conditions: תנאים עיקריים — זכאות, דמי הגשה, מה מקבלים, דרישות הגשה
- deadline: תאריך אחרון בפורמט YYYY-MM-DD, או "" אם לא ידוע
- family_flexible: true/false/null — רק לרזידנסי: האם גמיש למשפחה
- prestige: משפט קצר — עד כמה זה נחשב בעולם האמנות"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 8000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json()["content"])
    text = re.sub(r"```json|```", "", text).strip()
    try:
        ratings = json.loads(text)
    except json.JSONDecodeError:
        print("[!] תשובת Claude אינה JSON תקין — הפריטים יטופלו מחר שוב.")
        return []

    results = []
    for r in ratings:
        i = r.get("index", 0) - 1
        if not (0 <= i < len(new_items)):
            continue
        if r.get("score", 0) < RELEVANCE_THRESHOLD:
            continue
        src = new_items[i]
        results.append({
            "id": item_id(src["url"], src["title"]),
            "url": src["url"], "source": src["source"],
            "name": r.get("name", src["title"]),
            "category": r.get("category", "אחר"),
            "description": r.get("description", ""),
            "fit": r.get("fit", ""),
            "conditions": r.get("conditions", ""),
            "deadline": r.get("deadline", ""),
            "family_flexible": r.get("family_flexible"),
            "prestige": r.get("prestige", ""),
            "score": r.get("score"),
            "added": TODAY.isoformat(),
        })
    results.sort(key=lambda x: -x["score"])
    return results


# ---------------------------------------------------------------- תזכורות יומן
def gcal_link(title, day, details, url):
    """לינק ליצירת אירוע יום-שלם ביומן Google בלחיצה אחת."""
    d1 = day.strftime("%Y%m%d")
    d2 = (day + timedelta(days=1)).strftime("%Y%m%d")
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}&dates={d1}/{d2}"
            f"&details={quote(details + chr(10) + url)}")


def reminder_links(item):
    """מחזיר [(label, link)] — תזכורת התחלת כתיבה ותזכורת דדליין."""
    if not item.get("deadline"):
        return []
    try:
        dl = datetime.strptime(item["deadline"], "%Y-%m-%d").date()
    except ValueError:
        return []
    if dl < TODAY:
        return []
    prep_days = 28 if item["category"] in ("מענק", "פרס", "רזידנסי") else 14
    start = max(TODAY + timedelta(days=1), dl - timedelta(days=prep_days))
    final = max(TODAY + timedelta(days=1), dl - timedelta(days=3))
    links = [("⏰ להתחיל לכתוב",
              gcal_link(f"להתחיל הגשה: {item['name']}", start,
                        f"שלב 1: קריאת תנאים, טיוטת statement, איסוף תיק עבודות. "
                        f"דדליין: {item['deadline']}", item["url"]))]
    if final != start:
        links.append(("📅 בדיקה סופית והגשה",
                      gcal_link(f"הגשה סופית: {item['name']}", final,
                                f"שלב אחרון: הגהה, בדיקת דרישות טכניות, שליחה. "
                                f"דדליין: {item['deadline']}", item["url"])))
    return links


# ---------------------------------------------------------------- מייל בוקר
def build_email(results, dash_url):
    cards = []
    for it in results:
        rls = reminder_links(it)
        btns = "".join(
            f"<a href='{l}' style='display:inline-block;margin:4px 0 0 8px;padding:6px 12px;"
            f"background:#0E7C7B;color:#fff;text-decoration:none;border-radius:6px;"
            f"font-size:13px'>{lbl}</a>" for lbl, l in rls)
        dl = f"<b>דדליין:</b> {it['deadline']}<br>" if it["deadline"] else ""
        fam = ""
        if it["category"] == "רזידנסי" and it.get("family_flexible"):
            fam = "<span style='color:#0E7C7B'>👨‍👩‍👧 גמיש למשפחה</span><br>"
        cards.append(
            f"<div style='border:1px solid #E4E4E0;border-radius:10px;padding:16px;"
            f"margin:0 0 14px 0;background:#fff'>"
            f"<div style='font-size:12px;color:#888'>{it['category']} · ציון {it['score']}/10 · {it['source']}</div>"
            f"<div style='font-size:18px;font-weight:700;margin:4px 0'>{it['name']}</div>"
            f"<b>תיאור:</b> {it['description']}<br>"
            f"<b>למי מתאים:</b> {it['fit']}<br>"
            f"<b>תנאים:</b> {it['conditions']}<br>{dl}{fam}"
            f"<a href='{it['url']}' style='color:#0E7C7B'>🔗 לקול הקורא</a>{btns}</div>")
    dash = (f"<p><a href='{dash_url}' style='color:#0E7C7B;font-weight:700'>"
            f"↗ לדשבורד המלא — סימון רלוונטיות ומעקב</a></p>" if dash_url else "")
    return (f"<div style='font-family:Arial,Heebo,sans-serif;direction:rtl;text-align:right;"
            f"background:#FAFAF8;padding:20px'>"
            f"<h2 style='margin-top:0'>☀️ קולות קוראים — {TODAY.strftime('%d/%m/%Y')}</h2>"
            f"<p>{len(results)} הזדמנויות עברו את סף האיכות היום.</p>{dash}{''.join(cards)}</div>")


def send_email(html, n):
    user = os.environ.get("EMAIL_USER")
    pwd = os.environ.get("EMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO", user)
    if not (user and pwd):
        print("[!] אין פרטי מייל — הדיווח הודפס לקונסול בלבד.")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = f"🎨 {n} קולות קוראים איכותיים — {TODAY.strftime('%d/%m')}"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"[✓] מייל נשלח ({n} פריטים).")


# ---------------------------------------------------------------- דשבורד
def generate_dashboard(items):
    for it in items:
        it["reminders"] = reminder_links(it)
    with open(DASH_TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("__DATA__", json.dumps(items, ensure_ascii=False)) \
              .replace("__UPDATED__", TODAY.strftime("%d/%m/%Y"))
    os.makedirs("docs", exist_ok=True)
    with open(DASH_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[✓] דשבורד עודכן ({len(items)} פריטים פעילים).")


def prune_expired(items):
    keep = []
    for it in items:
        dl = it.get("deadline")
        if dl:
            try:
                if datetime.strptime(dl, "%Y-%m-%d").date() < TODAY - timedelta(days=2):
                    continue
            except ValueError:
                pass
        else:
            # בלי דדליין — נשאר 45 יום מרגע ההוספה
            added = datetime.strptime(it["added"], "%Y-%m-%d").date()
            if added < TODAY - timedelta(days=45):
                continue
        keep.append(it)
    return keep


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    with open("sources.yaml", encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]

    seen = set(load_json(SEEN_FILE, []))
    active = load_json(DATA_FILE, [])

    new_raw = []
    print(f"סורקת {len(sources)} מקורות...")
    for src in sources:
        for title, url in fetch_source(src, debug=args.debug):
            uid = item_id(url, title)
            if uid not in seen:
                new_raw.append({"title": title, "url": url, "source": src["name"]})
                seen.add(uid)
    print(f"{len(new_raw)} פריטים חדשים.")
    if args.debug:
        return

    if new_raw:
        print("מושכת את עמודי הפריטים לניתוח מעמיק...")
        for it in new_raw:
            it["page_text"] = fetch_page_text(it["url"])
        results = classify(new_raw)
        print(f"{len(results)} עברו את סף האיכות ({RELEVANCE_THRESHOLD}+).")
    else:
        results = []

    # עדכון מאגר פעיל + דשבורד (מתעדכן תמיד, גם כשאין חדש — בגלל דדליינים)
    known = {it["id"] for it in active}
    active += [r for r in results if r["id"] not in known]
    active = prune_expired(active)
    active.sort(key=lambda x: (x.get("deadline") or "9999", -x["score"]))
    generate_dashboard(active)

    if results:
        send_email(build_email(results, os.environ.get("DASHBOARD_URL", "")), len(results))
    else:
        print("אין פריטים חדשים שעברו סינון — לא נשלח מייל.")

    save_json(SEEN_FILE, sorted(seen))
    save_json(DATA_FILE, active)


if __name__ == "__main__":
    sys.exit(main())
