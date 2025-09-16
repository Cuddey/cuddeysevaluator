import os
import time
import math
import re
import json
from urllib.parse import quote_plus, urljoin, urlparse

from flask import Flask, render_template, request
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from googlesearch import search
from requests.exceptions import ReadTimeout, Timeout, RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# --------------------------
# Small in-process TTL cache
# --------------------------
_CACHE = {}
CACHE_TTL_SEC = 6 * 60 * 60     # 6 hours
CACHE_MAX_KEYS = 1000

def cache_get(key):
    v = _CACHE.get(key)
    if not v: return None
    val, ts = v
    if time.time() - ts > CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return val

def cache_set(key, val):
    if len(_CACHE) > CACHE_MAX_KEYS:
        # drop ~10% oldest
        for k, _ in list(sorted(_CACHE.items(), key=lambda kv: kv[1][1]))[: max(1, CACHE_MAX_KEYS // 10)]:
            _CACHE.pop(k, None)
    _CACHE[key] = (val, time.time())


# =============================================================================
# 1) CAD Scrapers (unchanged)
# =============================================================================
def tarrant_cad(address):
    url = f"https://www.tad.org/property-search-results/?searchtext={quote_plus(address)}"
    html = requests.get(url, timeout=10).text
    soup = BeautifulSoup(html, 'html.parser')
    link = soup.select_one('a.property-listing')
    if not link:
        return {}
    detail_url = "https://www.tad.org" + link['href']
    detail_html = requests.get(detail_url, timeout=10).text
    dsoup = BeautifulSoup(detail_html, 'html.parser')
    owner = dsoup.find('h4', text='Owner')
    tax   = dsoup.find('h4', text='Account #')
    mail  = dsoup.find('h4', text='Mailing Address')
    return {
        'owner_name': owner.find_next_sibling('p').get_text(strip=True) if owner else 'N/A',
        'tax_id':     tax.find_next_sibling('p').get_text(strip=True)   if tax   else 'N/A',
        'mailing_address': mail.find_next_sibling('p').get_text(strip=True) if mail else 'N/A'
    }

def dallas_cad(address):
    url = f"https://www.dallascad.org/SearchOwner.aspx?searchTerm={quote_plus(address)}"
    html = requests.get(url, timeout=10).text
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', id='Grid')
    if not table or len(table.find_all('tr')) < 2:
        return {}
    cols = table.find_all('tr')[1].find_all('td')
    return {
        'tax_id': cols[0].get_text(strip=True) or 'N/A',
        'owner_name': cols[1].get_text(strip=True) or 'N/A',
        'mailing_address': cols[2].get_text(strip=True) or 'N/A'
    }

def harris_cad(address): return {}
def bexar_cad(address):  return {}
def travis_cad(address): return {}

cad_modules = {
    'tarrant': tarrant_cad,
    'dallas':  dallas_cad,
    'harris':  harris_cad,
    'bexar':   bexar_cad,
    'travis':  travis_cad
}

def get_cad_details(county, state, address):
    func = cad_modules.get(county.lower())
    if func:
        data = func(address)
        if data:
            return data
    query = quote_plus(f"{county} {state} Appraisal District property search")
    return {'link': f"https://www.google.com/search?q={query}"}


# =============================================================================
# 2) LLC & Owner (unchanged)
# =============================================================================
def get_llc_info(owner_name):
    if not owner_name:
        return {}
    try:
        oc = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={'q': owner_name, 'jurisdiction_code': 'us_tx'}
        ).json()
        comps = oc.get('results', {}).get('companies', [])
        if not comps:
            return {}
        comp = comps[0]['company']
        num = comp.get('company_number')
        return {
            'llc_name': comp.get('name'),
            'formation_date': comp.get('incorporation_date'),
            'opencorporates_url': comp.get('opencorporates_url'),
            'sos_url': f"https://mycpa.cpa.state.tx.us/coa/servlet/DisplayAAE?reportingEntityId={num}"
        }
    except:
        return {}

def get_owner_profile(llc_name):
    return {'linkedIn':'N/A','facebook':'N/A','emails':[],'phones':[],'other_businesses':[]}

def search_owner_online(owner_name, address):
    query = owner_name or address
    results = []
    try:
        for url in search(query, num_results=3):
            html = requests.get(url, timeout=5).text
            soup = BeautifulSoup(html, 'html.parser')
            title = soup.title.string if soup.title else url
            desc_tag = soup.find('meta', attrs={'name': 'description'})
            description = desc_tag['content'] if desc_tag and desc_tag.get('content') else ''
            emails = list(set(re.findall(r'[A-Za-z0-9.+_-]+@[A-Za-z0-9._-]+\.[A-Za-z]+', html)))
            phones = list(set(re.findall(r'\(?\d{3}\)?\s*\d{3}\s*\d{4}', html)))
            results.append({
                'url': url,
                'title': title,
                'description': description,
                'emails': emails,
                'phones': phones
            })
    except:
        pass
    return results


# =============================================================================
# 4) Market & competition (unchanged)
# =============================================================================
def nearby_storage(lat, lng, radius_m):
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        'location': f"{lat},{lng}",
        'radius': int(radius_m),
        'type': 'storage',
        'key': GOOGLE_API_KEY
    }
    all_fac = []
    while True:
        res = requests.get(url, params=params).json()
        all_fac.extend(res.get('results', []))
        token = res.get('next_page_token')
        if not token:
            break
        time.sleep(2)
        params = {'pagetoken': token, 'key': GOOGLE_API_KEY}
    return all_fac

