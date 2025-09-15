import os
from flask import Flask, render_template, request
import requests
from urllib.parse import quote_plus
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import math

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# --- 1) Multi-County CAD Scrapers ---
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
    return {'link': f"https://www.dallascad.org/SearchOwner.aspx?searchTerm={quote_plus(address)}"}

def harris_cad(address):
    return {'link': f"https://hcad.org/property-search/?searchType=address&searchTerm={quote_plus(address)}"}

def bexar_cad(address):
    return {'link': f"https://bexar.trueautomation.com/clientdb/?cid=1&unit=property&tab=search&address={quote_plus(address)}"}

def travis_cad(address):
    return {'link': f"https://propaccess.traviscad.org/clientdb/?cid=1&unit=property&tab=search&address={quote_plus(address)}"}

cad_modules = {
    'tarrant': tarrant_cad,
    'dallas': dallas_cad,
    'harris': harris_cad,
    'bexar':  bexar_cad,
    'travis': travis_cad
}

def get_cad_details(county, address):
    func = cad_modules.get(county.lower())
    return func(address) if func else {}

# --- 2) LLC & Entity Tracing ---
def get_llc_info(owner_name):
    if not owner_name:
        return {}
    try:
        oc = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={'q': owner_name, 'jurisdiction_code': 'us_tx'}
        ).json()
        candidates = oc.get('results', {}).get('companies', [])
        if not candidates:
            return {}
        comp = candidates[0]['company']
        num = comp.get('company_number')
        return {
            'llc_name': comp.get('name'),
            'formation_date': comp.get('incorporation_date'),
            'opencorporates_url': comp.get('opencorporates_url'),
            'sos_url': f"https://mycpa.cpa.state.tx.us/coa/servlet/DisplayAAE?reportingEntityId={num}"
        }
    except:
        return {}

# --- 3) Owner Profile (stub) ---
def get_owner_profile(llc_name):
    return {
        'linkedIn':     'N/A',
        'facebook':     'N/A',
        'emails':       [],
        'phones':       [],
        'other_businesses': []
    }

# --- 4) Market & Competition (5 & 10 miles, all results) ---
def get_market_comps(lat, lng):
    def fetch(radius_m):
        res = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params={
                'location': f"{lat},{lng}",
                'radius': int(radius_m),
                'type': 'storage',
                'key': GOOGLE_API_KEY
            }
        ).json()
        comps = []
        for r in res.get('results', []):
            comps.append({
                'name':     r.get('name'),
                'rating':   r.get('rating'),
                'reviews':  r.get('user_ratings_total'),
                'vicinity': r.get('vicinity')
            })
        area_sq_mi = math.pi * (radius_m / 1609.34) ** 2
        density = (len(res.get('results', [])) / area_sq_mi) if area_sq_mi else 0
        return comps, round(density, 2)

    comps5, density5   = fetch(5 * 1609.34)
    comps10, density10 = fetch(10 * 1609.34)
    return {
        'competitors_5':  comps5,
        'density_5':      density5,
        'competitors_10': comps10,
        'density_10':     density10
    }

# --- 5) Places/Geocode helpers with bias options ---
def geocode_address(text):
    return requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={'address': text, 'key': GOOGLE_API_KEY}
    ).json()

def reverse_geocode(lat, lng):
    return requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={'latlng': f"{lat},{lng}", 'key': GOOGLE_API_KEY}
    ).json()

def places_findplace(text, locationbias=None):
    params = {
        'input': text,
        'inputtype': 'textquery',
        'fields': 'place_id,geometry,formatted_address,name',
        'key': GOOGLE_API_KEY
    }
    if locationbias:
        params['locationbias'] = locationbias
    return requests.get(
        "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
        params=params
    ).json()

def places_textsearch(query, location=None, radius_m=None):
    params = {'query': query, 'region': 'us', 'key': GOOGLE_API_KEY}
    if location and radius_m:
        params['location'] = f"{location[0]},{location[1]}"
        params['radius'] = int(radius_m)
    return requests.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json",
        params=params
    ).json()

def places_nearbysearch(keyword, location, radius_m, ptype=None):
    params = {
        'keyword': keyword,
        'location': f"{location[0]},{location[1]}",
        'radius': int(radius_m),
        'key': GOOGLE_API_KEY
    }
    if ptype:
        params['type'] = ptype
    return requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params=params
    ).json()

def fetch_place_details(place_id):
    return requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            'place_id': place_id,
            'fields': 'name,formatted_phone_number,website,rating,user_ratings_total,opening_hours,reviews,photos',
            'key': GOOGLE_API_KEY
        }
    ).json()

