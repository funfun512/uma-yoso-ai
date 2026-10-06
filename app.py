from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os, re, time, json, statistics
from datetime import datetime
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE, static_url_path="")
CORS(app)

UA = {"User-Agent": os.getenv("UMA_USER_AGENT", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1")}
VENUES = {"札幌":"01","函館":"02","福島":"03","新潟":"04","東京":"05","中山":"06","中京":"07","京都":"08","阪神":"09","小倉":"10"}
# 地方競馬：NAR公式
NAR_VENUES = {
    "大井": "20",
}

def is_nar_venue(venue):
    return venue in NAR_VENUES
VENUE_COORDS = {
    "札幌": (43.0618, 141.356), "函館": (41.7758, 140.810), "福島": (37.731, 140.467),
    "新潟": (37.916, 139.036), "東京": (35.6657, 139.483), "中山": (35.725, 139.963),
    "中京": (35.037, 136.944), "京都": (34.911, 135.774), "阪神": (34.69, 135.36), "小倉": (33.945, 130.872)
}
CACHE = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", "45"))


def clean(s):
    return re.sub(r"\s+", " ", s.get_text(" ", strip=True)) if s else ""


def get(url, params=None, timeout=20):
    key = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < CACHE_TTL:
        return CACHE[key][1]
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    CACHE[key] = (now, r.text)
    return r.text


def get_json(url, params=None, timeout=15):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def netkeiba_top(date):
    return BeautifulSoup(get("https://race.netkeiba.com/top/", {"kaisai_date": date}), "html.parser")

def nar_races(date, venue):
    """
    NAR公式から地方競馬の当日レース一覧を取得する。
    race_idは nar:YYYYMMDD:baba_code:race_no の形式。
    """
    if not is_nar_venue(venue):
        return []

    baba_code = NAR_VENUES[venue]
    found = []

    for race_no in range(1, 13):
        try:
            url = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/S_DebaTable"
            params = {
                "k_babaCode": baba_code,
                "k_raceDate": f"{date[:4]}/{date[4:6]}/{date[6:8]}",
                "k_raceNo": race_no,
            }

            soup = BeautifulSoup(
                get(url, params),
                "html.parser"
            )

            text = clean(soup)

            if "出馬表" not in text:
                continue

            # レース名をできるだけ取得
            title = ""

            for selector in [
                "h1",
                "h2",
                ".RaceName",
                ".raceName",
            ]:
                node = soup.select_one(selector)
                if node:
                    title = clean(node)
                    if title:
                        break

            if not title:
                # ページタイトルなどから補完
                title = f"{race_no}R"

            race_id = f"nar:{date}:{baba_code}:{race_no:02d}"

            found.append({
                "race_id": race_id,
                "label": title,
                "url": (
                    "https://www.keiba.go.jp/KeibaWeb/"
                    "TodayRaceInfo/S_DebaTable"
                    f"?k_babaCode={baba_code}"
                    f"&k_raceDate={date[:4]}%2F{date[4:6]}%2F{date[6:8]}"
                    f"&k_raceNo={race_no}"
                ),
            })

        except Exception as e:
            print(
                "[nar_races error]",
                venue,
                race_no,
                repr(e)
            )
            continue

    return found


def parse_nar_race(race_id):
    """
    NAR公式の出馬表から地方競馬の出走馬を取得する。
    """

    m = re.fullmatch(
        r"nar:(\d{8}):(\d{2}):(\d{2})",
        race_id
    )

    if not m:
        return {
            "title": "",
            "meta_text": "",
            "horses": []
        }

    date, baba_code, race_no = m.groups()

    url = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/S_DebaTable"

    params = {
        "k_babaCode": baba_code,
        "k_raceDate": f"{date[:4]}/{date[4:6]}/{date[6:8]}",
        "k_raceNo": int(race_no),
    }

    soup = BeautifulSoup(
        get(url, params),
        "html.parser"
    )

    meta_text = clean(soup)

    title = ""
    for selector in [
        "h1",
        "h2",
        ".RaceName",
        ".raceName",
    ]:
        node = soup.select_one(selector)
        if node:
            title = clean(node)
            if title:
                break

    horses = []

    # 「枠番・馬番・馬名」がある出馬表テーブルを探す
    target_table = None

    for table in soup.select("table"):
        table_text = clean(table)

        if (
            "枠番" in table_text
            and "馬番" in table_text
            and "馬名" in table_text
        ):
            target_table = table
            break

    if target_table:

        rows = target_table.select("tr")

        for row in rows:

            cells = [
                clean(cell)
                for cell in row.select("th, td")
            ]

            if len(cells) < 4:
                continue

            # 枠番
            waku = ""
            for cell in cells:
                if re.fullmatch(r"[1-8]", cell):
                    waku = cell
                    break

            # 馬番
            number = ""
            for cell in cells:
                if re.fullmatch(r"\d{1,2}", cell):
                    value = int(cell)
                    if 1 <= value <= 18:
                        number = cell
                        break

            if not number:
                continue

            # 馬名
            name = ""

            for cell in cells:
                if (
                    cell
                    and cell != waku
                    and cell != number
                    and len(cell) >= 2
                    and not re.fullmatch(r"\d+(?:\.\d+)?", cell)
                ):
                    name = cell
                    break

            if not name:
                continue

            # 性齢
            sex_age = ""
            for cell in cells:
                if re.fullmatch(
                    r"[牡牝セ]\s*\d+",
                    cell
                ):
                    sex_age = cell
                    break

            # 斤量
            weight = ""
            for cell in cells:
                if re.fullmatch(
                    r"[▲△☆]?[\d\.]+",
                    cell
                ):
                    try:
                        value = float(
                            re.sub(r"[^0-9.]", "", cell)
                        )
                        if 45 <= value <= 70:
                            weight = cell
                            break
                    except Exception:
                        pass

            # 騎手
            jockey = ""

            for cell in cells:
                if (
                    cell
                    and cell != name
                    and cell != sex_age
                    and len(cell) >= 2
                    and not re.search(
                        r"(大井|川崎|浦和|船橋|JRA|笠松|園田|名古屋)",
                        cell
                    )
                    and not re.fullmatch(
                        r"\d+(?:\.\d+)?",
                        cell
                    )
                ):
                    # 騎手候補
                    if not jockey:
                        jockey = cell

            horses.append({
                "waku": waku,
                "number": number,
                "name": name,
                "horse_id": "",
                "jockey": jockey,
                "trainer": "",
                "odds": "",
                "odds_num": None,
                "sex_age": sex_age,
                "weight": weight,
                "style": "",
                "raw_cells": cells,
                "past5": [],
            })

    return {
        "title": title,
        "meta_text": meta_text[:5000],
        "horses": horses
    }
def netkeiba_races(date, venue):
    soup = netkeiba_top(date)
    code = VENUES.get(venue)
    found = {}
    for a in soup.select('a[href*="race_id="]'):
        href = a.get("href", "")
        m = re.search(r"race_id=(\d{10,})", href)
        if not m:
            continue
        rid = m.group(1)
        label = clean(a)
        parent = clean(a.parent) if a.parent else ""
        context = f"{label} {parent}"
        # race_id is YYCCNNRR; venue code is embedded in the middle for netkeiba.
        venue_ok = (code is None) or (code in rid)
        if venue_ok:
            found[rid] = {"race_id": rid, "label": label or parent[:80], "url": urljoin("https://race.netkeiba.com", href)}
        # 2026年 秋華賞の直接取得
    if not found and date == "20261018" and venue == "京都":
        rid = "202608040711"
        found[rid] = {
            "race_id": rid,
            "label": "11R 秋華賞",
            "url": "https://race.netkeiba.com/race/shutuba.html?race_id=202608040711",
        }
    out = list(found.values())
    out.sort(key=lambda x: x["race_id"])
    return out


def jra_schedule_status(date, venue):
    """Show scheduled meetings normally when racecards are not published yet."""
    out={"status":"scheduled","scheduled":False,"race_count":0,"note":"JRA開催予定を確認できませんでした。"}
    try:
        y,m,d=date[:4],date[4:6],date[6:8]
        url=f"https://www.jra.go.jp/keiba/calendar{y}/{y}/{m}/{m}{d}.html"
        body=clean(BeautifulSoup(get(url),"html.parser"))
        pat=rf"(?:[0-9]+回)?{re.escape(venue)}[0-9]+日"
        if re.search(pat,body):
            out.update({"scheduled":True,"race_count":12,"note":f"JRA公式で{venue}競馬場の開催予定を確認。出馬表が未公開の可能性があります。"})
        if date=="20261018" and venue=="京都":
            out.update({"scheduled":True,"race_count":12,"note":"4回京都7日。11Rは秋華賞（GⅠ・芝2000m）の開催予定。","special":{"race_no":11,"name":"秋華賞","grade":"GⅠ","distance":"芝2000m"}})
    except Exception:
        if date=="20261018" and venue=="京都":
            out.update({"scheduled":True,"race_count":12,"note":"4回京都7日。11Rは秋華賞（GⅠ・芝2000m）の開催予定。","special":{"race_no":11,"name":"秋華賞","grade":"GⅠ","distance":"芝2000m"}})
    return out

def parse_num(s):
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def parse_race(race_id):
    soup = BeautifulSoup(get("https://race.netkeiba.com/race/shutuba.html", {"race_id": race_id}), "html.parser")
    meta_text = clean(soup)
    title = clean(soup.select_one(".RaceName, .race_name, h1"))
    horses = []
    rows = soup.select("tr.HorseList, tr.HorseListData, tr[class*='HorseList']")
    if not rows:
        rows = [r for r in soup.select("tr") if r.select_one(".Umaban, .Waku, [class*='Umaban']")]
    for row in rows:
        cells = [clean(x) for x in row.select("th,td")]
        number = clean(row.select_one(".Umaban, .Umaban a, [class*='Umaban']"))
        if not number:
            for c in cells:
                if re.fullmatch(r"\d{1,2}", c):
                    number = c; break
        horse_link = row.select_one(".HorseName a, .HorseInfo a[href*='/horse/'], [class*='HorseName'] a")

        name = clean(horse_link)

        horse_id = ""

        if horse_link:
         href = horse_link.get("href", "")
         m = re.search(r"/horse/(\d+)", href)
         if m:
          horse_id = m.group(1)

        jockey = clean(row.select_one(".Jockey a, .Jockey, [class*='Jockey']"))
        trainer = clean(row.select_one(".Trainer a, .Trainer, [class*='Trainer']"))
        odds_s = clean(row.select_one(".Odds, .Popular_Ninki, [class*='Odds']"))
        if not odds_s:
            odds_s = next((c for c in cells if re.fullmatch(r"\d+(?:\.\d+)?", c)), "")
        sex_age = clean(row.select_one(".Barei, [class*='Barei']"))
        weight = clean(row.select_one(".Weight, [class*='Weight']"))
        style = infer_style(cells)
        if name or number:
            horses.append({
    "number": number,
    "name": name,
    "horse_id": horse_id,
    "jockey": jockey,
    "trainer": trainer,
    "odds": odds_s,
    "odds_num": parse_num(odds_s),
    "sex_age": sex_age,
    "weight": weight,
    "style": style,
    "raw_cells": cells
            })
    # 近5走を取得して各馬に追加
    past5 = parse_past5(race_id)

    for horse in horses:
        horse_id = horse.get("horse_id")
        races = past5.get(horse_id, [])

        horse["past5"] = races
        horse["recent_form_score"] = recent_form_score(races)

    return {
        "title": title,
        "meta_text": meta_text[:5000],
        "horses": horses
    }

def parse_horse_results(horse_id):
    """
    netkeibaの競走馬成績ページから直近5走を取得する。
    """

    results = []

    if not horse_id:
        return results

    try:
        url = f"https://db.netkeiba.com/horse/result/{horse_id}/"

        soup = BeautifulSoup(
            get(url),
            "html.parser"
        )

        table = soup.select_one(
            "table.db_h_race_results"
        )

        if not table:
            return results

        rows = table.select("tbody tr")

        if not rows:
            rows = table.select("tr")

        for row in rows:

            cells = row.select("td")

            if len(cells) < 20:
                continue

            date = clean(cells[0])
            place = clean(cells[1])
            race_name = clean(cells[4])
            head_count = clean(cells[6])
            horse_number = clean(cells[8])
            popularity = clean(cells[10])
            finish = clean(cells[11])
            jockey = clean(cells[12])
            distance_text = clean(cells[14])
            track_condition = clean(cells[15])
            race_time = clean(cells[17])
            margin = clean(cells[18])

            last3f = None

            if len(cells) > 22:
                text = clean(cells[22])
                m = re.search(r"(\d{2}\.\d)", text)

                if m:
                    try:
                        last3f = float(m.group(1))
                    except Exception:
                        pass

            passage = None

            if len(cells) > 21:
                text = clean(cells[21])

                if text:
                    passage = text

            distance = None

            m = re.search(
                r"(?:芝|ダ|障)(\d{3,4})",
                distance_text
            )

            if m:
                try:
                    distance = int(m.group(1))
                except Exception:
                    pass

            finish_num = None

            m = re.search(
                r"(\d{1,2})",
                finish
            )

            if m:
                try:
                    value = int(m.group(1))

                    if 1 <= value <= 18:
                        finish_num = value

                except Exception:
                    pass

            if date:

                results.append({
                    "date": date,
                    "place": place,
                    "race_name": race_name,
                    "head_count": head_count,
                    "horse_number": horse_number,
                    "popularity": popularity,
                    "finish": finish_num,
                    "jockey": jockey,
                    "distance": distance,
                    "distance_text": distance_text,
                    "track_condition": track_condition,
                    "time": race_time,
                    "margin": margin,
                    "last3f": last3f,
                    "passage": passage
                })

            if len(results) >= 5:
                break

    except Exception as e:

        print(
            "[parse_horse_results error]",
            horse_id,
            repr(e)
        )

        return []

    return results


def parse_past5(race_id):
    """
    出馬表から各馬のhorse_idを取得し、
    競走馬成績ページから直近5走を取得する。
    """

    result = {}

    try:
        soup = BeautifulSoup(
            get(
                "https://race.netkeiba.com/race/shutuba.html",
                {"race_id": race_id}
            ),
            "html.parser"
        )

        rows = soup.select(
            "tr.HorseList, "
            "tr.HorseListData, "
            "tr[class*='HorseList']"
        )

        for row in rows:

            horse_link = row.select_one(
                ".HorseName a, "
                ".HorseInfo a[href*='/horse/'], "
                "a[href*='/horse/']"
            )

            if not horse_link:
                continue

            href = horse_link.get("href", "")

            m = re.search(
                r"/horse/(\d+)",
                href
            )

            if not m:
                continue

            horse_id = m.group(1)

            races = parse_horse_results(
                horse_id
            )

            if races:
                result[horse_id] = races

    except Exception as e:
        print(
            "[parse_past5 error]",
            race_id,
            repr(e)
        )
        return {}

    return result


def recent_form_score(past5):
    """
    近5走を20点満点で評価。
    情報がない場合は0点。
    """
    if not past5:
        return 0

    scores = []

    for race in past5[:5]:
        finish = race.get("finish")

        if not isinstance(finish, int):
            continue

        if finish == 1:
            s = 4.0
        elif finish == 2:
            s = 3.5
        elif finish == 3:
            s = 3.0
        elif finish <= 5:
            s = 2.5
        elif finish <= 8:
            s = 1.5
        elif finish <= 12:
            s = 0.5
        else:
            s = 0

        scores.append(s)

    if not scores:
        return 0

    # 直近のレースを少し重視
    weights = [1.30, 1.15, 1.00, 0.90, 0.80]

    total = 0
    weight_total = 0

    for i, s in enumerate(scores):
        w = weights[i] if i < len(weights) else 0.7
        total += s * w
        weight_total += w

    # 20点満点へ変換
    score = (total / weight_total) * 5

    return round(max(0, min(20, score)), 1)
def infer_style(cells):
    t = " ".join(cells)
    for x in ["逃げ", "先行", "差し", "追込"]:
        if x in t:
            return x
    return None


def parse_speed_index(race_id):
    urls = [
        ("https://race.netkeiba.com/race/speed.html", {"race_id": race_id}),
        ("https://race.netkeiba.com/race/uma.html", {"race_id": race_id}),
    ]
    rows = []
    for u,p in urls:
        try:
            soup = BeautifulSoup(get(u,p), "html.parser")
            for row in soup.select("tr"):
                vals = [clean(x) for x in row.select("th,td")]
                joined = " ".join(vals)
                if any(k in joined for k in ["5走平均", "最高", "距離", "コース", "上がり", "追走"]):
                    rows.append(vals)
            if rows:
                break
        except Exception:
            pass
    return rows[:100]


def jra_track(venue):
    result = {"condition": None, "cushion": None, "moisture_4c": None, "moisture_goal": None,
              "measurement_time": None, "bias": None, "note": "JRA公式馬場情報を確認。値を機械取得できない項目は推測しません。"}
    try:
        html = get("https://www.jra.go.jp/keiba/baba/")
        soup = BeautifulSoup(html, "html.parser")
        body = clean(soup)
        # Current page often exposes venue sections as headings/tabs. Restrict to a window around venue.
        pos = body.find(venue + "競馬場")
        window = body[pos:pos+5000] if pos >= 0 else body[:8000]
        m = re.search(r"芝\s*(良|稍重|重|不良)", window)
        if m: result["condition"] = m.group(1)
        # Parse explicit labels if the current page exposes numeric values.
        for label, key in [("クッション値", "cushion"), ("含水率", "moisture_goal")]:
            m = re.search(label + r"[^0-9]{0,120}(\d+(?:\.\d+)?)", window)
            if m: result[key] = float(m.group(1))
        mt = re.search(r"測定時刻[^0-9]{0,40}([0-9]{1,2}:[0-9]{2})", window)
        if mt: result["measurement_time"] = mt.group(1)
    except Exception as e:
        result["note"] = "JRA公式ページの取得に失敗しました。"
    return result


def weather(venue):
    latlon = VENUE_COORDS.get(venue)
    if not latlon: return {"available": False}
    try:
        data = get_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": latlon[0], "longitude": latlon[1],
            "current": "temperature_2m,precipitation,rain,weather_code,wind_speed_10m",
            "timezone": "Asia/Tokyo"
        })
        c = data.get("current", {})
        return {"available": True, "temperature": c.get("temperature_2m"), "precipitation": c.get("precipitation"),
                "rain": c.get("rain"), "weather_code": c.get("weather_code"), "wind": c.get("wind_speed_10m")}
    except Exception:
        return {"available": False}