def get_market_comps(lat, lng):
    def compute(rad):
        facs = nearby_storage(lat, lng, rad)
        comps = [{
            'place_id': f.get('place_id'),
            'name':     f.get('name'),
            'rating':   f.get('rating'),
            'reviews':  f.get('user_ratings_total'),
            'vicinity': f.get('vicinity'),
            'lat':      f['geometry']['location']['lat'],
            'lng':      f['geometry']['location']['lng']
        } for f in facs]
        count = len(comps)
        area = math.pi * (rad / 1609.34) ** 2
        dens  = round(count / area, 2) if area else 0
        return comps, count, dens

    c5, cnt5, d5    = compute(5 * 1609.34)
    c10, cnt10, d10 = compute(10 * 1609.34)
    ids5 = {c['place_id'] for c in c5}
    new10 = [c for c in c10 if c['place_id'] not in ids5]
    return {
        'competitors_5':  c5,
        'count_5':        cnt5,
        'density_5':      d5,
        'competitors_10': new10,
        'count_10':       cnt10,
        'density_10':     d10
    }


# =============================================================================
# 5) Listing comps (unchanged)
# =============================================================================
def scrape_crexi(lat, lng, radius_m=1):
    listings = []
    try:
        url = (
            f"https://www.crexi.com/search/properties"
            f"?property_type=Self+Storage&lat={lat}&lng={lng}&radius={radius_m}"
        )
        html = requests.get(url, timeout=5).text
        soup = BeautifulSoup(html, 'html.parser')
        for card in soup.select(".propertycard"):
            name = card.select_one(".card-title")
            price = card.select_one(".card-price")
            size  = card.select_one(".card-size")
            link  = card.find("a", href=True)
            if name and price and size:
                p = re.sub(r'[^\d.]', '', price.get_text())
                s = re.sub(r'[^\d.]', '', size.get_text())
                ppsf = round(float(p) / float(s), 2) if float(s) else 0
                listings.append({
                    'source': 'Crexi',
                    'name':   name.get_text(strip=True),
                    'nrsf':   float(s),
                    'price':  float(p),
                    'ppsf':   ppsf,
                    'link':   "https://www.crexi.com" + link['href'] if link else ''
                })
    except (ReadTimeout, Exception):
        pass
    return listings

def scrape_loopnet(lat, lng, radius_m=1):
    listings = []
    try:
        url = f"https://www.loopnet.com/for-sale/self-storage/{lat},{lng}/radius-{radius_m}"
        html = requests.get(url, timeout=5).text
        soup = BeautifulSoup(html, 'html.parser')
        for card in soup.select(".placardDetails"):
            name = card.select_one(".placardTitle a")
            price = card.select_one(".price")
            size  = card.select_one(".propertySize")
            link  = name['href'] if name else ''
            if name and price and size:
                p = re.sub(r'[^\d.]', '', price.get_text())
                s = re.sub(r'[^\d.]', '', size.get_text())
                ppsf = round(float(p) / float(s), 2) if float(s) else 0
                listings.append({
                    'source': 'LoopNet',
                    'name':   name.get_text(strip=True),
                    'nrsf':   float(s),
                    'price':  float(p),
                    'ppsf':   ppsf,
                    'link':   "https://www.loopnet.com" + link
                })
    except (ReadTimeout, Exception):
        pass
    return listings

def get_surrounding_listings(lat, lng):
    return scrape_crexi(lat, lng) + scrape_loopnet(lat, lng)


# =============================================================================
# === NEW: Stronger, faster, more reliable rate scraping ===
# =============================================================================

