import os
import time
import math
import re
from flask import Flask, render_template, request
import requests
from urllib.parse import quote_plus, urljoin, urlparse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from googlesearch import search
from requests.exceptions import ReadTimeout, Timeout, RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# 1) CAD Scrapers for Texas appraisal districts
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


# 2) LLC Tracing via OpenCorporates
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


# 3) Owner Profile stub and online search
def get_owner_profile(llc_name):
    return {
        'linkedIn': 'N/A',
        'facebook': 'N/A',
        'emails': [],
        'phones': [],
        'other_businesses': []
    }

def search_owner_online(owner_name, address):
    query = owner_name or address
    results = []
    try:
        for url in search(query, num_results=3):   # keep small to reduce latency
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


# 4) Market and competition via Google Places
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


# 5) Comparables scrapers (Crexi and LoopNet) with timeout handling
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


# === NEW: Stronger rate scraping (multi-strategy + concurrency + time budgets) ===
SIZE_WHITELIST = {"5x5", "5x10", "10x10", "10x15", "10x20", "10x30"}

_UNIT_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[x×]\s*(\d{1,2})(?!\d)', re.IGNORECASE)
_PRICE_RE = re.compile(r'\$?\s?(\d{2,4})(?:\.\d{2})?\s*(?:/|\bper\b)?\s*(?:mo|month|monthly)?', re.IGNORECASE)
_RATE_HINTS = re.compile(r'(rate|rent|monthly|per\s*month|/mo|special|price)', re.IGNORECASE)
_CC_POS = re.compile(r'(climate|climatized|temperature|temp[-\s]*controlled|a/c|air\s*conditioned)', re.IGNORECASE)
_CC_NEG = re.compile(r'(non[-\s]*climate|non[-\s]*climatized|drive[-\s]*up|standard)', re.IGNORECASE)

MAX_COMPETITORS_TO_SCRAPE = 8
PER_REQUEST_TIMEOUT = 5
MAX_BYTES = 600_000
TOTAL_RATE_SCRAPE_BUDGET = 18
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
    try:
        with requests.get(url, headers=_HEADERS, timeout=timeout, stream=True, allow_redirects=True) as r:
            ct = r.headers.get("Content-Type", "").lower()
            if "text/html" not in ct and "application/xhtml+xml" not in ct:
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
            return "".join(content)
    except (Timeout, ReadTimeout, RequestException):
        return ""
    except Exception:
        return ""

def _parse_json_ld(soup):
    rates = {}
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or s.text or "")
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            # Product with offers
            if isinstance(obj, dict) and obj.get("@type", "").lower() in ("product", "offer", "service"):
                name = (obj.get("name") or obj.get("sku") or "").lower()
                size = None
                for m in _UNIT_RE.finditer(name):
                    size = _normalize_size(m.group(1), m.group(2))
                    if size: break
                offer = obj.get("offers") or {}
                if isinstance(offer, list): offer = offer[0] if offer else {}
                price = offer.get("price") or obj.get("price")
                cc_bucket = None
                txt = json.dumps(obj).lower()
                if _CC_POS.search(txt) and not _CC_NEG.search(txt): cc_bucket = "climate"
                elif _CC_NEG.search(txt) and not _CC_POS.search(txt): cc_bucket = "non_climate"

                try:
                    if size and price:
                        price = float(re.sub(r"[^\d.]", "", str(price)))
                        rates.setdefault(size, {"climate": None, "non_climate": None})
                        if cc_bucket == "climate":
                            rates[size]["climate"] = min(price, rates[size]["climate"]) if rates[size]["climate"] else price
                        elif cc_bucket == "non_climate":
                            rates[size]["non_climate"] = min(price, rates[size]["non_climate"]) if rates[size]["non_climate"] else price
                        else:
                            # fill whichever empty
                            if rates[size]["non_climate"] is None:
                                rates[size]["non_climate"] = price
                            elif rates[size]["climate"] is None:
                                rates[size]["climate"] = price
                except:
                    pass
    return rates