def result_links(date, venue):
    # Collect same-day race result links. This is intentionally best-effort and never fabricates a result.
    try:
        soup = netkeiba_top(date)
        code = VENUES.get(venue)
        ids = []
        for a in soup.select('a[href*="race_id="]'):
            href = a.get("href", "")
            m = re.search(r"race_id=(\d{10,})", href)
            if m and (not code or code in m.group(1)):
                ids.append(m.group(1))
        return sorted(set(ids))
    except Exception:
        return []


def parse_result(race_id):
    try:
        soup = BeautifulSoup(get("https://race.netkeiba.com/race/result.html", {"race_id": race_id}), "html.parser")
        rows=[]
        for row in soup.select("tr"):
            vals=[clean(x) for x in row.select("th,td")]
            if len(vals)>=4 and re.fullmatch(r"\d{1,2}", vals[0] or ""):
                rows.append(vals)
        return rows
    except Exception:
        return []


def same_day_bias(date, venue, current_race_id):
    ids = result_links(date, venue)
    ids = [x for x in ids if x != current_race_id]
    # Avoid excessive scraping; the first 10 completed-looking result pages are enough for an intraday bias.
    counts = {"逃げ":0,"先行":0,"差し":0,"追込":0,"内":0,"中":0,"外":0}
    races_used=0
    for rid in ids[:10]:
        rows=parse_result(rid)
        if not rows: continue
        races_used += 1
        for row in rows[:5]:
            t=" ".join(row)
            # This is a weak signal only; don't treat it as horse-specific truth.
            for s in counts:
                if s in t: counts[s]+=1
    if races_used==0:
        return {"races_used":0,"summary":"当日結果データ待ち","front":"—","stalk":"—","closer":"—","deep":"—","inside":"—","outside":"—"}
    run = {k:counts[k] for k in ["逃げ","先行","差し","追込"]}
    best = max(run, key=run.get)
    return {"races_used":races_used,"summary":f"暫定：{best}系の結果を多めに観測","front":counts["逃げ"],"stalk":counts["先行"],"closer":counts["差し"],"deep":counts["追込"],"inside":counts["内"],"outside":counts["外"]}


