#!/usr/bin/env python3
"""Catalogo nutrizionale SaveTheFoods. Python 3.9+, requests, beautifulsoup4.
Uso: python savethefoods.py [--offline] [--no-browser] [--cartella PATH]
Ogni esecuzione normale aggiorna il catalogo. --offline riapre i dati salvati.
"""
import argparse
import csv
import html
import io
import json
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    raise SystemExit('Installa le dipendenze: python -m pip install requests beautifulsoup4')

BASE = 'https://www.savethefoods.it'
API = BASE + '/wp-json/wc/store/v1/products'
FIELDS = {
    'calorie_kcal': r'(?:energia|valore energetico|energy)',
    'grassi_g': r'(?:grassi|lipidi|fat)',
    'grassi_saturi_g': r'(?:(?:di cui\s*)?(?:acidi grassi\s*)?saturi|saturates)',
    'carboidrati_g': r'(?:carboidrati|carbohydrates)',
    'zuccheri_g': r'(?:(?:di cui\s*)?zuccheri|sugars)',
    'proteine_g': r'(?:proteine|proteins?)',
    'fibre_g': r'(?:fibr[ae](?: alimentari)?|fib(?:re|er))',
    'sale_g': r'(?:sale|salt)',
}
NUMBER = r'(?P<op><=|>=|<|>|≤|≥|≈|~)?\s*(?P<num>\d+(?:[.,]\d+)*)'
LOCAL = threading.local()


def session():
    if not hasattr(LOCAL, 'session'):
        s = requests.Session()
        s.headers.update({'User-Agent': 'SaveTheFoodsNutrition/2.0 (personal catalog reader)', 'Accept-Language': 'it-IT,it;q=0.9'})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'], respect_retry_after_header=True)
        s.mount('https://', HTTPAdapter(max_retries=retry))
        LOCAL.session = s
    return LOCAL.session


def get(url, **kwargs):
    if urlparse(url).hostname not in ('savethefoods.it', 'www.savethefoods.it'):
        raise ValueError('URL fuori dal catalogo SaveTheFoods')
    r = session().get(url, timeout=(15, 45), **kwargs)
    r.raise_for_status()
    return r


def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(str(value or ''))).strip()


def text(node):
    return clean(node.get_text(' ', strip=True)) if node else ''


def numeric(s):
    s = s.replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    else:
        s = s.replace(',', '.')
        if s.count('.') > 1:
            raise ValueError('Numero ambiguo: ' + s)
    return float(s)


def nutrient_value(raw, energy=False):
    # Preserve comparisons: <0.5 is not an exact 0.5 and is never changed to zero.
    unit = r'kcal' if energy else r'(?:mg|g)'
    m = re.search(('' if energy else r'^\s*') + NUMBER + r'\s*(?P<unit>' + unit + r')\b', raw, re.I)
    if not m:
        return None, ''
    try:
        value = numeric(m['num'])
    except ValueError:
        return None, ''
    if m['unit'].lower() == 'mg':
        value /= 1000
    return value, (m['op'] or '').replace('<=', '≤').replace('>=', '≥').replace('~', '≈')


def basis(raw):
    m = re.search(r'(?:per|/|su)\s*100\s*(ml|g(?:r(?:ammi)?)?)\b', raw, re.I)
    return ('100 ml' if m[1].lower() == 'ml' else '100 g') if m else ''