def _parse_tables(soup):
    rates = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if not headers:  # some sites use first row as headers
            first = table.find("tr")
            if first:
                headers = [td.get_text(" ", strip=True).lower() for td in first.find_all(["td","th"])]
        if not headers:
            continue
        if not any("size" in h for h in headers):
            continue
        if not any(("price" in h) or ("rate" in h) for h in headers):
            continue

        rows = table.find_all("tr")
        # skip header row if it is clearly header
        start_idx = 1 if rows and any(h in rows[0].get_text(" ", strip=True).lower() for h in ("size","price","rate")) else 0
        for tr in rows[start_idx:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            if not cells or len(cells) < 2:
                continue
            row_text = " ".join(cells)
            m = _UNIT_RE.search(row_text)
            if not m: 
                continue
            size = _normalize_size(m.group(1), m.group(2))
            if not size:
                continue
            price = None
            cc_bucket = None
            joined = " ".join(cells).lower()
            if _CC_POS.search(joined) and not _CC_NEG.search(joined): cc_bucket = "climate"
            elif _CC_NEG.search(joined) and not _CC_POS.search(joined): cc_bucket = "non_climate"

            prices = _PRICE_RE.findall(joined)
            vals = []
            for p in prices:
                try:
                    v = float(p)
                    if 15 <= v <= 1000:
                        vals.append(v)
                except:
                    pass
            if vals:
                price = min(vals)

            if price is not None:
                rates.setdefault(size, {"climate": None, "non_climate": None})
                if cc_bucket == "climate":
                    rates[size]["climate"] = min(price, rates[size]["climate"]) if rates[size]["climate"] else price
                elif cc_bucket == "non_climate":
                    rates[size]["non_climate"] = min(price, rates[size]["non_climate"]) if rates[size]["non_climate"] else price
                else:
                    if rates[size]["non_climate"] is None:
                        rates[size]["non_climate"] = price
                    elif rates[size]["climate"] is None:
                        rates[size]["climate"] = price
                    else:
                        if price < min(rates[size]["climate"], rates[size]["non_climate"]):
                            if rates[size]["climate"] >= rates[size]["non_climate"]:
                                rates[size]["climate"] = price
                            else:
                                rates[size]["non_climate"] = price
    return rates

def _parse_cards(soup):
    rates = {}
    # common unit cards
    candidates = soup.select(
        "[class*='unit'], [class*='sr-unit'], [class*='stor'], [data-unit], [data-size]"
    )
    for c in candidates:
        txt = c.get_text(" ", strip=True).lower()
        m = _UNIT_RE.search(txt)
        if not m:
            continue
        size = _normalize_size(m.group(1), m.group(2))
        if not size:
            continue

        cc_bucket = None
        if _CC_POS.search(txt) and not _CC_NEG.search(txt): cc_bucket = "climate"
        elif _CC_NEG.search(txt) and not _CC_POS.search(txt): cc_bucket = "non_climate"

        prices = _PRICE_RE.findall(txt)
        vals = []
        for p in prices:
            try:
                v = float(p)
                if 15 <= v <= 1000:
                    vals.append(v)
            except:
                pass
        if not vals:
            continue
        price = min(vals)

        rates.setdefault(size, {"climate": None, "non_climate": None})
        if cc_bucket == "climate":
            rates[size]["climate"] = min(price, rates[size]["climate"]) if rates[size]["climate"] else price
        elif cc_bucket == "non_climate":
            rates[size]["non_climate"] = min(price, rates[size]["non_climate"]) if rates[size]["non_climate"] else price
        else:
            if rates[size]["non_climate"] is None:
                rates[size]["non_climate"] = price
            elif rates[size]["climate"] is None:
                rates[size]["climate"] = price
            else:
                if price < min(rates[size]["climate"], rates[size]["non_climate"]):
                    if rates[size]["climate"] >= rates[size]["non_climate"]:
                        rates[size]["climate"] = price
                    else:
                        rates[size]["non_climate"] = price
    return rates

def _regex_fallback(text):
    out = {}
    if not text:
        return out
    text = text.lower()
    for m in _UNIT_RE.finditer(text):
        size = _normalize_size(m.group(1), m.group(2))
        if not size:
            continue
        start = max(m.start() - 150, 0)
        end   = min(m.end() + 250, len(text))
        window = text[start:end]
        if not _RATE_HINTS.search(window):
            continue
        is_cc = _CC_POS.search(window) is not None
        is_non = _CC_NEG.search(window) is not None
        bucket = 'climate' if is_cc and not is_non else ('non_climate' if is_non and not is_cc else None)
        vals = []
        for p in _PRICE_RE.findall(window):
            try:
                v = float(p)
                if 15 <= v <= 1000:
                    vals.append(v)
            except:
                pass
        if not vals:
            continue
        price = min(vals)
        out.setdefault(size, {'climate': None, 'non_climate': None})
        if bucket == 'climate':
            out[size]['climate'] = min(price, out[size]['climate']) if out[size]['climate'] else price
        elif bucket == 'non_climate':
            out[size]['non_climate'] = min(price, out[size]['non_climate']) if out[size]['non_climate'] else price
        else:
            if out[size]['non_climate'] is None:
                out[size]['non_climate'] = price
            elif out[size]['climate'] is None:
                out[size]['climate'] = price
            else:
                if price < min(out[size]['climate'], out[size]['non_climate']):
                    if out[size]['climate'] >= out[size]['non_climate']:
                        out[size]['climate'] = price
                    else:
                        out[size]['non_climate'] = price
    return out

def _merge_rate_dicts(dicts):
    merged = {}
    for d in dicts:
        for k, v in d.items():
            merged.setdefault(k, {"climate": None, "non_climate": None})
            for bucket in ("climate", "non_climate"):
                val = v.get(bucket)
                if val is None:
                    continue
                if merged[k][bucket] is None or val < merged[k][bucket]:
                    merged[k][bucket] = val
    return merged

def scrape_rates_from_website(url):
    if not url:
        return {}
    txt = _safe_fetch_text(url)
    if not txt:
        return {}

    try:
        soup = BeautifulSoup(txt, 'html.parser')
        visible = soup.get_text(separator=' ', strip=True)
        combined = (visible[:500_000]).lower()
    except Exception:
        combined = re.sub(r'<[^>]+>', ' ', txt)[:500_000].lower()
        soup = None

    candidates = []

    # 1) JSON-LD
    if soup is not None:
        try:
            candidates.append(_parse_json_ld(soup))
        except Exception:
            pass

    # 2) Vendor-style JSON blobs
    if soup is not None:
        for s in soup.find_all("script"):
            raw = (s.string or s.text or "").strip()
            if not raw:
                continue
            if any(key in raw for key in ("__NUXT__", "__PRELOADED_STATE__", "INITIAL_STATE", "window.__", "nuxtState")):
                try:
                    # coarse clean for "window.__STATE__ = {...}" styles
                    json_txt = raw
                    json_txt = re.sub(r"^[^{\[]+", "", json_txt)   # strip leading code to the first { or [
                    json_txt = re.sub(r";\s*$", "", json_txt)
                    data = json.loads(json_txt)
                    # walk the JSON to find any items containing size+price text
                    def walk(o):
                        local_rates = {}
                        if isinstance(o, dict):
                            txt = json.dumps(o).lower()
                            # find sizes in the string form
                            for m in _UNIT_RE.finditer(txt):
                                size = _normalize_size(m.group(1), m.group(2))
                                if not size:
                                    continue
                                prices = _PRICE_RE.findall(txt)
                                vals = []
                                for p in prices:
                                    try:
                                        v = float(p)
                                        if 15 <= v <= 1000:
                                            vals.append(v)
                                    except:
                                        pass
                                if vals:
                                    cc_bucket = None
                                    if _CC_POS.search(txt) and not _CC_NEG.search(txt): cc_bucket = "climate"
                                    elif _CC_NEG.search(txt) and not _CC_POS.search(txt): cc_bucket = "non_climate"
                                    price = min(vals)
                                    local_rates.setdefault(size, {"climate": None, "non_climate": None})
                                    if cc_bucket == "climate":
                                        local_rates[size]["climate"] = price
                                    elif cc_bucket == "non_climate":
                                        local_rates[size]["non_climate"] = price
                                    else:
                                        if local_rates[size]["non_climate"] is None:
                                            local_rates[size]["non_climate"] = price
                                        elif local_rates[size]["climate"] is None:
                                            local_rates[size]["climate"] = price
                                    # don't break—there might be more sizes
                            for v in o.values():
                                sub = walk(v)
                                if sub:
                                    local_rates = _merge_rate_dicts([local_rates, sub])
                        elif isinstance(o, list):
                            local_rates = {}
                            for it in o:
                                sub = walk(it)
                                if sub:
                                    local_rates = _merge_rate_dicts([local_rates, sub])
                        else:
                            return {}
                        return local_rates
                    found = walk(data)
                    if found:
                        candidates.append(found)
                except Exception:
                    pass

    # 3) Table extraction
    if soup is not None:
        try:
            candidates.append(_parse_tables(soup))
        except Exception:
            pass

    # 4) Card extraction
    if soup is not None:
        try:
            candidates.append(_parse_cards(soup))
        except Exception:
            pass

    # 5) Regex fallback on combined text
    try:
        candidates.append(_regex_fallback(combined))
    except Exception:
        pass

    merged = _merge_rate_dicts(candidates)
    # whitelist filter
    merged = {k: v for k, v in merged.items() if k in SIZE_WHITELIST}
    return merged

def discover_website_for(name, vicinity, fallback_query_suffix="storage units prices"):
    query = f"{name} {vicinity} {fallback_query_suffix}".strip()
    try:
        for url in search(query, num_results=3):
            u = url.lower()
            if any(t in u for t in [".com", ".net", ".org", ".storage"]) and not any(
                b in u for b in ["facebook.com", "yelp.com", "google.com/maps", "bing.com", "yellowpages", "yahoo"]
            ):
                return url
    except Exception:
        pass
    return None

def _try_discovery_paths(base_url, time_left):
    urls = []
    try:
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return []
        for path in DISCOVERY_PATHS:
            urls.append(urljoin(base_url, path))
    except Exception:
        return []
    # fetch quickly, keep only pages that return non-empty html
    good = []
    deadline = time.time() + max(0, time_left)
    for u in urls:
        if time.time() > deadline:
            break
        txt = _safe_fetch_text(u, timeout=min(PER_REQUEST_TIMEOUT, max(1, int(deadline - time.time()))))
        if txt and len(txt) > 500:
            good.append(u)
    return good

def get_place_website(place_id):
    try:
        res = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={'place_id': place_id, 'fields': 'website', 'key': GOOGLE_API_KEY},
            timeout=PER_REQUEST_TIMEOUT
        ).json()
        return res.get('result', {}).get('website')
    except Exception:
        return None