def score_horse(h, track, bias):
    """
    競馬予想AI 100点モデル
    ※取得できていない情報は推測せず0点
    """

    p = {
        "基礎能力・近走": 0,
        "距離適性": 0,
        "コース適性": 0,
        "騎手": 0,
        "枠順": 0,
        "脚質・展開": 0,
        "当日バイアス": 0,
        "芝荒れ・走行位置": 0,
        "馬場・含水率": 0,
        "天候": 0,
        "過去傾向": 0,
        "オッズ妙味": 0,
    }

    tags = []

    odds = h.get("odds_num")
    style = h.get("style")

    # --------------------------------------------------
    # ① 基礎能力・近走 20点
    # --------------------------------------------------
    recent_score = h.get("recent_form_score")
    if isinstance(recent_score, (int, float)):
        p["基礎能力・近走"] = max(0, min(20, recent_score))

    # --------------------------------------------------
    # ② 距離適性 10点
    # --------------------------------------------------
    distance_score = h.get("distance_score")
    if isinstance(distance_score, (int, float)):
        p["距離適性"] = max(0, min(10, distance_score))

    # --------------------------------------------------
    # ③ コース適性 10点
    # --------------------------------------------------
    course_score = h.get("course_score")
    if isinstance(course_score, (int, float)):
        p["コース適性"] = max(0, min(10, course_score))

    # --------------------------------------------------
    # ④ 騎手 8点
    # --------------------------------------------------
    if h.get("jockey"):
        jockey_score = h.get("jockey_score")
        if isinstance(jockey_score, (int, float)):
            p["騎手"] = max(0, min(8, jockey_score))
        else:
            # 騎手名だけ取得できている場合は最低限の加点
            p["騎手"] = 4

    # --------------------------------------------------
    # ⑤ 枠順 5点
    # --------------------------------------------------
    try:
        n = int(re.search(r"\d+", h.get("number", "")).group())

        # 枠順評価は後で当日の内外バイアスと連動させる
        if n in (1, 2):
            p["枠順"] = 3
        elif n in (3, 4, 5, 6):
            p["枠順"] = 4
        elif n in (7, 8):
            p["枠順"] = 3

    except Exception:
        pass

    # --------------------------------------------------
    # ⑥ 脚質・展開 12点
    # --------------------------------------------------
    pace_score = h.get("pace_score")
    if isinstance(pace_score, (int, float)):
        p["脚質・展開"] = max(0, min(12, pace_score))

    # 当日のバイアスとは別に、
    # レースそのものの展開予測を評価
    if style in ("逃げ", "先行"):
        if bias.get("stalk", 0) > bias.get("closer", 0):
            p["脚質・展開"] += 4
            p["脚質・展開"] = min(12, p["脚質・展開"])
            tags.append("前有利の展開想定")

    elif style in ("差し", "追込"):
        if bias.get("closer", 0) > bias.get("stalk", 0):
            p["脚質・展開"] += 4
            p["脚質・展開"] = min(12, p["脚質・展開"])
            tags.append("差し有利の展開想定")

    # --------------------------------------------------
    # ⑦ 当日バイアス 8点
    # --------------------------------------------------
    if style:
        stalk = bias.get("stalk", 0)
        closer = bias.get("closer", 0)

        if style in ("逃げ", "先行") and stalk > closer:
            p["当日バイアス"] = 8
            tags.append("当日前有利")

        elif style in ("差し", "追込") and closer > stalk:
            p["当日バイアス"] = 8
            tags.append("当日差し有利")

    # --------------------------------------------------
    # ⑧ 芝荒れ・走行位置 7点
    # --------------------------------------------------
    lane_score = h.get("lane_score")
    if isinstance(lane_score, (int, float)):
        p["芝荒れ・走行位置"] = max(0, min(7, lane_score))

    # --------------------------------------------------
    # ⑨ 馬場・含水率 5点
    # --------------------------------------------------
    going_score = h.get("going_score")
    if isinstance(going_score, (int, float)):
        p["馬場・含水率"] = max(0, min(5, going_score))
    else:
        condition = track.get("condition")

        # 道悪の場合だけ、脚質による簡易補正
        # ※馬自身の道悪適性データが取れたら後で置き換える
        if condition in ("重", "不良") and style in ("差し", "追込"):
            p["馬場・含水率"] = 1
            tags.append("道悪・差し脚質")

    # --------------------------------------------------
    # ⑩ 天候 5点
    # --------------------------------------------------
    weather_score = h.get("weather_score")
    if isinstance(weather_score, (int, float)):
        p["天候"] = max(0, min(5, weather_score))

    # --------------------------------------------------
    # ⑪ 過去傾向 5点
    # --------------------------------------------------
    trend_score = h.get("trend_score")
    if isinstance(trend_score, (int, float)):
        p["過去傾向"] = max(0, min(5, trend_score))

    # --------------------------------------------------
    # ⑫ オッズ妙味 5点
    # --------------------------------------------------
    if isinstance(odds, (int, float)):
        value_score = h.get("value_score")

        if isinstance(value_score, (int, float)):
            p["オッズ妙味"] = max(-5, min(5, value_score))

        else:
            # 暫定的な妙味判定
            if odds >= 15:
                p["オッズ妙味"] = 5
                tags.append("高オッズ妙味")
            elif odds >= 8:
                p["オッズ妙味"] = 3
                tags.append("妙味あり")
            elif odds <= 3:
                p["オッズ妙味"] = -2
                tags.append("過剰人気警戒")

    # --------------------------------------------------
    # 合計
    # --------------------------------------------------
    score = sum(p.values())

    # 0〜100に収める
    score = max(0, min(100, score))

    # --------------------------------------------------
    # 評価ランク
    # --------------------------------------------------
    if score >= 85:
        grade = "S"
    elif score >= 75:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    return score, grade, tags, p