# Only these sizes
SIZE_WHITELIST = {"5x5", "5x10", "10x10", "10x15", "10x20", "10x30"}

# Heuristics & regex
_UNIT_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[x×]\s*(\d{1,2})(?!\d)', re.IGNORECASE)
_PRICE_RE = re.compile(r'\$?\s?(\d{2,4})(?:\.\d{2})?\s*(?:/|\bper\b)?\s*(?:mo|month|monthly)?', re.IGNORECASE)
_RATE_HINTS = re.compile(r'(rate|rent|monthly|per\s*month|/mo|special|price)', re.IGNORECASE)
_CC_POS = re.compile(r'(climate|climatized|temperature|temp[-\s]*controlled|a/c|air\s*conditioned)', re.IGNORECASE)
_CC_NEG = re.compile(r'(non[-\s]*climate|non[-\s]*climatized|drive[-\s]*up|standard)', re.IGNORECASE)

# Speed/limits
MAX_COMPETITORS_TO_SCRAPE = 6
PER_REQUEST_TIMEOUT = 5
MAX_BYTES = 600_000
TOTAL_RATE_SCRAPE_BUDGET = 12
DISCOVERY_PATHS = ["/units", "/rent", "/storage-units", "/self-storage", "/pricing", "/rates", "/rent-online"]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/115.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8"
}

def _normalize_size(w, l):
    try:
        w = int(w); l = int(l)
    except:
        pass
    size = f"{w}x{l}"
    return size if size in SIZE_WHITELIST else None

def _safe_fetch_text(url, timeout=PER_REQUEST_TIMEOUT, max_bytes=MAX_BYTES):
    ck = f"fetch:{url}"
    cached = cache_get(ck)
    if cached is not None:
        return cached
    try:
        with requests.get(url, headers=_HEADERS, timeout=timeout, stream=True, allow_redirects=True) as r:
            ct = r.headers.get("Content-Type", "").lower()
            if "text/html" not in ct and "application/xhtml+xml" not in ct:
                cache_set(ck, "")
                return ""
            content = []
            total = 0
            for chunk in r.iter_content(chunk_size=8192, decode_unicode=True):
                if not chunk:
                    break
                content.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            text = "".join(content)
            cache_set(ck, text)
            return text
    except (Timeout, ReadTimeout, RequestException):
        cache_set(ck, "")
        return ""
    except Exception:
        cache_set(ck, "")
        return ""

def _merge_rate_dicts(dicts):
    merged = {}
    for d in dicts:
        for k, v in d.items():
            if k not in SIZE_WHITELIST:  # enforce whitelist
                continue
            merged.setdefault(k, {"climate": None, "non_climate": None})
            for bucket in ("climate", "non_climate"):
                val = v.get(bucket)
                if val is None:
                    continue
                if merged[k][bucket] is None or val < merged[k][bucket]:
                    merged[k][bucket] = val
    return merged

# ---------- General parsers (improved) ----------
def _parse_json_ld(soup):
    rates = {}
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = s.string or s.text or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            if not isinstance(obj, dict): 
                continue
            name = (obj.get("name") or obj.get("sku") or obj.get("description") or "").lower()
            size = None
            for m in _UNIT_RE.finditer(name):
                size = _normalize_size(m.group(1), m.group(2))
                if size: break
            offer = obj.get("offers")
            if isinstance(offer, list): 
                offer = offer[0] if offer else {}
            price = None
            if isinstance(offer, dict):
                price = offer.get("price") or offer.get("lowPrice")
            if not price:
                price = obj.get("price")
            if size and price:
                try:
                    price_v = float(re.sub(r"[^\d.]", "", str(price)))
                except:
                    continue
                cc_bucket = None
                txt = json.dumps(obj).lower()
                if _CC_POS.search(txt) and not _CC_NEG.search(txt): cc_bucket = "climate"
                elif _CC_NEG.search(txt) and not _CC_POS.search(txt): cc_bucket = "non_climate"
                rates.setdefault(size, {"climate": None, "non_climate": None})
                if cc_bucket == "climate":
                    rates[size]["climate"] = price_v if rates[size]["climate"] is None else min(rates[size]["climate"], price_v)
                elif cc_bucket == "non_climate":
                    rates[size]["non_climate"] = price_v if rates[size]["non_climate"] is None else min(rates[size]["non_climate"], price_v)
                else:
                    # fill whichever empty first
                    if rates[size]["non_climate"] is None: rates[size]["non_climate"] = price_v
                    elif rates[size]["climate"] is None:   rates[size]["climate"]   = price_v
    return rates