def parse_nutrition(soup):
    """Read product-only tables, then legacy prose; never related products."""
    candidates = []
    for table in soup.select('.envision-nutrition__table, .woocommerce-Tabs-panel table, #tab-description table'):
        raw = text(table)
        if re.search(r'energia|kcal|proteine', raw, re.I):
            parent = table.find_parent(class_='envision-nutrition') or table.parent
            candidates.append((table, raw, basis(raw) or basis(text(parent)), 'tabella'))
    desc = soup.select_one('#tab-description')
    if desc:
        raw = text(desc)
        m = re.search(r'(?:valori|informazioni|dichiarazione)\s+nutrizional[ei]', raw, re.I)
        if m:
            raw = raw[m.start():]
            candidates.append((None, raw, basis(raw), 'descrizione'))
    # An API description can be wrapped by the caller in #tab-description.
    output = {k: None for k in FIELDS}
    output.update({k + '_limite': '' for k in FIELDS})
    output.update(base_nutrizionale='', fonte_nutrizione='', testo_nutrizionale='', note=[])
    parsed = []
    for table, raw, base, source in candidates:
        vals = {}
        if table:
            headers = table.select('thead th, thead td')
            # On multi-column labels select the explicitly named per-100 column.
            col = next((i for i, h in enumerate(headers) if basis(text(h))), 1)
            for row in table.select('tr'):
                cells = row.find_all(['td', 'th'], recursive=False)
                if len(cells) <= col:
                    continue
                label = text(cells[0]).rstrip(':').strip()
                for key, pattern in FIELDS.items():
                    if re.fullmatch(pattern, label, re.I):
                        vals[key] = nutrient_value(text(cells[col]), key == 'calorie_kcal')
        else:
            for key, pattern in FIELDS.items():
                # Limit energy to its own segment; don't steal another product's kcal.
                tail = r'\s*[:：]?\s*([^;–—\n]{0,90})'
                m = re.search(r'\b' + pattern + tail, raw, re.I)
                if m:
                    vals[key] = nutrient_value(m[1], key == 'calorie_kcal')
        if any(v[0] is not None for v in vals.values()):
            parsed.append((base, source, raw, vals))
    if parsed:
        chosen_base = parsed[0][0]
        output.update(base_nutrizionale=chosen_base, fonte_nutrizione=parsed[0][1], testo_nutrizionale=parsed[0][2])
        for base, source, raw, vals in parsed:
            if base != chosen_base:
                output['note'].append('Basi nutrizionali discordanti: controllare la scheda')
                continue
            for key, (value, op) in vals.items():
                if value is None:
                    output['note'].append('Valore non interpretabile: ' + key)
                    continue
                if output[key] is None:
                    output[key], output[key + '_limite'] = value, op
                elif abs(output[key] - value) > .011 or output[key + '_limite'] != op:
                    output['note'].append('Valori discordanti tra tabella e descrizione')
        if not chosen_base:
            output['note'].append('Base nutrizionale non dichiarata: confronto per 100 non disponibile')
    return output


def metrics(record):
    def exact(key):
        return record.get(key) if not record.get(key + '_limite') else None
    def ratio(a, b, scale=1):
        x, y = exact(a), exact(b)
        return round(scale * x / y, 5) if x is not None and y is not None and y > 0 and record.get('base_nutrizionale') else None
    record['proteine_100kcal'] = ratio('proteine_g', 'calorie_kcal', 100)
    record['fibre_100kcal'] = ratio('fibre_g', 'calorie_kcal', 100)
    record['rapporto_grassi_proteine'] = ratio('grassi_g', 'proteine_g')
    record['zuccheri_su_carboidrati_pct'] = ratio('zuccheri_g', 'carboidrati_g', 100)
    notes = record.setdefault('note', [])
    for a, b in [('grassi_saturi_g', 'grassi_g'), ('zuccheri_g', 'carboidrati_g')]:
        if exact(a) is not None and exact(b) is not None and exact(a) > exact(b) + .2:
            notes.append('Dato del sito incoerente: ' + a + ' > ' + b)
    for key in FIELDS:
        val = record.get(key)
        if val is not None and record.get('base_nutrizionale') and val > (950 if key == 'calorie_kcal' else 100):
            notes.append('Valore da verificare sul sito: ' + key)
    energy = re.search(r'(\d+(?:[.,]\d+)?)\s*kJ', record.get('testo_nutrizionale', ''), re.I)
    if energy and exact('calorie_kcal') is not None:
        kj_kcal = numeric(energy[1]) / 4.184
        if abs(kj_kcal - exact('calorie_kcal')) > max(5, kj_kcal * .08):
            notes.append('Energia in kJ e kcal discordante sul sito')
    if notes:
        for key in ['proteine_100kcal', 'fibre_100kcal', 'rapporto_grassi_proteine', 'zuccheri_su_carboidrati_pct']:
            record[key] = None
    record['note'] = list(dict.fromkeys(notes))
    record['nutrizione_presente'] = any(record.get(k) is not None for k in FIELDS)
    return record