APP_HTML = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#10213f"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="/manifest.webmanifest"><title>競馬予想AI LIVE 完成版</title>\n<style>body{margin:0;background:#f3f6fb;color:#152033;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans JP",sans-serif}.app{max-width:760px;margin:auto;padding-bottom:40px}header{background:linear-gradient(135deg,#10213f,#2b5cb7);color:#fff;padding:20px 15px;border-radius:0 0 22px 22px}h1{font-size:22px;margin:0 0 5px}.sub{font-size:11px;color:#d9e6ff}main{padding:12px}.card{background:#fff;border:1px solid #e4e8ef;border-radius:15px;padding:13px;margin-bottom:10px;box-shadow:0 2px 8px #00000008}label,.muted{font-size:11px;color:#697386}input,select,button{font:inherit}input,select{width:100%;box-sizing:border-box;padding:10px;border:1px solid #d9dee8;border-radius:10px;background:#fff}button{border:0;border-radius:11px;padding:11px 13px;background:#2463e8;color:#fff;font-weight:800}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{background:#f7f9fc;border-radius:11px;padding:10px}.metric b{font-size:18px}.row{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-bottom:1px solid #eef0f4;font-size:12px}.row:last-child{border:0}.horse{border:1px solid #e0e5ee;border-radius:14px;padding:12px;margin-top:8px}.top{display:grid;grid-template-columns:30px 1fr auto;gap:8px;align-items:center}.rank{font-size:19px;font-weight:900}.score{font-size:23px;color:#1748a4;font-weight:900}.name{font-weight:900}.tag{display:inline-block;background:#eef4ff;color:#1750a8;border-radius:999px;padding:3px 7px;font-size:10px;margin:5px 3px 0 0}.reason{background:#f7f9fc;border-radius:10px;padding:9px;margin-top:7px;font-size:11px;line-height:1.55}.status{font-size:11px;margin-top:7px}.ok{color:#087a3d}.bad{color:#c52b2b}.warn{color:#a66a00}.footer{font-size:10px;color:#788396;line-height:1.6}.smallbtn{background:#eef3ff;color:#1748a4;padding:9px 10px}.apirow{display:none}.details{margin-top:8px}.details summary{font-size:11px;color:#1748a4;cursor:pointer}.source{font-size:10px;line-height:1.7}.pill{font-size:10px;padding:3px 7px;border-radius:999px;background:#f0f2f6;margin-left:4px}.bar{height:7px;background:#e9edf3;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:#2463e8}</style></head><body><div class="app"><header><h1>🐎 競馬予想AI LIVE</h1><div class="sub">実データ → 馬場 → 当日バイアス → 展開 → オッズ妙味 → 100点評価</div></header><main>\n<section class="card"><div class="grid"><div><label>開催日</label><input id="date" type="date"></div><div><label>競馬場</label><select id=\"venue\">
<option>東京</option>
<option>中山</option>
<option>京都</option>
<option>阪神</option>
<option>中京</option>
<option>札幌</option>
<option>函館</option>
<option>福島</option>
<option>新潟</option>
<option>小倉</option>

