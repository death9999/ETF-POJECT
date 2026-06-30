"""
ETF 持股爬蟲
優先順序：MoneyDJ Basic0007B → etfinfo.tw NUXT → pocket.tw → 投信官網
"""
import re
import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def safe_get(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"[錯誤] 無法抓取 {url}: {e}")
        return None


def scrape_moneydj_holdings(etf_code):
    """
    MoneyDJ Basic0007B 持股爬蟲（完整版，伺服器端渲染）
    table[4] 結構：3欄 - [個股名稱(含代號如台積電(2330.TW)), 投資比例(%), 持有股數]
    """
    url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={etf_code}.TW"
    r = safe_get(url, timeout=15)
    if not r:
        print(f"[MoneyDJ] {etf_code}: safe_get 失敗")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    print(f"[MoneyDJ] {etf_code}: 共 {len(tables)} 個table")

    holdings = []

    for idx in [4, 3, 5]:
        if idx >= len(tables):
            continue
        ths = tables[idx].find_all("th")
        th_texts = [th.get_text(strip=True) for th in ths]
        if not any("比例" in t or "名稱" in t or "持有" in t for t in th_texts):
            continue
        tds = tables[idx].find_all("td")
        if len(tds) < 9:
            continue
        print(f"[MoneyDJ] table[{idx}] tds={len(tds)} ths={th_texts}")

        for i in range(0, len(tds) - 2, 3):
            try:
                name_cell = tds[i].get_text(strip=True)
                pct_cell  = tds[i+1].get_text(strip=True)
                if not name_cell:
                    continue
                code_match = re.search(r"\((\d{4,6}[A-Za-z]?)\.TW[O]?\)", name_cell)
                if code_match:
                    code = code_match.group(1)
                    name = re.sub(r"\s*\(\d{4,6}[A-Za-z]?\.TW[O]?\)", "", name_cell).strip()
                else:
                    code_match2 = re.search(r"(\d{4,6}[A-Za-z]?)", name_cell)
                    code = code_match2.group(1) if code_match2 else ""
                    name = re.sub(r"\d{4,6}[A-Za-z]?", "", name_cell).strip() or name_cell
                pct_str = re.sub(r"[^\d.]", "", pct_cell.split("%")[0])
                pct = float(pct_str) if pct_str else 0.0
                if pct <= 0 or pct > 100:
                    continue
                holdings.append({"code": code, "name": name, "pct": round(pct, 3), "status": "hold"})
            except Exception:
                continue

        if len(holdings) >= 3:
            break

    seen, unique = set(), []
    for h in holdings:
        k = h["code"] or h["name"]
        if k and k not in seen:
            seen.add(k)
            unique.append(h)

    print(f"[MoneyDJ] {etf_code} 最終 {len(unique)} 檔")
    return unique if len(unique) >= 3 else None


def scrape_etfinfo(etf_code):
    """
    完整持股爬蟲：
    1. 優先用 MoneyDJ Basic0007B
    2. 備援 etfinfo.tw NUXT_DATA
    3. 備援 pocket.tw
    """
    holdings = scrape_moneydj_holdings(etf_code)
    if holdings and len(holdings) >= 5:
        print(f"[MoneyDJ] {etf_code}: 抓到 {len(holdings)} 檔完整持股")
        return holdings

    print(f"[etfinfo] MoneyDJ失敗，改試 etfinfo.tw {etf_code}")
    try:
        url = f"https://www.etfinfo.tw/etf/{etf_code}/holdings"
        r = safe_get(url, timeout=12)
        if r:
            soup = BeautifulSoup(r.text, "html.parser")
            nuxt_script = soup.find("script", id="__NUXT_DATA__")
            if nuxt_script:
                raw_str = nuxt_script.string or ""
                holding_objs = re.findall(
                    r'"code":"(\d{4,6}[A-Za-z]?)","name":"([^"]+)","weight":([\d.]+)',
                    raw_str
                )
                if holding_objs:
                    etf_holdings = []
                    seen_codes = set()
                    for code, name, weight in holding_objs:
                        if code in seen_codes:
                            continue
                        seen_codes.add(code)
                        pct = round(float(weight), 3)
                        if pct > 0:
                            etf_holdings.append({"code": code, "name": name,
                                                 "pct": pct, "status": "hold"})
                    if len(etf_holdings) >= 3:
                        print(f"[etfinfo NUXT] {etf_code}: 抓到 {len(etf_holdings)} 檔")
                        return etf_holdings
    except Exception as e:
        print(f"[etfinfo NUXT] {etf_code} 失敗: {e}")

    try:
        url = f"https://www.pocket.tw/etf/tw/{etf_code}/fundholding/"
        r = safe_get(url, timeout=10)
        if r and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            h = []
            for table in soup.find_all("table"):
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 2:
                        continue
                    try:
                        cell = cols[0].get_text(strip=True)
                        cm = re.match(r"^(\d{4,6}[A-Za-z]?)", cell)
                        if not cm:
                            continue
                        code = cm.group(1)
                        name = cell[len(code):].strip()
                        pct = 0.0
                        for col in cols[1:]:
                            txt = col.get_text(strip=True)
                            if "%" in txt:
                                ps = re.sub(r"[^\d.]", "", txt.split("%")[0])
                                if ps:
                                    pct = float(ps)
                                break
                        if code and pct > 0:
                            h.append({"code": code, "name": name,
                                      "pct": round(pct, 2), "status": "hold"})
                    except Exception:
                        continue
            if h:
                print(f"[pocket] {etf_code}: 抓到 {len(h)} 檔")
                return h
    except Exception as e:
        print(f"[pocket] {etf_code} 失敗: {e}")

    print(f"[爬蟲] {etf_code} 全部來源失敗")
    return None


