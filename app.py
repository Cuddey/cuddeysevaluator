import os
import time
import math
import re
from flask import Flask, render_template, request
import requests
from urllib.parse import quote_plus
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from googlesearch import search
from requests.exceptions import ReadTimeout

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
        for url in search(query, num_results=3):   # (fixed earlier: no pause=)
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


# === NEW: rate scraping (restricted sizes + climate detection + stricter matching) ===
SIZE_WHITELIST = {"5x5", "5x10", "10x10", "10x15", "10x20", "10x30"}

# match 5x5 / 5 x 5 / 10×20 etc.
_UNIT_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[x×]\s*(\d{1,2})(?!\d)', re.IGNORECASE)
# prices like $59, 59/mo, $125.00 per month
_PRICE_RE = re.compile(r'\$?\s?(\d{2,4})(?:\.\d{2})?\s*(?:/|\bper\b)?\s*(?:mo|month|monthly)?', re.IGNORECASE)
# hints that it's a monthly rental rate (avoid setup fees, deposits)
_RATE_HINTS = re.compile(r'(rate|rent|monthly|per\s*month|/mo|special)', re.IGNORECASE)
# climate control keywords
_CC_POS = re.compile(r'(climate|climatized|temperature|temp[-\s]*controlled|a/c|air\s*conditioned)', re.IGNORECASE)
_CC_NEG = re.compile(r'(non[-\s]*climate|non[-\s]*climatized|drive[-\s]*up|standard)', re.IGNORECASE)

def _normalize_size(w, l):
    try:
        w = int(w); l = int(l)
    except:
        pass
    size = f"{w}x{l}"
    return size if size in SIZE_WHITELIST else None

def _find_rates_in_text(text):
    """
    Returns dict:
      { size: { 'climate': price_or_None, 'non_climate': price_or_None } }
    Keeps the LOWEST advertised price found for each bucket.
    """
    out = {}
    # scan through all size mentions; use a local context window
    for m in _UNIT_RE.finditer(text):
        size = _normalize_size(m.group(1), m.group(2))
        if not size:
            continue
        start = max(m.start() - 150, 0)
        end   = min(m.end() + 250, len(text))
        window = text[start:end]

        # we only consider windows that look like rate language
        if not _RATE_HINTS.search(window):
            continue

        is_cc = _CC_POS.search(window) is not None
        is_non = _CC_NEG.search(window) is not None
        bucket = 'climate' if is_cc and not is_non else ('non_climate' if is_non and not is_cc else None)

        # collect prices in the window
        candidates = []
        for p in _PRICE_RE.findall(window):
            try:
                val = float(p)
                # plausible monthly self storage band
                if 15 <= val <= 1000:
                    candidates.append(val)
            except:
                continue
        if not candidates:
            continue
        price = min(candidates)

        out.setdefault(size, {'climate': None, 'non_climate': None})
        if bucket == 'climate':
            if out[size]['climate'] is None or price < out[size]['climate']:
                out[size]['climate'] = price
        elif bucket == 'non_climate':
            if out[size]['non_climate'] is None or price < out[size]['non_climate']:
                out[size]['non_climate'] = price
        else:
            # if indeterminate, prefer to fill whichever is empty
            if out[size]['non_climate'] is None:
                out[size]['non_climate'] = price
            elif out[size]['climate'] is None:
                out[size]['climate'] = price
            else:
                # keep the lowest of the two if this is smaller
                if price < min(out[size]['climate'], out[size]['non_climate']):
                    # replace the higher one to keep min signal
                    if out[size]['climate'] >= out[size]['non_climate']:
                        out[size]['climate'] = price
                    else:
                        out[size]['non_climate'] = price
    return out

def scrape_rates_from_website(url):
    """
    Fetch site and extract unit sizes + rates using regex (whitelist sizes only).
    Return dict size -> {'climate': price_or_None, 'non_climate': price_or_None}
    """
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        visible = soup.get_text(separator=' ', strip=True)
        # cap text length to keep it fast and avoid false positives deep in scripts
        combined = (visible[:250000]).lower()
        return _find_rates_in_text(combined)
    except Exception:
        return {}

def discover_website_for(name, vicinity, fallback_query_suffix="storage units prices"):
    query = f"{name} {vicinity} {fallback_query_suffix}"
    try:
        for url in search(query, num_results=3):
            u = url.lower()
            if any(t in u for t in [".com", ".net", ".org", ".storage"]) and not any(
                b in u for b in ["facebook.com", "yelp.com", "google.com/maps", "bing.com"]
            ):
                return url
    except Exception:
        pass
    return None

def get_place_website(place_id):
    try:
        res = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={'place_id': place_id, 'fields': 'website', 'key': GOOGLE_API_KEY},
            timeout=6
        ).json()
        return res.get('result', {}).get('website')
    except Exception:
        return None

def select_price_for_bucket(d, want_cc):
    """Pick climate/non-climate depending on desired bucket; fall back gracefully."""
    if not d:
        return None
    if want_cc:
        return d.get('climate') or d.get('non_climate')
    else:
        return d.get('non_climate') or d.get('climate')

def build_rate_analysis(subject_place, market):
    """
    Scrape subject + competitors for whitelisted sizes and climate buckets.
    Output:
      subject_rates: { size: {'climate': x, 'non_climate': y} }
      competitors: [ {name, vicinity, website, rates{size:{'climate':..,'non_climate':..}} }, ... ]
      summary: { size: { 'subject_climate': val, 'subject_non_climate': val,
                         'comp_avg_climate': val, 'comp_max_climate': val,
                         'comp_avg_non_climate': val, 'comp_max_non_climate': val,
                         'increase_pct_climate': val, 'increase_pct_non_climate': val } }
    """
    # Subject site
    subject_site = subject_place.get('website') if subject_place else None
    if not subject_site and subject_place:
        subj_name = subject_place.get('name', '')
        subject_site = discover_website_for(subj_name, "")

    subject_rates = scrape_rates_from_website(subject_site) if subject_site else {}

    # Competitors
    competitors_data = []
    for comp in market.get('competitors_5', []):
        name = comp.get('name', '')
        vicinity = comp.get('vicinity', '')
        website = get_place_website(comp.get('place_id')) or discover_website_for(name, vicinity)
        rates = scrape_rates_from_website(website) if website else {}
        # filter to whitelist only (safety)
        rates = {k: v for k, v in rates.items() if k in SIZE_WHITELIST}
        competitors_data.append({
            'name': name,
            'vicinity': vicinity,
            'website': website or '',
            'rates': rates
        })

    # All sizes in whitelist
    all_sizes = list(SIZE_WHITELIST)

    # Build summary
    summary = {}
    for size in all_sizes:
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

        def avg(xs): return round(sum(xs)/len(xs), 2) if xs else 0
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

    # Ensure subject rates only include whitelist
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
        # NEW: rate analysis structures
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

                # NEW: build rate analysis (subject + competitors) with whitelist + climate
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
                    # NEW:
                    'subject_rates': subj_rates,
                    'competitor_rates': comp_rates,
                    'rate_analysis': summary
                })

    return render_template('index.html', data=data, error=error, google_api_key=GOOGLE_API_KEY)

if __name__ == '__main__':
    app.run(debug=True)