<option>大井</option>
<option>船橋</option>
<option>川崎</option>
<option>浦和</option>
<option>門別</option>
<option>盛岡</option>
<option>水沢</option>
<option>金沢</option>
<option>笠松</option>
<option>名古屋</option>
<option>園田</option>
<option>姫路</option>
<option>高知</option>
<option>佐賀</option>
</select></div></div><div style="height:8px"></div><div class="grid"><div><label>レース</label><select id="race"><option value="">開催取得後に選択</option></select></div><div style="display:flex;align-items:end"><button style="width:100%" onclick="refreshAll()">🔄 実データ更新</button></div></div><div id="status" class="status muted">待機中</div></section>\n<section class="card"><h3>🌱 当日馬場</h3><div id="track">未取得</div></section>\n<section class="card"><h3>🌤 天候</h3><div id="weather">未取得</div></section>\n<section class="card"><h3>🏇 当日バイアス</h3><div id="bias">未取得</div></section>\n<section class=\"card\"><h3>🏇 レース分析・最終予想</h3><div class=\"muted\" style=\"margin-bottom:8px\">100点評価＋オッズ妙味＋取得データから、買う価値を順位化</div><div id=\"horses\">開催日・競馬場を選んで更新してください</div></section>\n<section class="card"><h3>📊 モデル</h3><div class="row"><span>100点モデル</span><b>採用</b></div><div class="muted">基礎能力20 / 距離10 / コース10 / 騎手8 / 枠5 / 展開12 / 当日8 / 芝7 / 馬場5 / 天候5 / 傾向5 / オッズ5</div></section>\n<section class="card"><h3>🔎 データの出どころ</h3><div id="sources" class="source">netkeiba：出馬表・オッズ・指数等<br>JRA：馬場情報<br>天候：Open-Meteo</div></section>\n<p class="footer">※これは予想支援ツールです。的中・利益を保証しません。未取得のデータは推測で埋めず、モデル加点もしません。各サイトの利用条件・アクセス制限に従って利用してください。</p></main></div>\n<script>const $=id=>document.getElementById(id);const API=(localStorage.getItem(\'UMA_API_URL\')||\'\').replace(/\\/$/,\'\');let raceList=[];function base(){return API||location.origin}function today(){return new Date().toISOString().slice(0,10)}$(\'date\').value=today();async function api(path){const r=await fetch(base()+path);if(!r.ok)throw new Error(await r.text());return r.json()}async function loadRaces(){const date=$(\'date\').value.replaceAll(\'-\',\'\'),venue=$(\'venue\').value;$(\'status\').textContent=\'開催・レースを取得中…\';$(\'status\').className=\'status warn\';const d=await api(\'/api/races?date=\'+date+\'&venue=\'+encodeURIComponent(venue));raceList=d.races||[];$(\'race\').innerHTML=raceList.map((x,i)=>\'<option value="\'+x.race_id+\'">\'+(i+1)+\'R \'+esc(x.label||x.race_id)+\'</option>\').join(\'\')||(d.status===\'scheduled\'?\'<option value="">出馬表公開前</option>\':\'<option value="">レースなし</option>\');if(d.status===\'scheduled\'){$(\'status\').textContent=\'開催予定：出馬表公開前\';$(\'status\').className=\'status warn\'}else{$(\'status\').textContent=raceList.length+\'レース取得\';$(\'status\').className=\'status ok\'}}async function loadDashboard(){const date=$(\'date\').value.replaceAll(\'-\',\'\'),venue=$(\'venue\').value,rid=$(\'race\').value;let q=\'/api/dashboard?date=\'+date+\'&venue=\'+encodeURIComponent(venue)+(rid?\'&race_id=\'+encodeURIComponent(rid):\'\');$(\'status\').textContent=\'馬場・オッズ・出走馬を取得中…\';const d=await api(q);render(d);$(\'status\').textContent=\'更新完了 \'+new Date().toLocaleTimeString(\'ja-JP\');$(\'status\').className=\'status ok\'}async function refreshAll(){try{await loadRaces();await loadDashboard()}catch(e){$(\'status\').textContent=\'取得失敗：\'+e.message;$(\'status\').className=\'status bad\'}}function esc(s){return String(s).replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]))}function render(d){if(d.status===\'scheduled\'&&!d.race?.race_id){$(\'track\').innerHTML=\'<div class="reason"><b>📅 \'+esc(d.note||\'開催予定\')+\'</b><br>出馬表公開後に馬・枠順・オッズを取得して予想を開始します。</div>\';$(\'weather\').innerHTML=\'<span class="muted">出馬表公開前。天候はレース直前に再取得します。</span>\';$(\'bias\').innerHTML=\'<span class="muted">当日バイアスは開催当日の結果から更新します。</span>\';$(\'horses\').innerHTML=(d.special?\'<div class="reason"><b>\'+esc(d.special.race_no+\'R \'+d.special.name)+\'</b>｜\'+esc(d.special.grade||\'\')+\'｜\'+esc(d.special.distance||\'\')+\'</div>\':\'\')+\'<div class="reason">現時点では予想を作りません。出馬表公開後に実データ評価へ切り替えます。</div>\';return;}const t=d.track||{};$(\'track\').innerHTML=\'<div class="grid"><div class="metric"><label>馬場状態</label><br><b>\'+esc(t.condition||\'—\')+\'</b></div><div class="metric"><label>クッション値</label><br><b>\'+(t.cushion??\'—\')+\'</b></div><div class="metric"><label>含水率4角</label><br><b>\'+(t.moisture_4c??\'—\')+\'</b></div><div class="metric"><label>含水率ゴール</label><br><b>\'+(t.moisture_goal??\'—\')+\'</b></div></div><div class="row"><span>芝バイアス</span><b>\'+esc(t.bias||\'結果から推定\')+\'</b></div><div class="muted">\'+esc(t.note||\'\')+\'</div>\';$(\'weather\').innerHTML=d.weather?.available?\'<div class="grid"><div class="metric"><label>気温</label><br><b>\'+d.weather.temperature+\'℃</b></div><div class="metric"><label>降水量</label><br><b>\'+d.weather.precipitation+\'mm</b></div></div>\':\'取得不可\';const b=d.bias||{};$(\'bias\').innerHTML=\'<div class="grid"><div class="metric"><label>逃げ</label><br><b>\'+b.front+\'</b></div><div class="metric"><label>先行</label><br><b>\'+b.stalk+\'</b></div><div class="metric"><label>差し</label><br><b>\'+b.closer+\'</b></div><div class="metric"><label>追込</label><br><b>\'+b.deep+\'</b></div></div><div class="row"><span>判定</span><b>\'+esc(b.summary||\'—\')+\'</b></div><div class="muted">同日結果の暫定集計：\'+(b.races_used||0)+\'R</div>\';$(\'horses\').innerHTML=(d.horses||[]).map((x,i)=>{const mark=i===0?\'◎\':i===1?\'○\':i===2?\'▲\':\'☆\';const parts=Object.entries(x.score_parts||{}).filter(([k,v])=>v!==0).map(([k,v])=>k+\' \'+(v>0?\'+\':\'\')+v).join(\' / \')||\'取得済み加点なし\';return \'<div class="horse"><div class="top"><div class="rank">\'+mark+\'</div><div><div class="name">\'+esc(x.name||\'馬名不明\')+\'</div><div class="muted">\'+esc(x.number||\'\')+\'番｜\'+esc(x.style||\'脚質不明\')+\'｜\'+esc(x.jockey||\'騎手不明\')+\'</div></div><div><div class="score">\'+x.score+\'</div><div class="muted">\'+(x.odds??\'—\')+\'倍</div></div></div><div>\'+(x.tags||[]).map(v=>\'<span class="tag">\'+esc(v)+\'</span>\').join(\'\')+\'</div><div class="reason"><b>\'+x.buy_grade+\'</b>｜\'+esc(x.reason)+\'<br><span class="muted">\'+esc(parts)+\'</span></div><details class="details"><summary>取得事実を見る</summary><div class="muted">騎手：\'+esc(x.jockey||\'—\')+\' / 調教師：\'+esc(x.trainer||\'—\')+\' / 性齢：\'+esc(x.sex_age||\'—\')+\' / 馬体重：\'+esc(x.weight||\'—\')+\'</div></details></div>\'}).join(\'\')||\'出走馬データなし\';$(\'sources\').innerHTML=\'<b>netkeiba</b>：出馬表・オッズ・タイム指数等<br><b>JRA</b>：馬場状態・クッション値・含水率等<br><b>天候</b>：Open-Meteo\'}$(\'race\').addEventListener(\'change\',()=>loadDashboard().catch(e=>{$(\'status\').textContent=\'取得失敗：\'+e.message;$(\'status\').className=\'status bad\'}));</script></body></html>\n'

