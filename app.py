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
    out = list(found.values())
    out.sort(key=lambda x: x["race_id"])
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
        name = clean(row.select_one(".HorseName a, .HorseName, [class*='HorseName']"))
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
                "number": number, "name": name, "jockey": jockey, "trainer": trainer,
                "odds": odds_s, "odds_num": parse_num(odds_s), "sex_age": sex_age,
                "weight": weight, "style": style, "raw_cells": cells
            })
    return {"title": title, "meta_text": meta_text[:5000], "horses": horses}


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
    # Transparent 100-point framework. Missing evidence = 0, never guessed.
    p = {"基礎能力・近走":0,"距離適性":0,"コース適性":0,"騎手":0,"枠順":0,"脚質・展開":0,"当日バイアス":0,"芝荒れ・走行位置":0,"馬場・含水率":0,"天候":0,"過去傾向":0,"オッズ妙味":0}
    tags=[]
    odds=h.get("odds_num")
    style=h.get("style")
    if h.get("jockey"): p["騎手"] = 4
    if h.get("trainer"): p["コース適性"] = 2
    try:
        n=int(re.search(r"\d+", h.get("number","")).group())
        p["枠順"] = 4 if n in (6,7,8) else 2
    except Exception: pass
    if style:
        if style in ("逃げ","先行") and isinstance(bias.get("stalk"),int) and bias.get("stalk",0)>bias.get("closer",0):
            p["脚質・展開"] += 6; tags.append("前有利と一致")
        if style in ("差し","追込") and isinstance(bias.get("closer"),int) and bias.get("closer",0)>bias.get("stalk",0):
            p["当日バイアス"] += 6; tags.append("差し有利と一致")
    if track.get("condition") in ("重","不良") and style in ("差し","追込"):
        p["馬場・含水率"] += 2
        tags.append("道悪適性チェック")
    if odds is not None:
        if odds >= 15: p["オッズ妙味"] = 5; tags.append("高オッズ妙味")
        elif odds >= 8: p["オッズ妙味"] = 3; tags.append("妙味あり")
        elif odds <= 3: p["オッズ妙味"] = -2; tags.append("過剰人気警戒")
    score = max(0,min(100,50+sum(p.values())))
    grade = "S" if score>=85 else "A" if score>=75 else "B" if score>=65 else "C"
    return score, grade, tags, p


