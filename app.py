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

# … your scraper and helper functions remain unchanged …

@app.route('/', methods=['GET', 'POST'])
def index():
    # initialize every key your template uses
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
        fac_in  = request.form.get('facility', '').strip()

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

                # overwrite the defaults with real values
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