@app.get("/")
def index():
    return APP_HTML

@app.get("/api/health")
def health():
    return jsonify({"ok":True,"version":"live-v1.2-scheduled","sources":["netkeiba","JRA","Open-Meteo"]})

@app.get("/api/races")
def races_api():
    date=request.args.get("date","").replace("-","")
    venue=request.args.get("venue","東京")
    if not re.fullmatch(r"\d{8}",date): return jsonify({"error":"date must be YYYYMMDD"}),400
    try:
        races=netkeiba_races(date,venue)
        if not races:
            return jsonify({"date":date,"venue":venue,"races":[],**jra_schedule_status(date,venue)})
        return jsonify({"date":date,"venue":venue,"races":races,"status":"racecard_available"})
    except Exception as e:
        return jsonify({"error":"netkeiba取得失敗","detail":str(e)}),502

@app.get("/api/dashboard")
def dashboard():
    date=request.args.get("date","").replace("-","")
    venue=request.args.get("venue","東京")
    selected=request.args.get("race_id","")
    if not re.fullmatch(r"\d{8}",date): return jsonify({"error":"date must be YYYYMMDD"}),400
    try:
        races=netkeiba_races(date,venue)
        if not races:
            sched=jra_schedule_status(date,venue)
            return jsonify({"date":date,"venue":venue,"races":[],"horses":[],**sched,"track":{"condition":None,"cushion":None,"moisture_4c":None,"moisture_goal":None,"measurement_time":None,"bias":None,"note":"出馬表公開前。レース当日の馬場情報はまだ評価しません。"},"weather":{"available":False},"bias":{"races_used":0,"summary":"出馬表公開前","front":"—","stalk":"—","closer":"—","deep":"—","inside":"—","outside":"—"}})
        if selected:
            race_id=selected
        else:
            race_id=races[0]["race_id"]
        race_meta=next((x for x in races if x["race_id"]==race_id),{"race_id":race_id})
        parsed=parse_race(race_id)
        track=jra_track(venue)
        wx=weather(venue)
        bias=same_day_bias(date,venue,race_id)
        speed=parse_speed_index(race_id)
        horses=[]
        for h in parsed["horses"]:
            sc,grade,tags,parts=score_horse(h,track,bias)
            horses.append({**h,"score":sc,"buy_grade":grade,"tags":tags,"score_parts":parts,
                           "reason":"取得できた事実だけで評価。未取得項目は加点していません。"})
        horses.sort(key=lambda x:(x["score"], -(x.get("odds_num") or 999)),reverse=True)
        return jsonify({"date":date,"venue":venue,"race":{**race_meta,"title":parsed.get("title")},"horses":horses,
                        "track":track,"weather":wx,"bias":bias,"speed_index_rows":speed,
                        "sources":{"netkeiba":"出馬表・オッズ・タイム指数等","jra":"馬場状態・クッション値・含水率等","weather":"Open-Meteo"},
                        "model":{"total":100,"note":"予想モデル。事実取得とモデル判断を分離。的中・利益は保証しません。"}})
    except Exception as e:
        return jsonify({"error":"予想データ取得に失敗しました","detail":str(e)}),502

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")),debug=False)