def _scrape_site_with_discovery(base_url, time_budget_sec):
    start = time.time()
    all_rates = []
    # 1) base page first
    base_rates = scrape_rates_from_website(base_url)
    if base_rates:
        all_rates.append(base_rates)

    time_left = time_budget_sec - (time.time() - start)
    if time_left <= 0:
        return _merge_rate_dicts(all_rates)

    # 2) try discovery paths in parallel (fast)
    discover_urls = _try_discovery_paths(base_url, time_left)
    if not discover_urls:
        return _merge_rate_dicts(all_rates)

    time_left = time_budget_sec - (time.time() - start)
    if time_left <= 0:
        return _merge_rate_dicts(all_rates)

    per_task_budget = max(2, int(time_left / len(discover_urls)))
    with ThreadPoolExecutor(max_workers=min(4, len(discover_urls))) as ex:
        futures = {ex.submit(scrape_rates_from_website, u): u for u in discover_urls}
        for fut in as_completed(futures):
            rates = {}
            try:
                rates = fut.result(timeout=per_task_budget)
            except Exception:
                pass
            if rates:
                all_rates.append(rates)

    return _merge_rate_dicts(all_rates)

def build_rate_analysis(subject_place, market):
    """
    Scrape subject + up to N competitors with a total time budget.
    """
    start_time = time.time()

    # Subject site
    subject_site = subject_place.get('website') if subject_place else None
    if not subject_site and subject_place:
        subj_name = subject_place.get('name', '')
        if time.time() - start_time < TOTAL_RATE_SCRAPE_BUDGET:
            subject_site = discover_website_for(subj_name, "")

    subject_rates = {}
    if subject_site and (time.time() - start_time < TOTAL_RATE_SCRAPE_BUDGET):
        # give subject a bit more budget than a single page because it matters most
        subject_rates = _scrape_site_with_discovery(subject_site, time_budget_sec=8)

    # Competitors
    competitors = market.get('competitors_5', [])[:MAX_COMPETITORS_TO_SCRAPE]

    def scrape_comp(comp):
        name = comp.get('name', '')
        vicinity = comp.get('vicinity', '')
        website = get_place_website(comp.get('place_id')) or discover_website_for(name, vicinity)
        rates = {}
        if website:
            rates = scrape_rates_from_website(website)
            # if very sparse, try discovery paths quickly
            filled = any(v.get('climate') or v.get('non_climate') for v in rates.values())
            if not filled:
                rates = _merge_rate_dicts([rates, _scrape_site_with_discovery(website, time_budget_sec=5)])
        rates = {k: v for k, v in rates.items() if k in SIZE_WHITELIST}
        return {
            'name': name,
            'vicinity': vicinity,
            'website': website or '',
            'rates': rates
        }

    competitors_data = []
    time_left = TOTAL_RATE_SCRAPE_BUDGET - (time.time() - start_time)
    if time_left > 0 and competitors:
        # split remaining budget across competitors in parallel
        max_workers = min(6, len(competitors))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(scrape_comp, c): c for c in competitors}
            for fut in as_completed(futures, timeout=max(5, int(time_left))):
                try:
                    res = fut.result(timeout=3)
                    competitors_data.append(res)
                except Exception:
                    # ignore slow/failed competitor
                    pass

    # Build summary table
    def avg(xs): return round(sum(xs)/len(xs), 2) if xs else 0
    summary = {}
    for size in sorted(list(SIZE_WHITELIST)):
        subj_cc = subject_rates.get(size, {}).get('climate')
        subj_nc = subject_rates.get(size, {}).get('non_climate')

        comp_cc_prices = []
        comp_nc_prices = []
        for c in competitors_data:
            cr = c['rates'].get(size)
            if cr:
                if cr.get('climate') is not None:
                    comp_cc_prices.append(cr['climate'])
                if cr.get('non_climate') is not None:
                    comp_nc_prices.append(cr['non_climate'])

        cc_avg, cc_max = avg(comp_cc_prices), (max(comp_cc_prices) if comp_cc_prices else 0)
        nc_avg, nc_max = avg(comp_nc_prices), (max(comp_nc_prices) if comp_nc_prices else 0)

        inc_cc = round(((cc_max - subj_cc) / subj_cc) * 100, 2) if subj_cc and cc_max else 0
        inc_nc = round(((nc_max - subj_nc) / subj_nc) * 100, 2) if subj_nc and nc_max else 0

        summary[size] = {
            'subject_climate': subj_cc,
            'subject_non_climate': subj_nc,
            'comp_avg_climate': cc_avg,
            'comp_max_climate': cc_max,
            'comp_avg_non_climate': nc_avg,
            'comp_max_non_climate': nc_max,
            'increase_pct_climate': inc_cc,
            'increase_pct_non_climate': inc_nc
        }

    subject_rates = {k: v for k, v in subject_rates.items() if k in SIZE_WHITELIST}
    return subject_rates, competitors_data, summary
# === END NEW ===


# 6) Tax history stub
def get_tax_history(address):
    return [
        {'year': 2023, 'tax': 3200},
        {'year': 2022, 'tax': 3000},
        {'year': 2021, 'tax': 2800}
    ]


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