APP_HTML = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#10213f"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="/manifest.webmanifest"><title>競馬予想AI LIVE 完成版</title>\n<style>body{margin:0;background:#f3f6fb;color:#152033;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans JP",sans-serif}.app{max-width:760px;margin:auto;padding-bottom:40px}header{background:linear-gradient(135deg,#10213f,#2b5cb7);color:#fff;padding:20px 15px;border-radius:0 0 22px 22px}h1{font-size:22px;margin:0 0 5px}.sub{font-size:11px;color:#d9e6ff}main{padding:12px}.card{background:#fff;border:1px solid #e4e8ef;border-radius:15px;padding:13px;margin-bottom:10px;box-shadow:0 2px 8px #00000008}label,.muted{font-size:11px;color:#697386}input,select,button{font:inherit}input,select{width:100%;box-sizing:border-box;padding:10px;border:1px solid #d9dee8;border-radius:10px;background:#fff}button{border:0;border-radius:11px;padding:11px 13px;background:#2463e8;color:#fff;font-weight:800}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{background:#f7f9fc;border-radius:11px;padding:10px}.metric b{font-size:18px}.row{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-bottom:1px solid #eef0f4;font-size:12px}.row:last-child{border:0}.horse{border:1px solid #e0e5ee;border-radius:14px;padding:12px;margin-top:8px}.top{display:grid;grid-template-columns:30px 1fr auto;gap:8px;align-items:center}.rank{font-size:19px;font-weight:900}.score{font-size:23px;color:#1748a4;font-weight:900}.name{font-weight:900}.tag{display:inline-block;background:#eef4ff;color:#1750a8;border-radius:999px;padding:3px 7px;font-size:10px;margin:5px 3px 0 0}.reason{background:#f7f9fc;border-radius:10px;padding:9px;margin-top:7px;font-size:11px;line-height:1.55}.status{font-size:11px;margin-top:7px}.ok{color:#087a3d}.bad{color:#c52b2b}.warn{color:#a66a00}.footer{font-size:10px;color:#788396;line-height:1.6}.smallbtn{background:#eef3ff;color:#1748a4;padding:9px 10px}.apirow{display:none}.details{margin-top:8px}.details summary{font-size:11px;color:#1748a4;cursor:pointer}.source{font-size:10px;line-height:1.7}.pill{font-size:10px;padding:3px 7px;border-radius:999px;background:#f0f2f6;margin-left:4px}.bar{height:7px;background:#e9edf3;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:#2463e8}</style></head><body><div class="app"><header><h1>🐎 競馬予想AI LIVE</h1><div class="sub">実データ → 馬場 → 当日バイアス → 展開 → オッズ妙味 → 100点評価</div></header><main>\n<section class="card"><div class="grid"><div><label>開催日</label><input id="date" type="date"></div><div><label>競馬場</label><select id="venue"><option>東京</option><option>中山</option><option>京都</option><option>阪神</option><option>中京</option><option>札幌</option><option>函館</option><option>福島</option><option>新潟</option><option>小倉</option></select></div></div><div style="height:8px"></div><div class="grid"><div><label>レース</label><select id="race"><option value="">開催取得後に選択</option></select></div><div style="display:flex;align-items:end"><button style="width:100%" onclick="refreshAll()">🔄 実データ更新</button></div></div><div id="status" class="status muted">待機中</div></section>\n<section class="card"><h3>🌱 当日馬場</h3><div id="track">未取得</div></section>\n<section class="card"><h3>🌤 天候</h3><div id="weather">未取得</div></section>\n<section class="card"><h3>🏇 当日バイアス</h3><div id="bias">未取得</div></section>\n<section class="card"><h3>⭐ 最終予想</h3><div id="horses">開催日・競馬場を選んで更新してください</div></section>\n<section class="card"><h3>📊 モデル</h3><div class="row"><span>100点モデル</span><b>採用</b></div><div class="muted">基礎能力20 / 距離10 / コース10 / 騎手8 / 枠5 / 展開12 / 当日8 / 芝7 / 馬場5 / 天候5 / 傾向5 / オッズ5</div></section>\n<section class="card"><h3>🔎 データの出どころ</h3><div id="sources" class="source">netkeiba：出馬表・オッズ・指数等<br>JRA：馬場情報<br>天候：Open-Meteo</div></section>\n<p class="footer">※これは予想支援ツールです。的中・利益を保証しません。未取得のデータは推測で埋めず、モデル加点もしません。各サイトの利用条件・アクセス制限に従って利用してください。</p></main></div>\n<script>const $=id=>document.getElementById(id);const API=(localStorage.getItem(\'UMA_API_URL\')||\'\').replace(/\\/$/,\'\');let raceList=[];function base(){return API||location.origin}function today(){return new Date().toISOString().slice(0,10)}$(\'date\').value=today();async function api(path){const r=await fetch(base()+path);if(!r.ok)throw new Error(await r.text());return r.json()}async function loadRaces(){const date=$(\'date\').value.replaceAll(\'-\',\'\'),venue=$(\'venue\').value;$(\'status\').textContent=\'開催・レースを取得中…\';$(\'status\').className=\'status warn\';const d=await api(\'/api/races?date=\'+date+\'&venue=\'+encodeURIComponent(venue));raceList=d.races||[];$(\'race\').innerHTML=raceList.map((x,i)=>\'<option value="\'+x.race_id+\'">\'+(i+1)+\'R \'+esc(x.label||x.race_id)+\'</option>\').join(\'\')||\'<option value="">レースなし</option>\';$(\'status\').textContent=raceList.length+\'レース取得\';$(\'status\').className=\'status ok\'}async function loadDashboard(){const date=$(\'date\').value.replaceAll(\'-\',\'\'),venue=$(\'venue\').value,rid=$(\'race\').value;let q=\'/api/dashboard?date=\'+date+\'&venue=\'+encodeURIComponent(venue)+(rid?\'&race_id=\'+encodeURIComponent(rid):\'\');$(\'status\').textContent=\'馬場・オッズ・出走馬を取得中…\';const d=await api(q);render(d);$(\'status\').textContent=\'更新完了 \'+new Date().toLocaleTimeString(\'ja-JP\');$(\'status\').className=\'status ok\'}async function refreshAll(){try{await loadRaces();await loadDashboard()}catch(e){$(\'status\').textContent=\'取得失敗：\'+e.message;$(\'status\').className=\'status bad\'}}function esc(s){return String(s).replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]))}function render(d){const t=d.track||{};$(\'track\').innerHTML=\'<div class="grid"><div class="metric"><label>馬場状態</label><br><b>\'+esc(t.condition||\'—\')+\'</b></div><div class="metric"><label>クッション値</label><br><b>\'+(t.cushion??\'—\')+\'</b></div><div class="metric"><label>含水率4角</label><br><b>\'+(t.moisture_4c??\'—\')+\'</b></div><div class="metric"><label>含水率ゴール</label><br><b>\'+(t.moisture_goal??\'—\')+\'</b></div></div><div class="row"><span>芝バイアス</span><b>\'+esc(t.bias||\'結果から推定\')+\'</b></div><div class="muted">\'+esc(t.note||\'\')+\'</div>\';$(\'weather\').innerHTML=d.weather?.available?\'<div class="grid"><div class="metric"><label>気温</label><br><b>\'+d.weather.temperature+\'℃</b></div><div class="metric"><label>降水量</label><br><b>\'+d.weather.precipitation+\'mm</b></div></div>\':\'取得不可\';const b=d.bias||{};$(\'bias\').innerHTML=\'<div class="grid"><div class="metric"><label>逃げ</label><br><b>\'+b.front+\'</b></div><div class="metric"><label>先行</label><br><b>\'+b.stalk+\'</b></div><div class="metric"><label>差し</label><br><b>\'+b.closer+\'</b></div><div class="metric"><label>追込</label><br><b>\'+b.deep+\'</b></div></div><div class="row"><span>判定</span><b>\'+esc(b.summary||\'—\')+\'</b></div><div class="muted">同日結果の暫定集計：\'+(b.races_used||0)+\'R</div>\';$(\'horses\').innerHTML=(d.horses||[]).map((x,i)=>{const mark=i===0?\'◎\':i===1?\'○\':i===2?\'▲\':\'☆\';const parts=Object.entries(x.score_parts||{}).filter(([k,v])=>v!==0).map(([k,v])=>k+\' \'+(v>0?\'+\':\'\')+v).join(\' / \')||\'取得済み加点なし\';return \'<div class="horse"><div class="top"><div class="rank">\'+mark+\'</div><div><div class="name">\'+esc(x.name||\'馬名不明\')+\'</div><div class="muted">\'+esc(x.number||\'\')+\'番｜\'+esc(x.style||\'脚質不明\')+\'｜\'+esc(x.jockey||\'騎手不明\')+\'</div></div><div><div class="score">\'+x.score+\'</div><div class="muted">\'+(x.odds??\'—\')+\'倍</div></div></div><div>\'+(x.tags||[]).map(v=>\'<span class="tag">\'+esc(v)+\'</span>\').join(\'\')+\'</div><div class="reason"><b>\'+x.buy_grade+\'</b>｜\'+esc(x.reason)+\'<br><span class="muted">\'+esc(parts)+\'</span></div><details class="details"><summary>取得事実を見る</summary><div class="muted">騎手：\'+esc(x.jockey||\'—\')+\' / 調教師：\'+esc(x.trainer||\'—\')+\' / 性齢：\'+esc(x.sex_age||\'—\')+\' / 馬体重：\'+esc(x.weight||\'—\')+\'</div></details></div>\'}).join(\'\')||\'出走馬データなし\';$(\'sources\').innerHTML=\'<b>netkeiba</b>：出馬表・オッズ・タイム指数等<br><b>JRA</b>：馬場状態・クッション値・含水率等<br><b>天候</b>：Open-Meteo\'}$(\'race\').addEventListener(\'change\',()=>loadDashboard().catch(e=>{$(\'status\').textContent=\'取得失敗：\'+e.message;$(\'status\').className=\'status bad\'}));</script></body></html>\n'

@app.get("/")
def index():
    return APP_HTML

@app.get("/api/health")
def health():
    return jsonify({"ok":True,"version":"live-v1.0","sources":["netkeiba","JRA","Open-Meteo"]})

@app.get("/api/races")
def races_api():
    date=request.args.get("date","").replace("-","")
    venue=request.args.get("venue","東京")
    if not re.fullmatch(r"\d{8}",date): return jsonify({"error":"date must be YYYYMMDD"}),400
    try:
        races=netkeiba_races(date,venue)
        return jsonify({"date":date,"venue":venue,"races":races})
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
        if selected:
            race_id=selected
        elif races:
            race_id=races[0]["race_id"]
        else:
            return jsonify({"date":date,"venue":venue,"races":[],"horses":[],"error":"開催レースを取得できませんでした"}),404
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