def _parse_tables(soup):
    rates = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if not headers:
            first = table.find("tr")
            if first:
                headers = [td.get_text(" ", strip=True).lower() for td in first.find_all(["td","th"])]
        if not headers or not any("size" in h for h in headers) or not any(("price" in h) or ("rate" in h) for h in headers):
            continue
        rows = table.find_all("tr")
        for tr in rows[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            if not cells: continue
            row_txt = " ".join(cells).lower()
            m = _UNIT_RE.search(row_txt)
            if not m: continue
            size = _normalize_size(m.group(1), m.group(2))
            if not size: continue
            cc_bucket = "climate" if (_CC_POS.search(row_txt) and not _CC_NEG.search(row_txt)) else ("non_climate" if (_CC_NEG.search(row_txt) and not _CC_POS.search(row_txt)) else None)
            vals=[]
            for p in _PRICE_RE.findall(row_txt):
                try:
                    v=float(p); 
                    if 15<=v<=1000: vals.append(v)
                except: pass
            if not vals: continue
            price=min(vals)
            rates.setdefault(size, {"climate": None, "non_climate": None})
            if cc_bucket == "climate":
                rates[size]["climate"]= price if rates[size]["climate"] is None else min(rates[size]["climate"], price)
            elif cc_bucket == "non_climate":
                rates[size]["non_climate"]= price if rates[size]["non_climate"] is None else min(rates[size]["non_climate"], price)
            else:
                if rates[size]["non_climate"] is None: rates[size]["non_climate"]=price
                elif rates[size]["climate"] is None:   rates[size]["climate"]=price
    return rates

def _parse_cards(soup):
    rates={}
    nodes = soup.select("[class*='unit'], [class*='sr-unit'], [class*='stor'], [data-unit], [data-size]")
    for c in nodes:
        txt = c.get_text(" ", strip=True).lower()
        m=_UNIT_RE.search(txt)
        if not m: continue
        size=_normalize_size(m.group(1), m.group(2))
        if not size: continue
        cc_bucket = "climate" if (_CC_POS.search(txt) and not _CC_NEG.search(txt)) else ("non_climate" if (_CC_NEG.search(txt) and not _CC_POS.search(txt)) else None)
        vals=[]
        for p in _PRICE_RE.findall(txt):
            try:
                v=float(p); 
                if 15<=v<=1000: vals.append(v)
            except: pass
        if not vals: continue
        price=min(vals)
        rates.setdefault(size, {"climate": None, "non_climate": None})
        if cc_bucket=="climate":
            rates[size]["climate"]= price if rates[size]["climate"] is None else min(rates[size]["climate"], price)
        elif cc_bucket=="non_climate":
            rates[size]["non_climate"]= price if rates[size]["non_climate"] is None else min(rates[size]["non_climate"], price)
        else:
            if rates[size]["non_climate"] is None: rates[size]["non_climate"]=price
            elif rates[size]["climate"] is None:   rates[size]["climate"]=price
    return rates

def _regex_fallback(text):
    out={}
    if not text: return out
    text=text.lower()
    for m in _UNIT_RE.finditer(text):
        size=_normalize_size(m.group(1), m.group(2))
        if not size: continue
        start=max(m.start()-150,0); end=min(m.end()+250,len(text))
        win=text[start:end]
        if not _RATE_HINTS.search(win): continue
        bucket = "climate" if (_CC_POS.search(win) and not _CC_NEG.search(win)) else ("non_climate" if (_CC_NEG.search(win) and not _CC_POS.search(win)) else None)
        vals=[]
        for p in _PRICE_RE.findall(win):
            try:
                v=float(p); 
                if 15<=v<=1000: vals.append(v)
            except: pass
        if not vals: continue
        price=min(vals)
        out.setdefault(size, {"climate": None, "non_climate": None})
        if bucket=="climate":
            out[size]["climate"]= price if out[size]["climate"] is None else min(out[size]["climate"], price)
        elif bucket=="non_climate":
            out[size]["non_climate"]= price if out[size]["non_climate"] is None else min(out[size]["non_climate"], price)
        else:
            if out[size]["non_climate"] is None: out[size]["non_climate"]=price
            elif out[size]["climate"] is None:   out[size]["climate"]=price
    return out

# ---------- Chain-specific adapters (accuracy boost) ----------
def _domain(url):
    try:
        return urlparse(url).netloc.lower()
    except:
        return ""

def _scrape_publicstorage(html):
    """PublicStorage uses Next.js __NEXT_DATA__ with unit details."""
    soup=BeautifulSoup(html, 'html.parser')
    data_tag = soup.find("script", id="__NEXT_DATA__")
    rates={}
    if not data_tag or not data_tag.string: 
        return rates
    try:
        data=json.loads(data_tag.string)
    except:
        return rates
    txt=json.dumps(data).lower()
    # Generic walk
    def walk(o):
        local={}
        if isinstance(o, dict):
            s=json.dumps(o).lower()
            for m in _UNIT_RE.finditer(s):
                size=_normalize_size(m.group(1), m.group(2))
                if not size: continue
                vals=[]
                for p in _PRICE_RE.findall(s):
                    try:
                        v=float(p); 
                        if 15<=v<=1000: vals.append(v)
                    except: pass
                if not vals: 
                    continue
                cc_bucket = "climate" if (_CC_POS.search(s) and not _CC_NEG.search(s)) else ("non_climate" if (_CC_NEG.search(s) and not _CC_POS.search(s)) else None)
                price=min(vals)
                local.setdefault(size, {"climate": None, "non_climate": None})
                if cc_bucket=="climate":
                    local[size]["climate"]=price
                elif cc_bucket=="non_climate":
                    local[size]["non_climate"]=price
                else:
                    if local[size]["non_climate"] is None: local[size]["non_climate"]=price
                    elif local[size]["climate"] is None:   local[size]["climate"]=price
            for v in o.values():
                sub=walk(v)
                local=_merge_rate_dicts([local, sub])
        elif isinstance(o, list):
            local={}
            for it in o:
                sub=walk(it)
                local=_merge_rate_dicts([local, sub])
        else:
            return {}
        return local
    return walk(data)

def _scrape_extraspace(html):
    """ExtraSpace often exposes JSON-LD + redux-like state."""
    soup=BeautifulSoup(html,'html.parser')
    rates=_parse_json_ld(soup)
    # look for window.__PRELOADED_STATE__ or INITIAL_STATE
    for s in soup.find_all("script"):
        raw=(s.string or s.text or "")
        if any(k in raw for k in ("__PRELOADED_STATE__", "INITIAL_STATE", "__INITIAL_STATE__")):
            try:
                json_txt=re.sub(r"^[^{\[]+","",raw)
                json_txt=re.sub(r";\s*$","",json_txt)
                data=json.loads(json_txt)
                # walk
                def walk(o):
                    local={}
                    if isinstance(o, dict):
                        st=json.dumps(o).lower()
                        for m in _UNIT_RE.finditer(st):
                            size=_normalize_size(m.group(1), m.group(2))
                            if not size: continue
                            vals=[]
                            for p in _PRICE_RE.findall(st):
                                try:
                                    v=float(p); 
                                    if 15<=v<=1000: vals.append(v)
                                except: pass
                            if not vals: continue
                            bucket="climate" if (_CC_POS.search(st) and not _CC_NEG.search(st)) else ("non_climate" if (_CC_NEG.search(st) and not _CC_POS.search(st)) else None)
                            price=min(vals)
                            local.setdefault(size, {"climate": None, "non_climate": None})
                            if bucket=="climate": local[size]["climate"]=price
                            elif bucket=="non_climate": local[size]["non_climate"]=price
                            else:
                                if local[size]["non_climate"] is None: local[size]["non_climate"]=price
                                elif local[size]["climate"] is None:   local[size]["climate"]=price
                        for v in o.values():
                            sub=walk(v)
                            local=_merge_rate_dicts([local, sub])
                    elif isinstance(o, list):
                        local={}
                        for it in o:
                            sub=walk(it)
                            local=_merge_rate_dicts([local, sub])
                    else:
                        return {}
                    return local
                rates=_merge_rate_dicts([rates, walk(data)])
            except: 
                pass
    if not rates:
        rates=_parse_tables(soup)
        rates=_merge_rate_dicts([rates, _parse_cards(soup)])
    return rates

def _scrape_cubesmart(html):
    """CubeSmart exposes 'initState' JSON in scripts; also table markup."""
    soup=BeautifulSoup(html,'html.parser')
    rates=_parse_tables(soup)
    rates=_merge_rate_dicts([rates, _parse_cards(soup), _parse_json_ld(soup)])
    if rates: return rates
    for s in soup.find_all("script"):
        raw=(s.string or s.text or "")
        if "initState" in raw or "initialState" in raw:
            try:
                json_txt=re.sub(r"^[^{\[]+","",raw)
                json_txt=re.sub(r";\s*$","",json_txt)
                data=json.loads(json_txt)
                # walk
                def walk(o):
                    local={}
                    if isinstance(o, dict):
                        st=json.dumps(o).lower()
                        for m in _UNIT_RE.finditer(st):
                            size=_normalize_size(m.group(1), m.group(2))
                            if not size: continue
                            vals=[]
                            for p in _PRICE_RE.findall(st):
                                try:
                                    v=float(p); 
                                    if 15<=v<=1000: vals.append(v)
                                except: pass
                            if not vals: continue
                            bucket="climate" if (_CC_POS.search(st) and not _CC_NEG.search(st)) else ("non_climate" if (_CC_NEG.search(st) and not _CC_POS.search(st)) else None)
                            price=min(vals)
                            local.setdefault(size, {"climate": None, "non_climate": None})
                            if bucket=="climate": local[size]["climate"]=price
                            elif bucket=="non_climate": local[size]["non_climate"]=price
                            else:
                                if local[size]["non_climate"] is None: local[size]["non_climate"]=price
                                elif local[size]["climate"] is None:   local[size]["climate"]=price
                        for v in o.values():
                            sub=walk(v)
                            local=_merge_rate_dicts([local, sub])
                    elif isinstance(o, list):
                        local={}
                        for it in o:
                            sub=walk(it)
                            local=_merge_rate_dicts([local, sub])
                    else:
                        return {}
                    return local
                return walk(data)
            except:
                pass
    return rates

def _scrape_lifestorage(html):
    """Life Storage: JSON-LD + tables/cards typical."""
    soup=BeautifulSoup(html,'html.parser')
    rates=_parse_json_ld(soup)
    rates=_merge_rate_dicts([rates, _parse_tables(soup), _parse_cards(soup)])
    if not rates:
        text=soup.get_text(" ", strip=True).lower()
        rates=_merge_rate_dicts([rates, _regex_fallback(text)])
    return rates

def _scrape_uhaul(html):
    """U-Haul storage pages often render tables with unit sizes and monthly prices."""
    soup=BeautifulSoup(html,'html.parser')
    rates=_parse_tables(soup)
    if not rates:
        rates=_parse_cards(soup)
    if not rates:
        text=soup.get_text(" ", strip=True).lower()
        rates=_regex_fallback(text)
    return rates

def _domain_specific_rates(url, html):
    d=_domain(url)
    if "publicstorage.com" in d:
        return _scrape_publicstorage(html)
    if "extraspace.com" in d:
        return _scrape_extraspace(html)
    if "cubesmart.com" in d:
        return _scrape_cubesmart(html)
    if "lifestorage.com" in d:
        return _scrape_lifestorage(html)
    if "uhaul.com" in d:
        return _scrape_uhaul(html)
    return {}  # unknown domain → fall back to generic below

def scrape_rates_from_website(url):
    if not url:
        return {}
    # Cache by URL
    ck=f"rates:{url}"
    cached=cache_get(ck)
    if cached is not None:
        return cached

    html=_safe_fetch_text(url)
    if not html:
        cache_set(ck,{})
        return {}

    # Try domain-specific parser first (accuracy boost)
    try:
        ds=_domain_specific_rates(url, html)
    except Exception:
        ds={}
    # Generic strategies
    try:
        soup = BeautifulSoup(html, 'html.parser')
        combined = soup.get_text(separator=' ', strip=True).lower()[:600_000]
    except Exception:
        combined = re.sub(r'<[^>]+>', ' ', html).lower()[:600_000]
        soup = None

    generic_candidates=[]
    if soup is not None:
        try: generic_candidates.append(_parse_json_ld(soup))
        except: pass
        try: generic_candidates.append(_parse_tables(soup))
        except: pass
        try: generic_candidates.append(_parse_cards(soup))
        except: pass
    try: generic_candidates.append(_regex_fallback(combined))
    except: pass

    merged=_merge_rate_dicts([ds]+generic_candidates)
    cache_set(ck, merged)
    return merged

DISCOVERY_PATHS = ["/units", "/rent", "/storage-units", "/self-storage", "/pricing", "/rates", "/rent-online"]

def _try_discovery_paths(base_url, max_paths=3):
    """Try a few common rate pages for a site, quickly."""
    found=[]
    try:
        parsed=urlparse(base_url)
        if not parsed.scheme or not parsed.netloc: 
            return found
        for path in DISCOVERY_PATHS:
            if len(found)>=max_paths: break
            u=urljoin(base_url, path)
            txt=_safe_fetch_text(u, timeout=PER_REQUEST_TIMEOUT)
            if txt and len(txt)>500:
                found.append(u)
    except:
        pass
    return found

def discover_website_for(name, vicinity, fallback_query_suffix="storage units prices"):
    query = f"{name} {vicinity} {fallback_query_suffix}".strip()
    ck=f"discover:{query}"
    c=cache_get(ck)
    if c is not None: return c
    try:
        for url in search(query, num_results=3):
            u=url.lower()
            if any(t in u for t in [".com", ".net", ".org", ".storage"]) and not any(
                b in u for b in ["facebook.com", "yelp.com", "google.com/maps", "bing.com", "yellowpages", "yahoo"]
            ):
                cache_set(ck, url)
                return url
    except Exception:
        pass
    cache_set(ck, None)
    return None

def get_place_website(place_id):
    ck=f"place_site:{place_id}"
    c=cache_get(ck)
    if c is not None:
        return c
    try:
        res = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={'place_id': place_id, 'fields': 'website,url', 'key': GOOGLE_API_KEY},
            timeout=PER_REQUEST_TIMEOUT
        ).json()
        site = res.get('result', {}).get('website') or res.get('result', {}).get('url')
        cache_set(ck, site)
        return site
    except Exception:
        cache_set(ck, None)
        return None

