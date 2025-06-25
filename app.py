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

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# cad scrapers
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
        'tax_id': tax.find_next_sibling('p').get_text(strip=True) if tax else 'N/A',
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

def harris_cad(address):
    return {}

def bexar_cad(address):
    return {}

def travis_cad(address):
    return {}

cad_modules = {
    'tarrant': tarrant_cad,
    'dallas': dallas_cad,
    'harris': harris_cad,
    'bexar': bexar_cad,
    'travis': travis_cad
}

def get_cad_details(county, state, address):
    func = cad_modules.get(county.lower())
    if func:
        data = func(address)
        if data:
            return data
    query = quote_plus(f"{county} {state} Appraisal District property search")
    return {'link': f"https://www.google.com/search?q={query}"}

# llc tracing
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

# owner profile and search
def get_owner_profile(llc_name):
    return {'linkedIn': 'N/A', 'facebook': 'N/A', 'emails': [], 'phones': [], 'other_businesses': []}

def search_owner_online(owner_name, address):
    query = owner_name or address
    results = []
    try:
        for url in search(query, num_results=3, pause=2):
            html = requests.get(url, timeout=5).text
            soup = BeautifulSoup(html, 'html.parser')
            title = soup.title.string if soup.title else url
            desc_tag = soup.find('meta', attrs={'name': 'description'})
            description = desc_tag['content'] if desc_tag and desc_tag.get('content') else ''
            emails = list(set(re.findall(r'[a-zA-Z0-9.+_-]+@[a-zA-Z0-9._-]+\.[a-zA-Z]+', html)))
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

# market and competition
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
            'name': f.get('name'),
            'rating': f.get('rating'),
            'reviews': f.get('user_ratings_total'),
            'vicinity': f.get('vicinity'),
            'lat': f['geometry']['location']['lat'],
            'lng': f['geometry']['location']['lng']
        } for f in facs]
        count = len(comps)
        area = math.pi * (rad / 1609.34) ** 2
        dens = round(count / area, 2) if area else 0
        return comps, count, dens

    c5, cnt5, d5 = compute(5 * 1609.34)
    c10, cnt10, d10 = compute(10 * 1609.34)
    ids5 = {c['place_id'] for c in c5}
    new10 = [c for c in c10 if c['place_id'] not in ids5]
    return {
        'competitors_5': c5,
        'count_5': cnt5,
        'density_5': d5,
        'competitors_10': new10,
        'count_10': cnt10,
        'density_10': d10
    }

@app.route('/', methods=['GET', 'POST'])
def index():
    data = {
        'address': '',
        'lat': 0,
        'lng': 0,
        'county': '',
        'state': '',
        'place': {},
        'cad': {},
        'llc': {},
        'owner': {},
        'owner_web': [],
        'market': {
            'competitors_5': [],
            'count_5': 0,
            'density_5': 0,
            'competitors_10': [],
            'count_10': 0,
            'density_10': 0
        },
        'cap': 0,
        'ppsf': 0,
        'score': ''
    }
    error = None

    if request.method == 'POST':
        addr_in = request.form.get('query', '').strip()
        fac_in = request.form.get('facility', '').strip()
        if not addr_in and not fac_in:
            error = "Enter address or facility name."
        else:
            q = fac_in or addr_in
            geo = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={'address': q, 'key': GOOGLE_API_KEY}
            ).json()

            if geo.get('status') != 'OK':
                error = "Geocode error: " + geo.get('status', '')
            else:
                r0 = geo['results'][0]
                addr = r0['formatted_address']
                lat = r0['geometry']['location']['lat']
                lng = r0['geometry']['location']['lng']
                comps = r0['address_components']
                county = next((c['long_name'].replace(' County', '')
                               for c in comps if 'administrative_area_level_2' in c['types']), 'Unknown')
                state = next((c['short_name']
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

                cad = get_cad_details(county, state, addr)
                llc = get_llc_info(cad.get('owner_name', ''))
                owner = get_owner_profile(llc.get('llc_name', ''))
                owner_web = search_owner_online(cad.get('owner_name', '') or addr_in, addr)
                market = get_market_comps(lat, lng)

                ask, inc, exp, nrsf = 1200000, 15000, 5000, 20000
                noi = (inc - exp) * 12
                cap = round(noi / ask * 100, 2)
                ppsf = round(ask / nrsf, 2)
                sv = (cap >= 7) + (ppsf < 75) + (ask < (noi / 0.07))
                score = ['Pass', 'Weak', 'Explore', 'Strong'][min(3, sv)]

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
                    'score': score
                })

    return render_template('index.html', data=data, error=error, google_api_key=GOOGLE_API_KEY)

if __name__ == '__main__':
    app.run(debug=True)