def scrape_fhtrust(etf_page_code):
    """復華投信官網備援（00991A）"""
    url = f"https://www.fhtrust.com.tw/ETF/etf_detail/{etf_page_code}"
    r = safe_get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    holdings = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "證券代號" not in headers and "證券名稱" not in headers:
            continue
        try:
            code_idx = next(i for i, h in enumerate(headers) if "代號" in h)
            name_idx = next(i for i, h in enumerate(headers) if "名稱" in h)
            pct_idx  = next((i for i, h in enumerate(headers) if "權重" in h or "%" in h), None)
            if pct_idx is None:
                continue
        except StopIteration:
            continue
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) <= max(code_idx, name_idx, pct_idx):
                continue
            try:
                code = cols[code_idx].get_text(strip=True)
                name = cols[name_idx].get_text(strip=True)
                pct_text = re.sub(r"[^\d.]", "", cols[pct_idx].get_text(strip=True))
                pct = float(pct_text) if pct_text else 0.0
                if code and name and pct > 0:
                    holdings.append({"code": code, "name": name, "pct": round(pct, 3), "status": "hold"})
            except Exception:
                continue
    return holdings if holdings else None


def scrape_capital(product_id):
    """群益投信官網備援（00992A）"""
    url = f"https://www.capitalfund.com.tw/etf/product/detail/{product_id}/portfolio"
    r = safe_get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    holdings = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not any("代號" in h or "名稱" in h for h in headers):
            continue
        code_idx = next((i for i, h in enumerate(headers) if "代號" in h), None)
        name_idx = next((i for i, h in enumerate(headers) if "名稱" in h), None)
        pct_idx  = next((i for i, h in enumerate(headers) if "權重" in h or "%" in h), None)
        if None in (code_idx, name_idx, pct_idx):
            continue
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) <= max(code_idx, name_idx, pct_idx):
                continue
            try:
                code = cols[code_idx].get_text(strip=True)
                name = cols[name_idx].get_text(strip=True)
                pct_text = re.sub(r"[^\d.]", "", cols[pct_idx].get_text(strip=True))
                pct = float(pct_text) if pct_text else 0.0
                if code and name and pct > 0:
                    holdings.append({"code": code, "name": name, "pct": round(pct, 2), "status": "hold"})
            except Exception:
                continue
    return holdings if holdings else None