def _scrape_site_with_discovery(base_url, extra_budget_ok=True):
    all_rates=[]
    # base page
    base_rates=scrape_rates_from_website(base_url)
    if base_rates: all_rates.append(base_rates)
    # try a few common paths
    if extra_budget_ok:
        for u in _try_discovery_paths(base_url):
            r=scrape_rates_from_website(u)
            if r: all_rates.append(r)
    return _merge_rate_dicts(all_rates)

def build_rate_analysis(subject_place, market):
    start = time.time()

    # Subject
    subject_site = subject_place.get('website') if subject_place else None
    if not subject_site and subject_place:
        subject_site = discover_website_for(subject_place.get('name', ''), "")

    subject_rates={}
    if subject_site:
        subject_rates = _scrape_site_with_discovery(subject_site, extra_budget_ok=True)

    # Competitors (parallel, capped)
    competitors = market.get('competitors_5', [])[:MAX_COMPETITORS_TO_SCRAPE]

    def scrape_comp(c):
        name=c.get('name',''); vicinity=c.get('vicinity','')
        website = get_place_website(c.get('place_id')) or discover_website_for(name, vicinity)
        rates={}
        if website:
            rates = _scrape_site_with_discovery(website, extra_budget_ok=False)
        rates={k:v for k,v in rates.items() if k in SIZE_WHITELIST}
        return {'name':name,'vicinity':vicinity,'website':website or '','rates':rates}

    competitors_data=[]
    if competitors:
        with ThreadPoolExecutor(max_workers=min(6, len(competitors))) as ex:
            futures = [ex.submit(scrape_comp, c) for c in competitors]
            for fut in as_completed(futures, timeout=TOTAL_RATE_SCRAPE_BUDGET):
                try:
                    competitors_data.append(fut.result(timeout=3))
                except Exception:
                    pass

    # Summary
    def avg(xs): return round(sum(xs)/len(xs), 2) if xs else 0
    summary={}
    for size in sorted(SIZE_WHITELIST):
        subj_cc = subject_rates.get(size,{}).get('climate')
        subj_nc = subject_rates.get(size,{}).get('non_climate')
        cc_prices=[]; nc_prices=[]
        for c in competitors_data:
            cr=c['rates'].get(size)
            if cr:
                if cr.get('climate') is not None: cc_prices.append(cr['climate'])
                if cr.get('non_climate') is not None: nc_prices.append(cr['non_climate'])
        cc_avg, cc_max = avg(cc_prices), (max(cc_prices) if cc_prices else 0)
        nc_avg, nc_max = avg(nc_prices), (max(nc_prices) if nc_prices else 0)
        inc_cc = round(((cc_max - subj_cc) / subj_cc)*100, 2) if subj_cc and cc_max else 0
        inc_nc = round(((nc_max - subj_nc) / subj_nc)*100, 2) if subj_nc and nc_max else 0
        summary[size]={
            'subject_climate': subj_cc,
            'subject_non_climate': subj_nc,
            'comp_avg_climate': cc_avg,
            'comp_max_climate': cc_max,
            'comp_avg_non_climate': nc_avg,
            'comp_max_non_climate': nc_max,
            'increase_pct_climate': inc_cc,
            'increase_pct_non_climate': inc_nc
        }

    subject_rates={k:v for k,v in subject_rates.items() if k in SIZE_WHITELIST}
    return subject_rates, competitors_data, summary