@app.route('/', methods=['GET','POST'])
def index():
    data = {}
    error = None

    if request.method == 'POST':
        addr_input     = request.form.get('query', '').strip()
        facility_input = request.form.get('facility', '').strip()

        if not addr_input and not facility_input:
            error = "Please enter an address or facility name."
            return render_template('index.html', data=data, error=error, google_api_key=GOOGLE_API_KEY)

        addr = None
        lat = None
        lng = None
        place = {}

        # If we have an address string (even when searching by facility), geocode it for bias
        bias_latlng = None
        if addr_input:
            g_bias = geocode_address(addr_input)
            if g_bias.get('status') == 'OK' and g_bias.get('results'):
                r = g_bias['results'][0]
                bias_latlng = (r['geometry']['location']['lat'], r['geometry']['location']['lng'])

        if facility_input:
            # 1) Find Place with best available bias
            locationbias = f"circle:50000@{bias_latlng[0]},{bias_latlng[1]}" if bias_latlng else "ipbias"
            fp = places_findplace(facility_input, locationbias=locationbias)
            if fp.get('candidates'):
                c0 = fp['candidates'][0]
                addr = c0.get('formatted_address', facility_input)
                loc  = c0.get('geometry', {}).get('location', {})
                lat, lng = loc.get('lat'), loc.get('lng')
                pid = c0.get('place_id')
                if pid:
                    det = fetch_place_details(pid)
                    place = det.get('result', {})
            else:
                # 2) Text Search with bias if we have it
                ts = places_textsearch(facility_input, location=bias_latlng, radius_m=50000 if bias_latlng else None)
                if ts.get('results'):
                    r0 = ts['results'][0]
                    addr = r0.get('formatted_address', facility_input)
                    loc  = r0.get('geometry', {}).get('location', {})
                    lat, lng = loc.get('lat'), loc.get('lng')
                    pid = r0.get('place_id')
                    if pid:
                        det = fetch_place_details(pid)
                        place = det.get('result', {})
                elif bias_latlng:
                    # 3) Nearby Search as a last resort when we have a bias location
                    nb = places_nearbysearch(facility_input, bias_latlng, 50000, ptype='storage')
                    if nb.get('results'):
                        r0 = nb['results'][0]
                        addr = r0.get('vicinity', addr_input or facility_input)
                        loc  = r0.get('geometry', {}).get('location', {})
                        lat, lng = loc.get('lat'), loc.get('lng')
                        pid = r0.get('place_id')
                        if pid:
                            det = fetch_place_details(pid)
                            place = det.get('result', {})
                    else:
                        error = f"No facility found (FindPlace: {fp.get('status')}, TextSearch: {ts.get('status')}, Nearby: {nb.get('status')})."
                        return render_template('index.html', data={}, error=error, google_api_key=GOOGLE_API_KEY)
                else:
                    # 4) No bias at all, final fallback: geocode the raw text
                    g = geocode_address(facility_input)
                    if g.get('status') != 'OK':
                        error = f"No facility found (FindPlace: {fp.get('status')}, TextSearch: {ts.get('status')}, Geocode: {g.get('status')})."
                        return render_template('index.html', data={}, error=error, google_api_key=GOOGLE_API_KEY)
                    r0 = g['results'][0]
                    addr = r0['formatted_address']
                    lat  = r0['geometry']['location']['lat']
                    lng  = r0['geometry']['location']['lng']
                    place = {}
        else:
            # Address-only flow (unchanged)
            g = geocode_address(addr_input)
            if g.get('status') != 'OK':
                error = f"Geocode failed: {g.get('status')}"
                return render_template('index.html', data={}, error=error, google_api_key=GOOGLE_API_KEY)
            r0 = g['results'][0]
            addr = r0['formatted_address']
            lat  = r0['geometry']['location']['lat']
            lng  = r0['geometry']['location']['lng']

            # Attach a nearby storage business like before
            fp = places_findplace(f"self storage near {addr}")
            if fp.get('candidates'):
                pid = fp['candidates'][0]['place_id']
                det = fetch_place_details(pid)
                place = det.get('result', {})

        # County via reverse geocode
        county = "Unknown"
        try:
            rev = reverse_geocode(lat, lng)
            if rev.get('results'):
                comps = rev['results'][0].get('address_components', [])
                county = next(
                    (c['long_name'].replace(' County','')
                     for c in comps if 'administrative_area_level_2' in c['types']),
                    'Unknown'
                )
        except:
            pass

        # Remaining modules
        cad    = get_cad_details(county, addr)
        llc    = get_llc_info(cad.get('owner_name',''))
        owner  = get_owner_profile(llc.get('llc_name',''))
        market = get_market_comps(lat, lng)

        # Static example scoring (unchanged)
        asking, income, expenses, nrsf = 1_200_000, 15_000, 5_000, 20_000
        noi   = (income - expenses) * 12
        cap   = round(noi / asking * 100, 2)
        ppsf  = round(asking / nrsf, 2)
        score_val = (cap >= 7) + (ppsf < 75) + (asking < (noi / 0.07))
        score_labels = ['Pass', 'Weak', 'Explore', 'Strong']
        score = score_labels[min(3, score_val)]

        data = {
            'address': addr,
            'lat':      lat,
            'lng':      lng,
            'county':   county,
            'place':    place,
            'cad':      cad,
            'llc':      llc,
            'owner':    owner,
            'market':   market,
            'cap':      cap,
            'ppsf':     ppsf,
            'score':    score
        }

    return render_template('index.html',
                           data=data,
                           error=error,
                           google_api_key=GOOGLE_API_KEY)

if __name__ == '__main__':
    app.run(debug=True)


