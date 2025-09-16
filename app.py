import os
import time
import math
import re
import json
from flask import Flask, render_template, request
import requests
from urllib.parse import quote_plus, urlparse, urljoin
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from googlesearch import search
from requests.exceptions import ReadTimeout, Timeout, RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# ===========================
# Tiny in-memory TTL cache
# ===========================
_CACHE = {}
CACHE_TTL_SEC = 6 * 60 * 60
CACHE_MAX_KEYS = 1000

def _cache_get(key):
    v = _CACHE.get(key)
    if not v:
        return None
    val, ts = v
    if time.time() - ts > CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return val

def _cache_set(key, val):
    try:
        if len(_CACHE) > CACHE_MAX_KEYS:
            for k, _ in list(sorted(_CACHE.items(), key=lambda kv: kv[1][1]))[: max(1, CACHE_MAX_KEYS // 10)]:
                _CACHE.pop(k, None)
        _CACHE[key] = (val, time.time())
    except Exception:
        pass


# ====================================
# 1) CAD Scrapers for Texas districts
# ====================================
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


# =========================
# 2) LLC & Owner stubs
# =========================
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


# =====================================
# 4) Market & competition (Google Places)
# =====================================
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


# =====================================
# 5) Nearby Listings on CREXI/LoopNet
# =====================================
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


# =====================================
# 6) Tax history stub
# =====================================
def get_tax_history(address):
    return [
        {'year': 2023, 'tax': 3200},
        {'year': 2022, 'tax': 3000},
        {'year': 2021, 'tax': 2800}
    ]


# =======================================================
# === NEW: Accurate standard rate scraping (headless) ===
# =======================================================

# Focus sizes and climate detection
SIZE_WHITELIST = {"5x5", "5x10", "10x10", "10x15", "10x20", "10x30"}
UNIT_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[x×]\s*(\d{1,2})(?!\d)', re.IGNORECASE)
PRICE_RE = re.compile(r'\$?\s?(\d{2,4})(?:\.\d{2})?', re.IGNORECASE)
CC_POS = re.compile(r'(climate|climatized|temperature|temp[-\s]*controlled|a/c|air\s*conditioned)', re.IGNORECASE)
CC_NEG = re.compile(r'(non[-\s]*climate|non[-\s]*climatized|drive[-\s]*up|standard)', re.IGNORECASE)
STRIKE_HINTS = re.compile(r'(was|regular|in[-\s]*store|strikethrough|strike|line[-\s]*through|list price)', re.IGNORECASE)
DISCOUNT_HINTS = re.compile(r'(now|online|special|promo|discount|sale|deal|today|limited|save|% off|first month|1st month|\$1)', re.IGNORECASE)

HEADLESS_RATES = os.getenv("HEADLESS_RATES", "1").strip() != "0"

# Selenium setup (lazy)
_SELENIUM_OK = None
_driver_path_hint = os.getenv("CHROMEDRIVER_PATH")  # optional


def _have_selenium():
    global _SELENIUM_OK
    if _SELENIUM_OK is not None:
        return _SELENIUM_OK
    try:
        from selenium import webdriver  # noqa
        from selenium.webdriver.chrome.options import Options  # noqa
        _SELENIUM_OK = True
    except Exception:
        _SELENIUM_OK = False
    return _SELENIUM_OK


def _headless_html(url, timeout=12):
    """Fetch fully rendered HTML via Selenium headless. Returns '' on failure."""
    if not HEADLESS_RATES or not _have_selenium():
        return ""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1200,2000")
        # Try selenium-manager (Selenium 4.6+) to auto-manage driver:
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            # wait for something meaningful to render
            try:
                WebDriverWait(driver, 6).until(
                    EC.presence_of_all_elements_located((By.TAG_NAME, "body"))
                )
            except Exception:
                pass
            html = driver.page_source or ""
            return html
        finally:
            driver.quit()
    except Exception:
        return ""


def _http_html(url, timeout=10, max_bytes=900_000):
    """Fast HTTP fetch (no JS)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        with requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ct:
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
    except Exception:
        return ""


def _normalize_size(w, l):
    try:
        w = int(w); l = int(l)
    except Exception:
        return None
    s = f"{w}x{l}"
    return s if s in SIZE_WHITELIST else None


def _extract_standard_price_from_window(txt):
    """Given a small text window near a size, return (standard_price, climate_flag) or (None, None)."""
    if not txt:
        return None, None
    low = txt.lower()

    # Climate flag
    cc = None
    if CC_POS.search(low) and not CC_NEG.search(low):
        cc = True
    elif CC_NEG.search(low) and not CC_POS.search(low):
        cc = False

    # Collect all price-like numbers in the window
    prices = []
    for m in PRICE_RE.findall(low):
        try:
            v = float(m)
            if 15 <= v <= 1000:
                prices.append(v)
        except Exception:
            pass
    if not prices:
        return None, cc

    # Heuristics:
    # - If we detect strikethrough/was/regular: the **standard** is usually the **higher** value.
    # - If we detect discount/now/online special: prefer the **higher** value as standard.
    # - If only one price: treat it as standard.
    if STRIKE_HINTS.search(low) or DISCOUNT_HINTS.search(low):
        return max(prices), cc
    return max(prices), cc


def _parse_rates_from_html(html):
    """Return dict: {size: {'climate': price_or_None, 'non_climate': price_or_None}} – only standard rates."""
    out = {}
    if not html:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")
        raw_text = soup.get_text(separator=" ", strip=True)
    except Exception:
        raw_text = re.sub(r'<[^>]+>', ' ', html)

    low = raw_text.lower()

    # For each size in whitelist, search windows around the match
    for m in UNIT_RE.finditer(low):
        size = _normalize_size(m.group(1), m.group(2))
        if not size:
            continue
        start = max(0, m.start() - 200)
        end   = min(len(low), m.end() + 250)
        win = low[start:end]

        price, cc = _extract_standard_price_from_window(win)
        if price is None:
            continue

        out.setdefault(size, {"climate": None, "non_climate": None})
        if cc is True:
            out[size]["climate"] = price if out[size]["climate"] is None else min(out[size]["climate"], price)
        elif cc is False:
            out[size]["non_climate"] = price if out[size]["non_climate"] is None else min(out[size]["non_climate"], price)
        else:
            # unknown climate – fill non_climate first, then climate
            if out[size]["non_climate"] is None:
                out[size]["non_climate"] = price
            elif out[size]["climate"] is None:
                out[size]["climate"] = price

    return out


def scrape_rates_from_website(url):
    """Fetch a site's page(s) and extract standard (non-discount) rates."""
    if not url:
        return {}

    ck = f"rates:{url}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    # Try headless first, then HTTP
    html = _headless_html(url)
    if not html:
        html = _http_html(url)

    # Try common pricing paths if base page fails to produce rates
    candidates = [url]
    for path in ("/units", "/rent", "/storage-units", "/self-storage", "/pricing", "/rates", "/rent-online"):
        try:
            base = f"{url.rstrip('/')}"
            candidates.append(base + path)
        except Exception:
            pass

    merged = {}
    # short budget for secondary pages
    for i, u in enumerate(candidates[:4]):
        h = html if i == 0 else (_headless_html(u) or _http_html(u))
        if not h:
            continue
        rates = _parse_rates_from_html(h)
        # merge, keep min price for each bucket (more conservative)
        for size, buckets in rates.items():
            if size not in SIZE_WHITELIST:
                continue
            merged.setdefault(size, {"climate": None, "non_climate": None})
            for b in ("climate", "non_climate"):
                val = buckets.get(b)
                if val is None:
                    continue
                if merged[size][b] is None or val < merged[size][b]:
                    merged[size][b] = val

    _cache_set(ck, merged)
    return merged


def _domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_place_website(place_id):
    ck = f"place_site:{place_id}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        res = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={'place_id': place_id, 'fields': 'website,url', 'key': GOOGLE_API_KEY},
            timeout=6
        ).json()
        site = res.get('result', {}).get('website') or res.get('result', {}).get('url')
        _cache_set(ck, site)
        return site
    except Exception:
        _cache_set(ck, None)
        return None


def discover_website_for(name, vicinity):
    """Fallback website discovery via web search (skip aggregators)."""
    query = f"{name} {vicinity} storage website"
    ck = f"discover:{query}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        for url in search(query, num_results=3):
            u = url.lower()
            if any(b in u for b in ["facebook.com", "yelp.com", "google.com/maps", "bing.com", "yellowpages", "sparefoot", "selfstorage.com", "storage.com"]):
                continue
            _cache_set(ck, url)
            return url
    except Exception:
        pass
    _cache_set(ck, None)
    return None


def build_rate_analysis(subject_place, market):
    """Return (subject_rates, competitor_rates_list, summary_by_size)."""
    # Subject
    subject_site = subject_place.get('website') if subject_place else None
    if not subject_site and subject_place:
        subject_site = discover_website_for(subject_place.get('name', ''), subject_place.get('formatted_address', ''))

    subject_rates = scrape_rates_from_website(subject_site) if subject_site else {}

    # Competitors (all within 5 miles); include even if no website
    competitors_raw = market.get('competitors_5', [])
    MAX_COMP = 12  # cover all typical sites in dense markets without timeouts
    competitors = competitors_raw[:MAX_COMP]

    def scrape_comp(c):
        name = c.get('name', '')
        vicinity = c.get('vicinity', '')
        website = get_place_website(c.get('place_id')) or discover_website_for(name, vicinity)
        if website:
            rates = scrape_rates_from_website(website)
        else:
            rates = {}
        # Only self-storage: light filter by name keywords (optional; Places type is already 'storage')
        return {
            'name': name,
            'vicinity': vicinity,
            'website': website or '',
            'rates': {k: v for k, v in rates.items() if k in SIZE_WHITELIST}
        }

    comp_data = []
    if competitors:
        with ThreadPoolExecutor(max_workers=min(6, len(competitors))) as ex:
            futures = [ex.submit(scrape_comp, c) for c in competitors]
            for f in as_completed(futures, timeout=20):
                try:
                    comp_data.append(f.result())
                except Exception:
                    pass

    # Summary (subject vs comp averages & max)
    def avg(xs): return round(sum(xs) / len(xs), 2) if xs else 0
    summary = {}
    for size in sorted(SIZE_WHITELIST):
        subj_cc = subject_rates.get(size, {}).get('climate')
        subj_nc = subject_rates.get(size, {}).get('non_climate')
        cc_prices, nc_prices = [], []
        for c in comp_data:
            r = c['rates'].get(size)
            if not r:
                continue
            if r.get('climate') is not None:
                cc_prices.append(r['climate'])
            if r.get('non_climate') is not None:
                nc_prices.append(r['non_climate'])
        cc_avg, cc_max = avg(cc_prices), (max(cc_prices) if cc_prices else 0)
        nc_avg, nc_max = avg(nc_prices), (max(nc_prices) if nc_prices else 0)
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
    return subject_rates, comp_data, summary


# =====================================
# 7) Flask view (kept intact; only adds rate fields)
# =====================================
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
                            'fields': 'name,formatted_phone_number,website,rating,user_ratings_total,opening_hours,reviews,formatted_address',
                            'key': GOOGLE_API_KEY
                        }
                    ).json().get('result', {})

                cad       = get_cad_details(county, state, addr)
                llc       = get_llc_info(cad.get('owner_name', ''))
                owner     = get_owner_profile(llc.get('llc_name', ''))
                owner_web = search_owner_online(cad.get('owner_name', '') or addr_in, addr)
                market    = get_market_comps(lat, lng)

                # Deal score stub (unchanged)
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

                # NEW: subject + competitors standard rate analysis
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