# =============================================================================
# 6) Tax stub (unchanged)
# =============================================================================
def get_tax_history(address):
    return [
        {'year': 2023, 'tax': 3200},
        {'year': 2022, 'tax': 3000},
        {'year': 2021, 'tax': 2800}
    ]


# =============================================================================
# 7) Flask view (unchanged, except storing new rate fields)
# =============================================================================
@app.route('/', methods=['GET', 'POST'])
def index():
    data = {
        'address': '', 'lat': 0, 'lng': 0,
        'county': '', 'state': '',
        'place': {}, 'cad': {}, 'llc': {}, 'owner': {}, 'owner_web': [],
        'market': {'competitors_5': [], 'count_5': 0, 'density_5': 0,
                   'competitors_10': [], 'count_10': 0, 'density_10': 0},
        'cap': 0, 'ppsf': 0, 'score': '',
        'nrsf': 0, 'listings': [], 'recommended_ppsf': 0,
        'recommended_value': 0, 'tax_records': [], 'avg_tax': 0,
        'subject_rates': {}, 'competitor_rates': [], 'rate_analysis': {}
    }
    error = None

    if request.method == 'POST':
        addr_in = request.form.get('query', '').strip()
        fac_in  = request.form.get('facility', '').strip()
        if not addr_in and not fac_in:
            error = "Enter address or facility name."
        else:
            q   = fac_in or addr_in
            geo = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={'address': q, 'key': GOOGLE_API_KEY}
            ).json()

            if geo.get('status') != 'OK':
                error = "Geocode error: " + geo.get('status', '')
            else:
                r0    = geo['results'][0]
                addr  = r0['formatted_address']
                lat   = r0['geometry']['location']['lat']
                lng   = r0['geometry']['location']['lng']
                comps = r0['address_components']
                county = next((c['long_name'].replace(' County','')
                               for c in comps if 'administrative_area_level_2' in c['types']), 'Unknown')
                state  = next((c['short_name']
                               for c in comps if 'administrative_area_level_1' in c['types']), '')

                fp = requests.get(
                    "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
                    params={
                        'input': fac_in or f"self storage near {addr}",
                        'inputtype': 'textquery',
                        'fields': 'place_id',
                        'key': GOOGLE_API_KEY
                    }
                ).json()

                place = {}
                if fp.get('candidates'):
                    pid = fp['candidates'][0]['place_id']
                    place = requests.get(
                        "https://maps.googleapis.com/maps/api/place/details/json",
                        params={
                            'place_id': pid,
                            'fields': 'name,formatted_phone_number,website,rating,user_ratings_total,opening_hours,reviews',
                            'key': GOOGLE_API_KEY
                        }
                    ).json().get('result', {})

                cad       = get_cad_details(county, state, addr)
                llc       = get_llc_info(cad.get('owner_name', ''))
                owner     = get_owner_profile(llc.get('llc_name', ''))
                owner_web = search_owner_online(cad.get('owner_name', '') or addr_in, addr)
                market    = get_market_comps(lat, lng)

                ask, inc, exp, nrsf = 1_200_000, 15_000, 5_000, 20_000
                noi  = (inc - exp) * 12
                cap  = round(noi / ask * 100, 2)
                ppsf = round(ask / nrsf, 2)
                sv   = (cap >= 7) + (ppsf < 75) + (ask < (noi / 0.07))
                score = ['Pass', 'Weak', 'Explore', 'Strong'][min(3, sv)]

                listings  = get_surrounding_listings(lat, lng)
                avg_ppsf  = round(sum(l['ppsf'] for l in listings) / len(listings), 2) if listings else 0
                rec_value = round(avg_ppsf * nrsf, 2) if listings else 0
                taxes     = get_tax_history(addr)
                avg_tax   = round(sum(r['tax'] for r in taxes) / len(taxes), 2) if taxes else 0

                # NEW: stronger, cached rate analysis
                subj_rates, comp_rates, summary = build_rate_analysis(place or {}, market)

                data.update({
                    'address': addr,
                    'lat': lat,
                    'lng': lng,
                    'county': county,
                    'state': state,
                    'place': place,
                    'cad': cad,
                    'llc': llc,
                    'owner': owner,
                    'owner_web': owner_web,
                    'market': market,
                    'cap': cap,
                    'ppsf': ppsf,
                    'score': score,
                    'nrsf': nrsf,
                    'listings': listings,
                    'recommended_ppsf': avg_ppsf,
                    'recommended_value': rec_value,
                    'tax_records': taxes,
                    'avg_tax': avg_tax,
                    'subject_rates': subj_rates,
                    'competitor_rates': comp_rates,
                    'rate_analysis': summary
                })

    return render_template('index.html', data=data, error=error, google_api_key=GOOGLE_API_KEY)

if __name__ == '__main__':
    app.run(debug=True)
