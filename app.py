import os
import time
import math
from flask import Flask, render_template, request
import requests
from urllib.parse import quote_plus
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = Flask(__name__)

# ================================
# CAD Scrapers (same as before)
# ================================
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

def get_cad_details(county, state, address):
    if county.lower() == "tarrant":
        return tarrant_cad(address)
    elif county.lower() == "dallas":
        return dallas_cad(address)
    return {}

# ================================
# Market competition (Google Places)
# ================================
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
    return {
        'competitors_5':  c5,
        'count_5':        cnt5,
        'density_5':      d5,
        'competitors_10': c10,
        'count_10':       cnt10,
        'density_10':     d10
    }

# ================================
# Flask Route
# ================================
@app.route('/', methods=['GET', 'POST'])
def index():
    data = {
        'address': '', 'lat': 0, 'lng': 0,
        'county': '', 'state': '',
        'place': {}, 'cad': {}, 'market': {},
        'manual_rates': {}, 'summary': {}
    }
    error = None

    if request.method == 'POST':
        # Handle manual rate input first
        if "manual_entry" in request.form:
            sizes = ["5x5","5x10","10x10","10x15","10x20","10x30"]
            subject = {}
            comps = []

            # subject property rates
            for s in sizes:
                nc = request.form.get(f"subject_{s}_nc")
                cc = request.form.get(f"subject_{s}_cc")
                subject[s] = {
                    "non_climate": float(nc) if nc else None,
                    "climate": float(cc) if cc else None
                }

            # competitors (max 5 for now)
            for i in range(1, 6):
                cname = request.form.get(f"comp{i}_name")
                if not cname:
                    continue
                comp = {"name": cname, "rates": {}}
                for s in sizes:
                    nc = request.form.get(f"comp{i}_{s}_nc")
                    cc = request.form.get(f"comp{i}_{s}_cc")
                    comp["rates"][s] = {
                        "non_climate": float(nc) if nc else None,
                        "climate": float(cc) if cc else None
                    }
                comps.append(comp)

            # Calculate summary
            summary = {}
            for s in sizes:
                subj_nc = subject[s]["non_climate"]
                subj_cc = subject[s]["climate"]
                nc_prices = [c["rates"][s]["non_climate"] for c in comps if c["rates"][s]["non_climate"]]
                cc_prices = [c["rates"][s]["climate"] for c in comps if c["rates"][s]["climate"]]

                summary[s] = {
                    "subject_non_climate": subj_nc,
                    "subject_climate": subj_cc,
                    "comp_avg_nc": round(sum(nc_prices)/len(nc_prices),2) if nc_prices else None,
                    "comp_max_nc": max(nc_prices) if nc_prices else None,
                    "comp_avg_cc": round(sum(cc_prices)/len(cc_prices),2) if cc_prices else None,
                    "comp_max_cc": max(cc_prices) if cc_prices else None,
                    "increase_pct_nc": round(((max(nc_prices)-subj_nc)/subj_nc)*100,2) if subj_nc and nc_prices else None,
                    "increase_pct_cc": round(((max(cc_prices)-subj_cc)/subj_cc)*100,2) if subj_cc and cc_prices else None,
                }

            data["manual_rates"] = {"subject": subject, "competitors": comps}
            data["summary"] = summary

        else:
            # Handle normal facility search (same as before)
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
                if geo.get('status') == 'OK':
                    r0 = geo['results'][0]
                    addr  = r0['formatted_address']
                    lat   = r0['geometry']['location']['lat']
                    lng   = r0['geometry']['location']['lng']
                    comps = r0['address_components']
                    county = next((c['long_name'].replace(' County','')
                                for c in comps if 'administrative_area_level_2' in c['types']), 'Unknown')
                    state  = next((c['short_name']
                                for c in comps if 'administrative_area_level_1' in c['types']), '')
                    cad = get_cad_details(county, state, addr)
                    market = get_market_comps(lat, lng)
                    data.update({
                        "address": addr, "lat": lat, "lng": lng,
                        "county": county, "state": state,
                        "cad": cad, "market": market
                    })

    return render_template("index.html", data=data, error=error, google_api_key=GOOGLE_API_KEY)

if __name__ == '__main__':
    app.run(debug=True)