# 靜態備援資料（所有來源失敗時使用，資料日期 2026/05/27）
FALLBACK = {
    "00991A": [
        {"code":"2330","name":"台積電","pct":16.01,"status":"hold"},
        {"code":"2383","name":"台光電","pct":7.82,"status":"hold"},
        {"code":"3037","name":"欣興","pct":6.71,"status":"hold"},
        {"code":"8299","name":"群聯","pct":6.58,"status":"hold"},
        {"code":"2327","name":"國巨*","pct":6.51,"status":"hold"},
        {"code":"2408","name":"南亞科","pct":4.16,"status":"hold"},
        {"code":"2454","name":"聯發科","pct":4.12,"status":"hold"},
        {"code":"2345","name":"智邦","pct":4.05,"status":"hold"},
        {"code":"2308","name":"台達電","pct":3.86,"status":"hold"},
        {"code":"7769","name":"鴻勁","pct":3.82,"status":"hold"},
        {"code":"3017","name":"奇鋐","pct":3.74,"status":"hold"},
        {"code":"2368","name":"金像電","pct":3.41,"status":"hold"},
        {"code":"6223","name":"旺矽","pct":3.38,"status":"hold"},
        {"code":"6669","name":"緯穎","pct":3.37,"status":"hold"},
        {"code":"5274","name":"信驊","pct":3.33,"status":"hold"},
        {"code":"8046","name":"南電","pct":3.12,"status":"hold"},
        {"code":"3189","name":"景碩","pct":2.82,"status":"hold"},
        {"code":"6515","name":"穎崴","pct":2.5,"status":"hold"},
        {"code":"2360","name":"致茂","pct":2.42,"status":"hold"},
        {"code":"8210","name":"勤誠","pct":2.23,"status":"hold"},
        {"code":"8996","name":"高力","pct":2.17,"status":"hold"},
        {"code":"3042","name":"晶技","pct":1.57,"status":"hold"},
        {"code":"3711","name":"日月光投控","pct":0.01,"status":"hold"},
        {"code":"2059","name":"川湖","pct":0.01,"status":"hold"},
        {"code":"3653","name":"健策","pct":0.01,"status":"hold"},
        {"code":"6510","name":"精測","pct":0.01,"status":"hold"},
        {"code":"2449","name":"京元電子","pct":0.01,"status":"hold"},
        {"code":"2382","name":"廣達","pct":0.01,"status":"hold"},
        {"code":"2313","name":"華通","pct":0.01,"status":"hold"},
        {"code":"2317","name":"鴻海","pct":0.01,"status":"hold"},
    ],
    "00981A": [
        {"code":"2330","name":"台積電","pct":9.24,"status":"hold"},
        {"code":"2383","name":"台光電","pct":8.72,"status":"hold"},
        {"code":"2454","name":"聯發科","pct":6.35,"status":"hold"},
        {"code":"2345","name":"智邦","pct":5.7,"status":"hold"},
        {"code":"2308","name":"台達電","pct":5.39,"status":"hold"},
        {"code":"3017","name":"奇鋐","pct":4.89,"status":"hold"},
        {"code":"6223","name":"旺矽","pct":4.73,"status":"hold"},
        {"code":"3037","name":"欣興","pct":4.57,"status":"hold"},
        {"code":"6669","name":"緯穎","pct":4.32,"status":"hold"},
        {"code":"2327","name":"國巨*","pct":4.23,"status":"hold"},
        {"code":"2368","name":"金像電","pct":3.87,"status":"hold"},
        {"code":"3665","name":"貿聯-KY","pct":3.72,"status":"hold"},
        {"code":"8046","name":"南電","pct":3.67,"status":"hold"},
        {"code":"3711","name":"日月光投控","pct":3.34,"status":"hold"},
        {"code":"2303","name":"聯電","pct":3.02,"status":"hold"},
        {"code":"3653","name":"健策","pct":2.9,"status":"hold"},
        {"code":"5274","name":"信驊","pct":2.54,"status":"hold"},
        {"code":"6274","name":"台燿","pct":2.29,"status":"hold"},
        {"code":"2449","name":"京元電子","pct":1.53,"status":"hold"},
        {"code":"6515","name":"穎崴","pct":1.37,"status":"hold"},
        {"code":"6510","name":"精測","pct":1.11,"status":"hold"},
        {"code":"6805","name":"富世達","pct":1.1,"status":"hold"},
        {"code":"8210","name":"勤誠","pct":1.09,"status":"hold"},
        {"code":"2404","name":"漢唐","pct":0.76,"status":"hold"},
        {"code":"3533","name":"嘉澤","pct":0.71,"status":"hold"},
        {"code":"3264","name":"欣銓","pct":0.5,"status":"hold"},
        {"code":"6187","name":"萬潤","pct":0.49,"status":"hold"},
        {"code":"5439","name":"高技","pct":0.48,"status":"hold"},
        {"code":"8996","name":"高力","pct":0.45,"status":"hold"},
        {"code":"3008","name":"大立光","pct":0.42,"status":"hold"},
        {"code":"8358","name":"金居","pct":0.37,"status":"hold"},
        {"code":"8150","name":"南茂","pct":0.34,"status":"hold"},
        {"code":"6278","name":"台表科","pct":0.32,"status":"hold"},
        {"code":"6147","name":"頎邦","pct":0.29,"status":"hold"},
        {"code":"4966","name":"譜瑞-KY","pct":0.29,"status":"hold"},
        {"code":"1590","name":"亞德客-KY","pct":0.27,"status":"hold"},
        {"code":"6415","name":"矽力*-KY","pct":0.23,"status":"hold"},
        {"code":"3443","name":"創意","pct":0.18,"status":"hold"},
        {"code":"2313","name":"華通","pct":0.16,"status":"hold"},
        {"code":"3376","name":"新日興","pct":0.13,"status":"hold"},
        {"code":"6191","name":"精成科","pct":0.13,"status":"hold"},
        {"code":"2481","name":"強茂","pct":0.11,"status":"hold"},
        {"code":"6271","name":"同欣電","pct":0.06,"status":"hold"},
        {"code":"2002","name":"中鋼","pct":0.04,"status":"hold"},
        {"code":"1319","name":"東陽","pct":0.01,"status":"hold"},
    ],
    "00403A": [
        {"code":"2330","name":"台積電","pct":16.34,"status":"hold"},
        {"code":"2303","name":"聯電","pct":5.35,"status":"hold"},
        {"code":"3037","name":"欣興","pct":5,"status":"hold"},
        {"code":"3017","name":"奇鋐","pct":4.55,"status":"hold"},
        {"code":"3711","name":"日月光投控","pct":3.8,"status":"hold"},
        {"code":"2383","name":"台光電","pct":3.67,"status":"hold"},
        {"code":"2368","name":"金像電","pct":3.46,"status":"hold"},
        {"code":"2327","name":"國巨*","pct":2.98,"status":"hold"},
        {"code":"2345","name":"智邦","pct":2.63,"status":"hold"},
        {"code":"2308","name":"台達電","pct":2.2,"status":"hold"},
        {"code":"2344","name":"華邦電","pct":2.12,"status":"hold"},
        {"code":"6669","name":"緯穎","pct":2.06,"status":"hold"},
        {"code":"6223","name":"旺矽","pct":2.01,"status":"hold"},
        {"code":"8046","name":"南電","pct":1.68,"status":"hold"},
        {"code":"3533","name":"嘉澤","pct":1.57,"status":"hold"},
        {"code":"4958","name":"臻鼎-KY","pct":1.52,"status":"hold"},
        {"code":"3665","name":"貿聯-KY","pct":1.5,"status":"hold"},
        {"code":"8299","name":"群聯","pct":1.5,"status":"hold"},
        {"code":"2454","name":"聯發科","pct":1.39,"status":"hold"},
        {"code":"5274","name":"信驊","pct":1.37,"status":"hold"},
        {"code":"2337","name":"旺宏","pct":1.27,"status":"hold"},
        {"code":"3081","name":"聯亞","pct":1.06,"status":"hold"},
        {"code":"2360","name":"致茂","pct":1.02,"status":"hold"},
        {"code":"3443","name":"創意","pct":1.01,"status":"hold"},
        {"code":"6442","name":"光聖","pct":0.97,"status":"hold"},
        {"code":"5347","name":"世界","pct":0.84,"status":"hold"},
        {"code":"3653","name":"健策","pct":0.81,"status":"hold"},
        {"code":"6515","name":"穎崴","pct":0.76,"status":"hold"},
        {"code":"8996","name":"高力","pct":0.75,"status":"hold"},
        {"code":"3529","name":"力旺","pct":0.7,"status":"hold"},
        {"code":"7769","name":"鴻勁","pct":0.67,"status":"hold"},
        {"code":"6147","name":"頎邦","pct":0.67,"status":"hold"},
        {"code":"2313","name":"華通","pct":0.63,"status":"hold"},
        {"code":"6274","name":"台燿","pct":0.61,"status":"hold"},
        {"code":"3044","name":"健鼎","pct":0.6,"status":"hold"},
        {"code":"8210","name":"勤誠","pct":0.5,"status":"hold"},
        {"code":"8358","name":"金居","pct":0.47,"status":"hold"},
        {"code":"6805","name":"富世達","pct":0.46,"status":"hold"},
        {"code":"3583","name":"辛耘","pct":0.42,"status":"hold"},
        {"code":"4966","name":"譜瑞-KY","pct":0.33,"status":"hold"},
        {"code":"4979","name":"華星光","pct":0.3,"status":"hold"},
        {"code":"3189","name":"景碩","pct":0.29,"status":"hold"},
        {"code":"3105","name":"穩懋","pct":0.28,"status":"hold"},
        {"code":"3036","name":"文曄","pct":0.23,"status":"hold"},
        {"code":"2049","name":"上銀","pct":0.21,"status":"hold"},
        {"code":"6196","name":"帆宣","pct":0.2,"status":"hold"},
        {"code":"2481","name":"強茂","pct":0.19,"status":"hold"},
        {"code":"2455","name":"全新","pct":0.1,"status":"hold"},
        {"code":"1560","name":"中砂","pct":0.03,"status":"hold"},
    ],
    "00992A": [
        {"code":"2330","name":"台積電","pct":7.33,"status":"hold"},
        {"code":"3037","name":"欣興","pct":5.78,"status":"hold"},
        {"code":"2383","name":"台光電","pct":5.2,"status":"hold"},
        {"code":"2345","name":"智邦","pct":4.86,"status":"hold"},
        {"code":"6669","name":"緯穎","pct":4.5,"status":"hold"},
        {"code":"6223","name":"旺矽","pct":3.88,"status":"hold"},
        {"code":"8996","name":"高力","pct":3.86,"status":"hold"},
        {"code":"3105","name":"穩懋","pct":3.54,"status":"hold"},
        {"code":"3443","name":"創意","pct":3.49,"status":"hold"},
        {"code":"3661","name":"世芯-KY","pct":3.34,"status":"hold"},
        {"code":"2059","name":"川湖","pct":3.31,"status":"hold"},
        {"code":"8046","name":"南電","pct":2.8,"status":"hold"},
        {"code":"3665","name":"貿聯-KY","pct":2.77,"status":"hold"},
        {"code":"6510","name":"精測","pct":2.64,"status":"hold"},
        {"code":"3017","name":"奇鋐","pct":2.5,"status":"hold"},
        {"code":"6515","name":"穎崴","pct":2.47,"status":"hold"},
        {"code":"2455","name":"全新","pct":2.43,"status":"hold"},
        {"code":"8299","name":"群聯","pct":2.25,"status":"hold"},
        {"code":"3081","name":"聯亞","pct":2.2,"status":"hold"},
        {"code":"2454","name":"聯發科","pct":2.13,"status":"hold"},
        {"code":"3163","name":"波若威","pct":2.07,"status":"hold"},
        {"code":"3533","name":"嘉澤","pct":2.03,"status":"hold"},
        {"code":"2327","name":"國巨*","pct":1.94,"status":"hold"},
        {"code":"7769","name":"鴻勁","pct":1.94,"status":"hold"},
        {"code":"6274","name":"台燿","pct":1.84,"status":"hold"},
        {"code":"6584","name":"南俊國際","pct":1.82,"status":"hold"},
        {"code":"2368","name":"金像電","pct":1.77,"status":"hold"},
        {"code":"2308","name":"台達電","pct":1.54,"status":"hold"},
        {"code":"3653","name":"健策","pct":1.33,"status":"hold"},
        {"code":"7751","name":"竑騰","pct":1.25,"status":"hold"},
        {"code":"6442","name":"光聖","pct":0.99,"status":"hold"},
        {"code":"5274","name":"信驊","pct":0.95,"status":"hold"},
        {"code":"2360","name":"致茂","pct":0.95,"status":"hold"},
        {"code":"4991","name":"環宇-KY","pct":0.92,"status":"hold"},
        {"code":"3036","name":"文曄","pct":0.63,"status":"hold"},
        {"code":"3189","name":"景碩","pct":0.62,"status":"hold"},
        {"code":"6531","name":"愛普*","pct":0.54,"status":"hold"},
        {"code":"5289","name":"宜鼎","pct":0.49,"status":"hold"},
        {"code":"8021","name":"尖點","pct":0.44,"status":"hold"},
        {"code":"2467","name":"志聖","pct":0.43,"status":"hold"},
        {"code":"2337","name":"旺宏","pct":0.43,"status":"hold"},
        {"code":"6139","name":"亞翔","pct":0.39,"status":"hold"},
        {"code":"7734","name":"印能科技","pct":0.39,"status":"hold"},
        {"code":"6805","name":"富世達","pct":0.37,"status":"hold"},
        {"code":"3491","name":"昇達科","pct":0.37,"status":"hold"},
        {"code":"3711","name":"日月光投控","pct":0.36,"status":"hold"},
        {"code":"3260","name":"威剛","pct":0.3,"status":"hold"},
        {"code":"3583","name":"辛耘","pct":0.05,"status":"hold"},
    ],
}