def catalog_api():
    result, seen = [], set()
    page = 1
    while True:
        response = get(API, params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'})
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError('Risposta del catalogo non riconosciuta')
        if not batch:
            break
        new = [p for p in batch if p.get('id') not in seen]
        if not new:
            raise ValueError('La paginazione restituisce sempre gli stessi prodotti')
        for p in new:
            if not p.get('permalink') or not p.get('name'):
                raise ValueError('Struttura del catalogo cambiata')
            seen.add(p['id'])
            result.append(p)
        print(f'Catalogo: {len(result)} prodotti', flush=True)
        total_pages = response.headers.get('X-WP-TotalPages')
        if (total_pages and page >= int(total_pages)) or (not total_pages and len(batch) < 100):
            break
        page += 1
        if page > 100:
            raise ValueError('Troppe pagine nel catalogo')
        time.sleep(.4)
    if not result:
        raise ValueError('Catalogo vuoto; i dati precedenti non saranno sovrascritti')
    return result


def catalog_sitemap():
    # Fallback if the public Store API is disabled. Walk product sitemap indexes.
    pending = [BASE + '/sitemap_index.xml']
    seen, urls = set(), set()
    while pending:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        root = ElementTree.fromstring(get(url).content)
        ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        if root.tag.endswith('sitemapindex'):
            pending += [e.text for e in root.findall(f'{ns}sitemap/{ns}loc') if e.text and 'product' in e.text and 'category' not in e.text and 'tag' not in e.text]
        else:
            urls.update(e.text for e in root.findall(f'{ns}url/{ns}loc') if e.text and '/prodotto/' in e.text)
    if not urls:
        raise ValueError('Nessun prodotto nella sitemap')
    return [{'permalink': u} for u in sorted(urls)]


def record_from_page(product, markup):
    soup = BeautifulSoup(markup, 'html.parser')
    title = soup.select_one('h1.product_title')
    if not title:
        raise ValueError('Titolo prodotto assente: pagina non riconosciuta')
    root = title.find_parent(class_='type-product') or soup
    # Exclude related products from price, stock, category and date extraction.
    for unrelated in root.select('.related, .upsells, .cross-sells, .wd-products'):
        unrelated.decompose()
    record = {'id': product.get('id'), 'nome': text(title), 'url': product['permalink'], 'categorie': [clean(c['name']) for c in product.get('categories', [])]}
    stock = root.select_one('.stock')
    availability = product.get('is_in_stock')
    if stock:
        classes = stock.get('class', [])
        if 'out-of-stock' in classes:
            availability = False
        elif 'in-stock' in classes:
            availability = True
    button = root.select_one('button[name="add-to-cart"]')
    if availability is None and button and not button.has_attr('disabled'):
        availability = True
    record['disponibile'] = availability
    record['disponibilita_testo'] = text(stock) or product.get('stock_availability', {}).get('text', '')
    price = root.select_one('p.price')
    def price_value(node):
        match = re.search(r'\d[\d.,]*', text(node))
        return numeric(match[0]) if match else None
    current = price.select_one('ins .amount') if price else None
    if current is None and price:
        current = price.select_one('.amount')
    record['prezzo_eur'] = price_value(current)
    old = price.select_one('del .amount') if price else None
    record['prezzo_originale_eur'] = price_value(old)
    prices = product.get('prices', {})
    if record['prezzo_eur'] is None and prices.get('price') and not prices.get('price_range'):
        record['prezzo_eur'] = int(prices['price']) / 10 ** prices.get('currency_minor_unit', 2)
    record['sconto_pct'] = round(100 * (1 - record['prezzo_eur'] / record['prezzo_originale_eur'])) if record['prezzo_eur'] is not None and record['prezzo_originale_eur'] else None
    image = root.select_one('.woocommerce-product-gallery__image img')
    record['immagine_url'] = (image.get('data-src') or image.get('src', '')) if image else ''
    if not record['categorie']:
        record['categorie'] = list(dict.fromkeys(text(a) for a in root.select('a[href*="/categoria/"]')))
    expiry = root.select_one('.wmc-product-expiry')
    record['termine_testo'] = text(expiry)
    record['termine_iso'] = ''
    if expiry:
        match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', text(expiry))
        if match:
            try:
                record['termine_iso'] = datetime.strptime(match[1], '%d/%m/%Y').date().isoformat()
            except ValueError:
                pass
    record.update(parse_nutrition(root))
    record['ingredienti'] = ''
    for panel in root.select('.woocommerce-Tabs-panel'):
        label = soup.find(id=panel.get('aria-labelledby', ''))
        if label and 'ingredient' in text(label).lower():
            record['ingredienti'] = text(panel)
            break
    if not record['ingredienti']:
        desc = root.select_one('#tab-description')
        m = re.search(r'Ingredienti\s*:\s*(.*?)(?=Valori nutrizionali|$)', text(desc), re.I)
        if m:
            record['ingredienti'] = m[1].strip()
    record['rilevato_il'] = datetime.now().astimezone().isoformat(timespec='seconds')
    return metrics(record)


def scrape(product):
    time.sleep(.35)
    return record_from_page(product, get(product['permalink']).text)


def atomic_write(path, content, encoding='utf-8'):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(content, encoding=encoding)
    temporary.replace(path)


def csv_text(records):
    columns = ['nome','url','categorie','disponibile','disponibilita_testo','prezzo_eur','prezzo_originale_eur','sconto_pct','termine_testo','base_nutrizionale']
    for key in FIELDS:
        columns.extend([key, key + '_limite'])
    columns += ['proteine_100kcal','fibre_100kcal','rapporto_grassi_proteine','zuccheri_su_carboidrati_pct','ingredienti','note','immagine_url','fonte_nutrizione','testo_nutrizionale','rilevato_il']
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for record in records:
        row = {k: ' | '.join(v) if isinstance(v, list) else v for k,v in record.items()}
        for k, v in row.items():
            if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')):
                row[k] = "'" + v
        writer.writerow(row)
    return out.getvalue()


def generate_html(payload):
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return HTML.replace('__PAYLOAD__', data)


HTML = r'''<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SaveTheFoods · Catalogo nutrizionale</title>
<style>
:root{--ink:#19352b;--muted:#67776e;--line:#dfe6df;--accent:#24734b;--bg:#f3f5f0}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px system-ui,-apple-system,Segoe UI,sans-serif}header{padding:24px 28px 16px;display:flex;justify-content:space-between;gap:20px;align-items:center}h1{font-size:25px;letter-spacing:-.7px;margin:0 0 5px}p{margin:5px 0;color:var(--muted)}main{padding:0 28px 25px}.toolbar,.filters{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.toolbar{margin-bottom:12px}.filters{padding:14px 0}.control{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)}input,select,button{font:inherit;color:var(--ink);border:1px solid #cbd6ca;border-radius:8px;padding:10px;background:white}input[type=search]{width:320px}input[type=number]{width:110px}button{cursor:pointer}button:hover{border-color:var(--accent);background:#eef5ed}button.primary{color:white;background:var(--accent);border-color:var(--accent)}.check{display:flex;align-items:center;gap:6px;min-height:40px}.check input{accent-color:var(--accent)}details{border-top:1px solid var(--line)}summary{cursor:pointer;padding:10px 0;font-size:13px}.tablebox{border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:calc(100vh - 280px);min-height:280px;background:white}table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th{position:sticky;top:0;background:#e9eee5;z-index:3;text-align:right;white-space:nowrap;border-bottom:1px solid #ccd8c8;padding:0}th button{border:0;border-radius:0;background:transparent;width:100%;text-align:inherit;font-size:12px;line-height:1.4;padding:9px 7px}td{padding:6px 7px;border-bottom:1px solid #edf0ea;text-align:right;white-space:nowrap}th:first-child,td:first-child{position:sticky;left:0;text-align:left;min-width:220px;max-width:255px;background:white;z-index:2}th:first-child{z-index:4;background:#e9eee5}tr:hover td{background:#f5f8f2}td.product{white-space:normal}.productcell{display:flex;align-items:center;gap:10px}.thumb{width:32px;height:36px;object-fit:contain;flex-shrink:0;border-radius:5px}.product a{color:var(--ink);text-decoration:none;font-weight:600;line-height:1.4}.product a:hover{text-decoration:underline}.sub{display:block;color:var(--muted);font-size:11px;margin-top:4px;line-height:1.4}.metric{background:#f1f7ed;color:#25603d;font-weight:650}.missing{color:#a0a8a0}.badge{display:inline-block;padding:2px 6px;border-radius:4px;background:#fff0ca;color:#775317;font-size:10px;margin-top:4px}.end{display:flex;justify-content:space-between;gap:15px;align-items:center;padding:12px 0;font-size:12px;color:var(--muted)}.paging{display:flex;align-items:center;gap:9px}.paging button{padding:7px 12px}dialog{border:1px solid var(--line);border-radius:16px;width:min(650px,94vw);max-height:85vh;padding:24px;color:var(--ink)}dialog::backdrop{background:#0c271f66}dialog h2{font-size:20px;padding-right:35px}dialog .close{position:absolute;right:12px;top:12px}dialog p{line-height:1.6}dialog .raw{font-size:13px;white-space:pre-wrap}.detail-button{padding:0;border:0;background:none;text-align:left}.status{font-size:12px;color:var(--muted)}.empty{padding:35px;text-align:center}.cols{display:flex;gap:10px;flex-wrap:wrap;padding:10px 0}.note{font-size:12px;color:var(--muted);margin:8px 0}a{color:var(--accent)}@media(max-width:700px){header{padding:18px 14px;align-items:start}h1{font-size:21px}main{padding:0 12px 15px}input[type=search]{width:100%}.search{width:100%}.tablebox{max-height:65vh}th:first-child,td:first-child{min-width:220px;max-width:240px}.thumb{width:30px;height:38px}.end{flex-wrap:wrap}header button{padding:8px}.control{flex:1}}
</style></head><body>
<header><div><h1>SaveTheFoods</h1><p>Catalogo nutrizionale <span id="updated"></span></p></div><button id="export">Esporta risultati CSV</button></header>
<main><div class="toolbar"><label class="control search">Prodotto o ingrediente<input id="search" type="search" placeholder="Cerca nel catalogo…"></label><label class="control">Categoria<select id="category"><option value="">Tutte le categorie</option></select></label><label class="control">Valori riferiti a<select id="basis"><option value="">100 g e 100 ml</option><option>100 g</option><option>100 ml</option><option value="unknown">Base non dichiarata</option></select></label><label class="check"><input type="checkbox" id="available" checked>Disponibili</label><label class="check"><input type="checkbox" id="nutrition" checked>Con valori nutrizionali</label></div>
<details><summary>Filtri nutrizionali e colonne</summary><div class="filters"><label class="control">kcal max / 100<input id="maxkcal" type="number" min="0" step="any"></label><label class="control">Proteine min, g / 100<input id="minprotein" type="number" min="0" step="any"></label><label class="control">Fibre min, g / 100<input id="minfibre" type="number" min="0" step="any"></label><label class="control">Saturi max, g / 100<input id="maxsat" type="number" min="0" step="any"></label><label class="control">Zuccheri max, g / 100<input id="maxsugar" type="number" min="0" step="any"></label><label class="control">Prezzo max, €<input id="maxprice" type="number" min="0" step="any"></label><button id="reset">Azzera filtri</button></div><div id="columns" class="cols"></div></details>
<p class="note">Valori per 100 g; le schede per 100 ml sono contrassegnate · — = dato non dichiarato · clic sul nome: scheda del negozio · ⓘ: ingredienti e dettagli</p>
<div class="tablebox"><table><thead id="head"></thead><tbody id="body"></tbody></table></div>
<div class="end"><span id="count" role="status" aria-live="polite"></span><div class="paging"><label>Righe <select id="pagesize"><option>25</option><option>50</option><option>100</option><option value="10000">Tutte</option></select></label><button id="prev" aria-label="Pagina precedente">←</button><span id="page"></span><button id="next" aria-label="Pagina successiva">→</button></div></div><div class="status" id="warnings"></div>
</main><dialog id="detail"><button class="close" aria-label="Chiudi">✕</button><div id="detailcontent"></div></dialog>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
'use strict';
const dataset=JSON.parse(document.getElementById('data').textContent), all=dataset.products;
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeurl=s=>{try{const u=new URL(s);return ['http:','https:'].includes(u.protocol)?u.href:''}catch{return ''}};
const fmt=v=>v==null?'—':new Intl.NumberFormat('it-IT',{maximumFractionDigits:2}).format(v);
const columns=[
['nome','Prodotto','',true],
['calorie_kcal','kcal','Energia per 100 g, oppure per 100 ml dove indicato',true],
['proteine_100kcal','Densità<br>proteica','Grammi di proteine per 100 kcal',true],
['fibre_100kcal','Fibre /<br>100 kcal','Grammi di fibre per 100 kcal; non è un indice di sazietà',true],
['rapporto_grassi_proteine','Grassi /<br>proteine','Rapporto tra i grammi di grassi e quelli di proteine',true],
['zuccheri_su_carboidrati_pct','Zuccheri /<br>carboidrati %','Percentuale dei carboidrati costituita da zuccheri',true],
['proteine_g','Proteine g','',true],
['grassi_g','Grassi g','',true],
['grassi_saturi_g','Saturi g','',true],
['carboidrati_g','Carboidrati g','',true],
['zuccheri_g','Zuccheri g','',true],
['fibre_g','Fibre g','',true],
['sale_g','Sale g','',true],
['prezzo_eur','Prezzo €','Prezzo della confezione indicata nel nome',false],
['termine_iso','Termine','Data riportata dal negozio; apri i dettagli per la dicitura completa',false],
['base_nutrizionale','Base','Quantità cui si riferiscono i nutrienti',false]];
const scales={proteine_100kcal:[8,false],fibre_100kcal:[5,false],rapporto_grassi_proteine:[4,true],zuccheri_su_carboidrati_pct:[50,true]};
function heat(key,value){if(value==null||!scales[key])return '';const [max,invert]=scales[key],t=Math.max(0,Math.min(1,value/max));return 'background-color:hsl('+Math.round(120*(invert?1-t:t))+',65%,90%)'}
let visible=new Set(columns.filter(c=>c[3]).map(c=>c[0])),sortKey='proteine_100kcal',direction=-1,page=1,filtered=[];
$('updated').textContent='· '+new Date(dataset.updated_at).toLocaleString('it-IT',{dateStyle:'short',timeStyle:'short'});
for(const category of [...new Set(all.flatMap(p=>p.categorie))].sort((a,b)=>a.localeCompare(b,'it'))){const o=document.createElement('option');o.textContent=category;$('category').append(o)}
$('columns').innerHTML=columns.slice(1).map(c=>`<label class="check"><input type="checkbox" data-col="${c[0]}" ${visible.has(c[0])?'checked':''}>${c[1].replace(/<br>/g,' ')}</label>`).join('');
$('columns').addEventListener('change',e=>{const k=e.target.dataset.col;if(e.target.checked)visible.add(k);else visible.delete(k);render()});
function meets(p,key,limit,min){const v=p[key],op=p[key+'_limite']||'';if(v==null||!p.base_nutrizionale&&key!=='prezzo_eur')return false;if(op==='≈')return false;if(min){if(op.startsWith('<')||op==='≤')return false;return v>=limit}if(op.startsWith('>')||op==='≥')return false;return v<=limit}
function apply(){const q=$('search').value.trim().toLocaleLowerCase('it'),cat=$('category').value,base=$('basis').value;const rules=[['maxkcal','calorie_kcal',false],['minprotein','proteine_g',true],['minfibre','fibre_g',true],['maxsat','grassi_saturi_g',false],['maxsugar','zuccheri_g',false],['maxprice','prezzo_eur',false]];filtered=all.filter(p=>(!q||(p.nome+' '+p.ingredienti).toLocaleLowerCase('it').includes(q))&&(!cat||p.categorie.includes(cat))&&(!base||(base==='unknown'?!p.base_nutrizionale:p.base_nutrizionale===base))&&(!$('available').checked||p.disponibile===true)&&(!$('nutrition').checked||p.nutrizione_presente)&&rules.every(([id,k,m])=>$(id).value===''||meets(p,k,Number($(id).value),m)));sort();page=1;render()}
function sort(){filtered.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null||x==='')return y==null||y===''?a.nome.localeCompare(b.nome,'it'):1;if(y==null||y==='')return -1;return direction*(typeof x==='number'?x-y:String(x).localeCompare(String(y),'it'))||a.nome.localeCompare(b.nome,'it')})}
function render(){const cols=columns.filter(c=>visible.has(c[0]));$('head').innerHTML='<tr>'+cols.map(c=>`<th scope="col" aria-sort="${sortKey===c[0]?(direction===1?'ascending':'descending'):'none'}"><button data-sort="${c[0]}" title="${esc(c[2])}">${c[1]} ${sortKey===c[0]?(direction===1?'↑':'↓'):''}</button></th>`).join('')+'</tr>';let size=Number($('pagesize').value),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages);$('body').innerHTML=filtered.slice((page-1)*size,page*size).map(p=>'<tr>'+cols.map(c=>{const k=c[0],v=p[k];if(k==='nome')return `<td class="product"><div class="productcell">${safeurl(p.immagine_url)?`<img class="thumb" loading="lazy" src="${esc(safeurl(p.immagine_url))}" alt="">`:''}<div><a href="${esc(safeurl(p.url))}" target="_blank" rel="noopener noreferrer">${esc(p.nome)}</a> <button class="detail-button" data-detail="${all.indexOf(p)}" aria-label="Dettagli ${esc(p.nome)}">ⓘ</button>${p.base_nutrizionale==='100 ml'?'<span class="sub">Valori per 100 ml</span>':!p.base_nutrizionale?'<span class="sub">Base non dichiarata</span>':''}${p.disponibile!==true?'<span class="badge">'+(p.disponibile===false?'Esaurito':'Disponibilità da verificare')+'</span>':''}${p.note.length?'<span class="badge">Dati da verificare</span>':''}</div></div></td>`;if(k==='termine_iso')return `<td title="${esc(p.termine_testo)}">${v?esc(v.split('-').reverse().join('/')):'—'}</td>`;if(k==='base_nutrizionale')return `<td>${esc(v||'Non indicata')}</td>`;return `<td style="${heat(k,v)}" class="${v==null?'missing':scales[k]?'metric':''}">${esc(p[k+'_limite']||'')}${fmt(v)}</td>`}).join('')+'</tr>').join('')||`<tr><td colspan="${cols.length}" class="empty">Nessun prodotto corrisponde ai filtri.</td></tr>`;$('count').textContent=`${filtered.length} prodotti su ${all.length}`;$('page').textContent=`${page} / ${pages}`;$('prev').disabled=page<=1;$('next').disabled=page>=pages}
$('head').addEventListener('click',e=>{const b=e.target.closest('[data-sort]');if(!b)return;let k=b.dataset.sort;direction=sortKey===k?-direction:(['nome','termine_iso','base_nutrizionale'].includes(k)?1:-1);sortKey=k;sort();render()});
$('body').addEventListener('click',e=>{const b=e.target.closest('[data-detail]');if(!b)return;const p=all[Number(b.dataset.detail)];$('detailcontent').innerHTML=`<h2>${esc(p.nome)}</h2><p><strong>${fmt(p.prezzo_eur)} €</strong> · ${esc(p.disponibilita_testo||'Disponibilità non dichiarata')}</p><p>${esc(p.termine_testo)}</p>${p.note.length?`<p class="badge">${esc(p.note.join(' · '))}</p>`:''}<h3>Ingredienti</h3><p>${esc(p.ingredienti||'Non riportati nella scheda testuale.')}</p><h3>Valori riportati dal sito</h3><p class="raw">${esc(p.testo_nutrizionale||'Non disponibili in forma testuale.')}</p><p>Proteine / 100 kcal: ${fmt(p.proteine_100kcal)} g · Fibre / 100 kcal: ${fmt(p.fibre_100kcal)} g</p><p><a href="${esc(safeurl(p.url))}" target="_blank" rel="noopener noreferrer">Apri il prodotto su SaveTheFoods ↗</a></p>`;$('detail').showModal()});
$('detail').querySelector('.close').onclick=()=>$('detail').close();$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};$('pagesize').onchange=()=>{page=1;render()};
for(const id of ['search','category','basis','available','nutrition','maxkcal','minprotein','minfibre','maxsat','maxsugar','maxprice'])$(id).addEventListener('input',apply);
$('reset').onclick=()=>{for(const id of ['search','category','basis','maxkcal','minprotein','minfibre','maxsat','maxsugar','maxprice'])$(id).value='';$('available').checked=true;$('nutrition').checked=true;apply()};
$('export').onclick=()=>{const keys=['nome','url','prezzo_eur','termine_testo','base_nutrizionale',...columns.filter(c=>!['nome','prezzo_eur','termine_iso','base_nutrizionale'].includes(c[0])).map(c=>c[0])];const quote=x=>'"'+String(x??'').replace(/^[=+@\-]/,"'$&").replace(/"/g,'""')+'"';const rows=[keys,...filtered.map(p=>keys.map(k=>p[k]==null?'':(p[k+'_limite']||'')+p[k]))];const blob=new Blob(['\ufeff'+rows.map(r=>r.map(quote).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='savethefoods_filtrati.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
const missing=all.filter(p=>!p.nutrizione_presente).length;const stale=(Date.now()-new Date(dataset.updated_at).getTime())>5*86400000;$('warnings').textContent=(stale?'Dati non aggiornati da oltre 5 giorni. ':'')+(missing?`${missing} prodotti senza valori testuali: disattiva “Con valori nutrizionali” per mostrarli.`:'');apply();
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='Usa il JSON salvato, senza rete')
    parser.add_argument('--aggiorna', action='store_true', help='Aggiorna dal sito (comportamento predefinito)')
    parser.add_argument('--no-browser', action='store_true', help='Non apre automaticamente il browser')
    parser.add_argument('--cartella', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 5), metavar='1-4')
    parser.add_argument('--includi-esauriti', action='store_true')
    args = parser.parse_args()
    folder = args.cartella.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / 'prodotti_savethefoods.json'
    html_path = folder / 'savethefoods_dashboard.html'
    try:
        if args.offline:
            if not cache.exists():
                raise ValueError('Dati salvati non trovati. Esegui prima senza --offline.')
            payload = json.loads(cache.read_text(encoding='utf-8'))
            if payload.get('schema_version') != 2:
                raise ValueError('Cache non compatibile. Esegui un aggiornamento.')
        else:
            print('Aggiornamento SaveTheFoods…', flush=True)
            try:
                products = catalog_api()
            except (requests.RequestException, ValueError) as exc:
                print(f'Catalogo API non disponibile ({exc}). Provo la sitemap…', flush=True)
                products = catalog_sitemap()
            selected = [p for p in products if args.includi_esauriti or (p.get('is_in_stock') is not False and p.get('is_purchasable') is not False)]
            records, errors = [], []
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(scrape, p): p for p in selected}
                for i, future in enumerate(as_completed(futures), 1):
                    product = futures[future]
                    try:
                        record = future.result()
                        if args.includi_esauriti or record['disponibile'] is not False:
                            records.append(record)
                        print(f'[{i}/{len(selected)}] {record["nome"]}', flush=True)
                    except Exception as exc:
                        errors.append({'url': product['permalink'], 'errore': str(exc)})
                        print(f'[{i}/{len(selected)}] ERRORE: {product["permalink"]}: {exc}', flush=True)
            if errors:
                atomic_write(folder / 'errori_ultimo_aggiornamento.json', json.dumps(errors, ensure_ascii=False, indent=2))
                raise ValueError(f'{len(errors)} schede non scaricate. Per evitare un catalogo incompleto i dati precedenti sono conservati. Riprova; dettagli in errori_ultimo_aggiornamento.json.')
            if not records or not any(r['nutrizione_presente'] for r in records):
                raise ValueError('Nessun dato nutrizionale trovato: possibile modifica del sito. Dati precedenti conservati.')
            records.sort(key=lambda r: r['nome'].casefold())
            payload = {'schema_version': 2, 'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'catalog_count': len(products), 'products': records}
            # Prepare all output before replacing saved data.
            page = generate_html(payload)
            csv_content = csv_text(records)
            atomic_write(cache, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
            atomic_write(folder / 'prodotti_savethefoods.csv', csv_content, encoding='utf-8-sig')
        atomic_write(html_path, generate_html(payload))
        print(f'Pronto: {len(payload["products"])} prodotti.\n{html_path}')
        if not args.no_browser:
            webbrowser.open(html_path.as_uri())
        return 0
    except (requests.RequestException, ValueError, OSError, ElementTree.ParseError) as exc:
        print(f'\nAggiornamento non completato: {exc}', file=sys.stderr)
        if cache.exists():
            print('Puoi riaprire il catalogo precedente con --offline.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
