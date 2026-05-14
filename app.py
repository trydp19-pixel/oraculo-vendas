import os
import re
import json
import sqlite3
import time
import hmac
import hashlib
import requests
import base64
import urllib.parse
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv

# Carrega as chaves
load_dotenv()

GEMINI_KEY = os.getenv("GEMINI_KEY")
CHATGPT_KEY = os.getenv("CHATGPT_KEY")
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_APP_SECRET = os.getenv("SHOPEE_APP_SECRET")
ML_TOKEN = os.getenv("ML_TOKEN") 

try: 
    openai_client = OpenAI(api_key=CHATGPT_KEY)
except: 
    openai_client = None

# ==========================================
# 🗄️ MÓDULO DE BANCO DE DADOS
# ==========================================
DB_PATH = "oraculo_memoria_v245.db" 

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS cupons_salvos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, 
                        loja TEXT, 
                        codigo TEXT, 
                        tipo TEXT, 
                        valor REAL, 
                        maximo REAL
                    )''')
    conn.commit()
    conn.close()

init_db()

def salvar_cupom(loja, codigo, tipo, valor, maximo):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Se o cupom já existe, deleta o antigo para que o novo registro vá para o topo (maior ID)
        cursor.execute("SELECT id FROM cupons_salvos WHERE loja=? AND codigo=? AND tipo=? AND valor=? AND maximo=?", (loja, codigo, tipo, valor, maximo))
        res = cursor.fetchone()
        if res:
            cursor.execute("DELETE FROM cupons_salvos WHERE id=?", (res[0],))
        cursor.execute("INSERT INTO cupons_salvos (loja, codigo, tipo, valor, maximo) VALUES (?, ?, ?, ?, ?)", (loja, codigo, tipo, valor, maximo))
        conn.commit()
        conn.close()
    except: pass

def carregar_cupons_loja(loja):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT codigo, tipo, valor, maximo FROM cupons_salvos WHERE loja=? ORDER BY id DESC LIMIT 5", (loja,))
        res = cursor.fetchall()
        conn.close()
        final_res = []
        for r in res:
            if r not in final_res: final_res.append(r)
        return final_res[:5]
    except: return []

def identificar_loja(url):
    u = url.lower()
    if "mercadolivre" in u or "meli.la" in u: return "ML"
    if "amazon" in u or "amzn.to" in u: return "AMZ"
    if "shopee" in u or "shp.ee" in u or "shope.ee" in u: return "SHP"
    if "magalu" in u or "magazineluiza" in u: return "MGL"
    return "OUTROS"

# ==========================================
# 🛒 MÓDULO DE EXTRAÇÃO E APIS OFICIAIS
# ==========================================
def formatar_moeda(valor):
    if not valor or str(valor).strip() in ["Ver no site", "0", "0.0", "0.00", "0,00"]: return ""
    try:
        v_str = str(valor).replace(',', '.')
        if v_str.count('.') > 1:
            parts = v_str.rsplit('.', 1)
            v_str = parts[0].replace('.', '') + '.' + parts[1]
        v = float(v_str)
        if v <= 0: return ""
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return str(valor)

def preco_valido(p):
    if not p: return False
    try:
        v = float(str(p).replace(',', '.'))
        return v > 0
    except:
        return False

# --- MERCADO LIVRE ---
def extrair_mercadolivre(url, ml_token=None):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO MERCADO LIVRE")
    
    headers_base = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'pt-BR,pt;q=0.9'
    }
    
    try:
        resp_resolve = requests.get(url, headers=headers_base, allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
    except:
        url_final = url

    sessao = requests.Session()
    if ml_token:
        try:
            cookie_str = base64.b64decode(ml_token).decode('utf-8')
            if "=" in cookie_str:
                k, v = cookie_str.split("=", 1)
                sessao.cookies.set(k, v, domain=".mercadolivre.com.br")
                sessao.cookies.set(k, v, domain=".mercadolibre.com")
        except: pass

    try:
        resp = sessao.get(url_final, headers=headers_base, allow_redirects=True, timeout=15)
        html_resp = resp.text
        soup = BeautifulSoup(html_resp, 'html.parser')
        
        if '/social/' in url_final:
            url_real = None
            links = soup.find_all('a', href=True)
            for a in links:
                href = a['href']
                if '/p/' in href or 'MLB' in href:
                    url_real = href
                    break
            if not url_real:
                canonico = soup.find('link', rel='canonical')
                if canonico and canonico.get('href'): url_real = canonico['href']
            if url_real:
                if url_real.startswith('/'): url_real = "https://www.mercadolivre.com.br" + url_real
                url_final = url_real
                resp = sessao.get(url_final, headers=headers_base, timeout=15)
                html_resp = resp.text
                soup = BeautifulSoup(html_resp, 'html.parser')
    except Exception as e:
        return None

    titulo, preco_atual, preco_antigo, foto_url, descricao = "Produto Mercado Livre", None, None, None, ""

    def extrair_valor_da_tag(tag):
        if not tag: return None
        frac = tag.find('span', class_=re.compile(r'andes-money-amount__fraction'))
        cents = tag.find('span', class_=re.compile(r'andes-money-amount__cents'))
        if frac:
            val = frac.text.replace('.', '')
            if cents: val += f".{cents.text}"
            return val
        return None

    try:
        t_meta = soup.find('meta', property='og:title')
        if t_meta: titulo = t_meta['content']
        i_meta = soup.find('meta', property='og:image')
        if i_meta: foto_url = i_meta['content']
        
        d_meta = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', property='og:description')
        if d_meta: descricao = d_meta.get('content', '')[:800]

        meta_p = soup.find('meta', itemprop='price')
        if meta_p and preco_valido(meta_p.get('content')): preco_atual = meta_p['content']
        if not preco_atual:
            meta_p2 = soup.find('meta', property='product:price:amount')
            if meta_p2 and preco_valido(meta_p2.get('content')): preco_atual = meta_p2['content']
        
        s_tag = soup.find('s', class_=re.compile(r'andes-money-amount'))
        if s_tag:
            p_antigo = extrair_valor_da_tag(s_tag)
            if preco_valido(p_antigo): preco_antigo = p_antigo

        if not preco_atual:
            bloco_preco = soup.find('div', class_=re.compile(r'ui-pdp-price__second-line'))
            if bloco_preco:
                span_money = bloco_preco.find('span', class_=re.compile(r'andes-money-amount'))
                if span_money and not span_money.find_parent('s') and span_money.name != 's':
                    p = extrair_valor_da_tag(span_money)
                    if preco_valido(p): preco_atual = p

        if not preco_atual:
            container = soup.find('div', class_=re.compile(r'ui-pdp-price'))
            if container:
                tags_money = container.find_all('span', class_=re.compile(r'andes-money-amount'))
                for tag in tags_money:
                    if tag.find_parent(class_=re.compile(r'(coupon|pill|cashback|loyalty)', re.IGNORECASE)): continue
                    if tag.name == 's' or tag.find_parent('s'): continue
                    p = extrair_valor_da_tag(tag)
                    if preco_valido(p):
                        preco_atual = p
                        break
    except: pass

    if not preco_atual or not foto_url or not preco_antigo:
        match = re.search(r'MLB[-_]?(\d+)', url_final, re.IGNORECASE)
        if match:
            mlb_id = match.group(0).upper().replace('-', '').replace('_', '')
            try:
                if '/p/' in url_final or '/product/' in url_final:
                    api_url = f"https://api.mercadolibre.com/products/{mlb_id}"
                    dados = sessao.get(api_url, headers=headers_base, timeout=10).json()
                    if titulo == "Produto Mercado Livre": titulo = dados.get('name', titulo)
                    if not descricao: descricao = dados.get('short_description', '')[:800]
                    if 'buy_box_winner' in dados and dados['buy_box_winner']:
                        bbw = dados['buy_box_winner']
                        if not preco_atual and preco_valido(bbw.get('price')): preco_atual = bbw.get('price')
                        if not preco_antigo and preco_valido(bbw.get('original_price')): preco_antigo = bbw.get('original_price')
                    if not foto_url and dados.get('pictures'): foto_url = dados['pictures'][0]['url']
                else:
                    api_url = f"https://api.mercadolibre.com/items/{mlb_id}"
                    dados = sessao.get(api_url, headers=headers_base, timeout=10).json()
                    if titulo == "Produto Mercado Livre": titulo = dados.get('title', titulo)
                    if not preco_atual: 
                        p_base = dados.get('price')
                        if not preco_valido(p_base) and dados.get('variations') and len(dados['variations']) > 0:
                            p_base = dados['variations'][0].get('price')
                        if preco_valido(p_base): preco_atual = p_base
                    if not preco_antigo:
                        p_old = dados.get('original_price')
                        if not preco_valido(p_old) and dados.get('variations') and len(dados['variations']) > 0:
                            p_old = dados['variations'][0].get('original_price')
                        if preco_valido(p_old): preco_antigo = p_old
                    if not foto_url and dados.get('pictures'): foto_url = dados['pictures'][0]['url']
            except: pass

    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    return {
        "titulo": titulo, "descricao": descricao, "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, "foto_url": foto_url, "link": url
    }

# --- AMAZON ---
def extrair_amazon(url, token=None):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO AMAZON")
    
    headers_list = [
        {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Accept-Language': 'pt-BR,pt;q=0.9'},
        {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1', 'Accept-Language': 'pt-BR,pt;q=0.9'}
    ]
    
    try:
        resp_resolve = requests.get(url, headers=headers_list[0], allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
    except: url_final = url

    match_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url_final)
    if not match_asin: match_asin = re.search(r'([A-Z0-9]{10})', url_final)
    if match_asin: url_final = f"https://www.amazon.com.br/dp/{match_asin.group(1)}"

    sessao = requests.Session()
    if token:
        try:
            cookie_str = base64.b64decode(token).decode('utf-8')
            for cookie_part in cookie_str.split(';'):
                if "=" in cookie_part:
                    k, v = cookie_part.strip().split("=", 1)
                    sessao.cookies.set(k, v, domain=".amazon.com.br")
                    sessao.cookies.set(k, v, domain="www.amazon.com.br")
        except: pass

    html = ""
    for idx, headers in enumerate(headers_list):
        try:
            resp = sessao.get(url_final, headers=headers, timeout=15)
            html = resp.text
            if "api-services-support@amazon.com" not in html and "captcha" not in html.lower(): break
        except: pass

    soup = BeautifulSoup(html, 'html.parser')
    titulo_tag = soup.find(id='productTitle') or soup.find('span', id='productTitle')
    titulo = titulo_tag.text.strip() if titulo_tag else 'Produto Amazon'
    
    descricao = ""
    d_meta = soup.find('meta', attrs={'name': 'description'})
    if d_meta: descricao = d_meta.get('content', '')[:800]
    
    preco_atual, preco_antigo = None, None

    def parse_brl(texto):
        if not texto: return None
        match = re.search(r'(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+(?:\.\d{2})?)', texto.replace('\xa0', ' '))
        if not match: return None
        clean = match.group(1)
        if ',' in clean and '.' in clean: clean = clean.replace('.', '').replace(',', '.')
        elif ',' in clean: clean = clean.replace(',', '.')
        try:
            val = float(clean)
            return str(val) if val > 0 else None
        except: return None

    match_twister = re.search(r'id="twister-plus-price-data-price"[^>]+value="([\d.]+)"', html)
    if match_twister:
        try:
            if float(match_twister.group(1)) > 0: preco_atual = str(float(match_twister.group(1)))
        except: pass
        
    if not preco_atual:
        match_attach = re.search(r'id="attach-base-product-price"[^>]+value="([\d.]+)"', html)
        if match_attach:
            try:
                if float(match_attach.group(1)) > 0: preco_atual = str(float(match_attach.group(1)))
            except: pass

    if not preco_atual:
        match_js = re.search(r'"priceAmount":\s*([\d.]+)', html)
        if match_js:
            try:
                if float(match_js.group(1)) > 0: preco_atual = str(float(match_js.group(1)))
            except: pass

    def extract_from_block(block):
        if not block: return None
        offscreen = block.find('span', class_='a-offscreen')
        if offscreen: return parse_brl(offscreen.text)
        whole = block.find('span', class_='a-price-whole')
        frac = block.find('span', class_='a-price-fraction')
        if whole:
            w = re.sub(r'[^\d]', '', whole.text)
            f = re.sub(r'[^\d]', '', frac.text) if frac else '00'
            if w: return f"{w}.{f}"
        return parse_brl(block.text)

    safe_zones = [soup.find('div', id='centerCol'), soup.find('div', id='rightCol'), soup.find('div', id='desktop_buybox'), soup.find('div', id='buybox')]

    if not preco_atual:
        for zone in safe_zones:
            if not zone: continue
            apex = zone.find('span', class_=re.compile(r'apexPriceToPay|priceToPay'))
            if apex:
                p = extract_from_block(apex)
                if p: 
                    preco_atual = p
                    break
    
    if not preco_atual:
        for zone in safe_zones:
            if not zone: continue
            core = zone.find('div', id=re.compile(r'corePriceDisplay_desktop_feature_div|corePrice_desktop|corePrice_feature_div'))
            if core:
                for price_span in core.find_all('span', class_='a-price'):
                    if 'a-text-price' in price_span.get('class', []): continue
                    p = extract_from_block(price_span)
                    if p:
                        preco_atual = p
                        break
            if preco_atual: break

    match_basis = re.search(r'class="a-text-price"[^>]*>\s*<span class="a-offscreen">R\$\s*([\d.,]+)</span>', html)
    if match_basis:
        p_ant = parse_brl(match_basis.group(1))
        if p_ant: preco_antigo = p_ant

    if not preco_antigo:
        for zone in safe_zones:
            if not zone: continue
            basis = zone.find('span', class_=re.compile(r'basisPrice'))
            if basis:
                preco_antigo = extract_from_block(basis)
                if preco_antigo: break
                
    if not preco_antigo:
        for zone in safe_zones:
            if not zone: continue
            for old_span in zone.find_all('span', class_=re.compile(r'a-text-price|a-strike')):
                pt = old_span.parent.text.lower() if old_span.parent else ""
                if 'de:' in pt or 'a-strike' in old_span.get('class', []) or old_span.find('span', class_='a-strike'):
                    p = extract_from_block(old_span)
                    if p:
                        preco_antigo = p
                        break
            if preco_antigo: break

    if preco_atual and preco_antigo:
        try:
            if float(preco_antigo) <= float(preco_atual): preco_antigo = None
        except: pass

    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    img = soup.find(id='landingImage') or soup.find('img', id='imgBlkFront') or soup.find('img', id='main-image') or soup.find('img', class_='a-dynamic-image')
    foto_url = img.get('data-old-hires') or img.get('src') if img else None
    
    if not foto_url:
        meta_img = soup.find('meta', property='og:image')
        if meta_img: foto_url = meta_img.get('content')
        
    return {
        "titulo": titulo, "descricao": descricao,
        "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, 
        "foto_url": foto_url, "link": url
    }

# --- SHOPEE ---
def extrair_shopee(url):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO SHOPEE")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9'
    }
    
    try:
        resp_resolve = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
    except: url_final = url

    url_limpa = url_final.split('?')[0]

    titulo, descricao, preco_atual, preco_antigo, foto_url = "Produto Shopee", "", None, None, None

    match = re.search(r'i\.(\d+)\.(\d+)', url_limpa)
    if not match:
        shop_match = re.search(r'shopid=(\d+)', url_limpa, re.IGNORECASE)
        item_match = re.search(r'itemid=(\d+)', url_limpa, re.IGNORECASE)
        if shop_match and item_match: shop_id, item_id = shop_match.group(1), item_match.group(1)
        else: shop_id, item_id = None, None
    else: shop_id, item_id = match.group(1), match.group(2)

    if shop_id and item_id:
        api_url = f"https://shopee.com.br/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
        headers_api = headers.copy()
        headers_api['Accept'] = 'application/json'
        headers_api['x-api-source'] = 'pc'
        try:
            api_resp = requests.get(api_url, headers=headers_api, timeout=10)
            if api_resp.status_code == 200:
                dados = api_resp.json()
                if 'data' in dados and dados['data']:
                    item_data = dados['data']
                    titulo = item_data.get('name', titulo)
                    descricao = item_data.get('description', '')[:800]
                    p_atual_raw = item_data.get('price')
                    p_antigo_raw = item_data.get('price_before_discount')
                    if p_atual_raw: preco_atual = str(p_atual_raw / 100000)
                    if p_antigo_raw: preco_antigo = str(p_antigo_raw / 100000)
                    foto_id = item_data.get('image')
                    if foto_id: foto_url = f"https://cf.shopee.com.br/file/{foto_id}"
        except: pass

    if not preco_atual or titulo == "Produto Shopee":
        headers_bot = {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)', 'Accept': '*/*'}
        try:
            html_resp = requests.get(url_limpa, headers=headers_bot, timeout=15).text
            soup = BeautifulSoup(html_resp, 'html.parser')
            
            t = soup.find('title')
            if t: titulo = t.text.replace(' | Shopee Brasil', '').strip()
            
            d_meta = soup.find('meta', attrs={'name': 'description'})
            if d_meta: descricao = d_meta.get('content', '')[:800]
            
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get('@type') == 'Product':
                        titulo = data.get('name', titulo)
                        if not descricao and data.get('description'): descricao = data['description'][:800]
                        if not foto_url and data.get('image'): foto_url = data['image']
                        if 'offers' in data:
                            preco_seo = data['offers'].get('price')
                            if preco_seo: preco_atual = str(preco_seo)
                except: pass
            
            if not preco_atual:
                match_price = re.search(r'"price":\s*(\d{5,})', html_resp)
                if match_price: preco_atual = str(float(match_price.group(1)) / 100000)
            if not preco_antigo:
                match_old = re.search(r'"price_before_discount":\s*(\d{5,})', html_resp)
                if match_old: preco_antigo = str(float(match_old.group(1)) / 100000)
            if not foto_url:
                i_meta = soup.find('meta', property='og:image')
                if i_meta: foto_url = i_meta['content']
        except: pass

    if SHOPEE_APP_ID and SHOPEE_APP_SECRET:
        try:
            timestamp = int(time.time())
            path_link = "/api/v2/affiliate/generate_short_link"
            base_string_link = f"{SHOPEE_APP_ID}{path_link}{timestamp}"
            sign_link = hmac.new(SHOPEE_APP_SECRET.encode('utf-8'), base_string_link.encode('utf-8'), hashlib.sha256).hexdigest()
            api_link_url = f"https://partner.shopeemobile.com{path_link}?partner_id={SHOPEE_APP_ID}&timestamp={timestamp}&sign={sign_link}"
            headers_post = {'Content-Type': 'application/json'}
            
            payload_graphql = {"query": f'mutation {{\n  generateShortLink(input: {{originUrl: "{url_limpa}"}}) {{\n    shortLink\n  }}\n}}'}
            link_resp = requests.post(api_link_url, json=payload_graphql, headers=headers_post, timeout=10).json()
            
            novo_link = None
            if link_resp.get("data") and link_resp["data"].get("generateShortLink"):
                novo_link = link_resp["data"]["generateShortLink"].get("shortLink")
            
            if not novo_link:
                link_resp = requests.post(api_link_url, json={"originUrl": url_limpa}, headers=headers_post, timeout=10).json()
                if link_resp.get("data") and "shortLink" in link_resp["data"]: novo_link = link_resp["data"]["shortLink"]
                elif link_resp.get("response") and "shortLink" in link_resp["response"]: novo_link = link_resp["response"]["shortLink"]
            
            if novo_link: url = novo_link
        except: pass

    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    return {
        "titulo": titulo, "descricao": descricao,
        "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, 
        "foto_url": foto_url, "link": url
    }

def extrair_magalu(url):
    return {"titulo": "Produto Magalu", "descricao": "", "preco_atual": "Ver no site", "preco_antigo": None, "foto_url": None, "link": url}

def extrair_dados_loja(url, ml_token=None):
    url = url.strip() 
    if "mercadolivre" in url or "meli.la" in url: return extrair_mercadolivre(url, ml_token)
    elif "amazon" in url or "amzn.to" in url: return extrair_amazon(url, ml_token) 
    elif "shopee" in url or "shp.ee" in url or "shope.ee" in url: return extrair_shopee(url)
    elif "magazineluiza" in url or "magalu" in url: return extrair_magalu(url)
    return None

# ==========================================
# 🧠 MÓDULO DE INTELIGÊNCIA ARTIFICIAL (CLEAN + FALLBACK GPT)
# ==========================================
PROMPT_ANALISTA_PRODUTO = """
Você é o Analista de Produtos de um grande portal de ofertas no Brasil.
Sua missão é formatar o título do produto e identificar a quantidade exata de itens idênticos para fracionamento de preço.

# PRODUTO ORIGINAL: {PRODUTO}
# DETALHES DA LOJA: {DESCRICAO}

# REGRA DO TÍTULO (MUITO IMPORTANTE): 
1. INICIE com o TIPO DO PRODUTO (ex: "Smart TV", "Pote Hermético", "Multivitamínico", "Barraca").
2. MANTENHA a Marca, o Modelo e a quantidade se houver. 
3. DESTAQUE ESPECIFICAÇÕES VITAIS no título (ex: "120 Cápsulas", "200ml", "110V", "12 Pessoas").
4. REMOVA palavras de enfeite (ex: "Original", "Lindo"). 
5. FILTRO DE PREÇO (ATENÇÃO MÁXIMA): O título original pode estar sujo com o preço colado (ex: "Multivitamínico - R$ 39,90" ou "Tênis - 150"). VOCÊ DEVE OBRIGATORIAMENTE APAGAR QUALQUER VALOR EM REAIS, SÍMBOLO "R$", NÚMEROS DE PREÇO, DESCONTOS OU OFERTAS DO TEXTO. O título final deve conter APENAS o nome e especificações da mercadoria.
6. Formate o texto EXATAMENTE separando as informações principais por hífen. Ex: "Pote Hermético - Vidro e Bambu - 200ml - Kit 10 Unidades".

# REGRA DA QUANTIDADE (MUITO IMPORTANTE): Identifique a quantidade de PRODUTOS IDÊNTICOS no pacote para dividir o preço. Se for "Kit 10 Potes", "Kit 10 Cuecas", "Kit 5 Pneus", a quantidade é O NÚMERO DO KIT (Ex: 10, 5). EXCEÇÕES (Quantidade = 1): Pares (meias, sapatos), jogos compostos de peças diferentes (Jogo de Panelas 5 Peças = 1, Dominó 28 Peças = 1). Retorne APENAS o número inteiro.

RETORNE OBRIGATORIAMENTE UM JSON COM AS CHAVES: "titulo_resumido" e "quantidade_itens".
"""

def executar_pipeline_universal(nome_produto, descricao_produto):
    # Faxina Python de Emergência: Limpa o nome original caso todas as IAs falhem
    titulo_emergencia = re.sub(r'(?i)[-\s]*R\$\s*\d+(?:[.,]\d+)?', '', nome_produto)
    titulo_emergencia = re.sub(r'-\s*$', '', titulo_emergencia).strip()
    
    prompt_editor = PROMPT_ANALISTA_PRODUTO.replace("{PRODUTO}", nome_produto).replace("{DESCRICAO}", descricao_produto)
    
    # Tentativa 1: GEMINI
    try:
        schema = {
            "type": "OBJECT", 
            "properties": {
                "titulo_resumido": {"type": "STRING"},
                "quantidade_itens": {"type": "INTEGER", "description": "Quantas unidades vêm no pacote? (Padrão: 1)"}
            }, 
            "required": ["titulo_resumido", "quantidade_itens"]
        }
        
        gemini_payload = {
            "contents": [{"parts": [{"text": prompt_editor}]}], 
            "generationConfig": {
                "temperature": 0.3, 
                "responseMimeType": "application/json", 
                "responseSchema": schema
            }
        }
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
        
        for _ in range(2):
            try:
                r = requests.post(gemini_url, json=gemini_payload, timeout=10)
                if r.status_code == 200:
                    dados_api = r.json()
                    texto_resposta = dados_api['candidates'][0]['content']['parts'][0]['text']
                    
                    match = re.search(r'\{.*\}', texto_resposta, re.DOTALL)
                    if match:
                        dados = json.loads(match.group(0))
                        titulo_bruto = dados.get("titulo_resumido", titulo_emergencia)
                        
                        # Faxina Python pós-IA: Garante que a IA não deixou o preço passar
                        titulo_limpo = re.sub(r'(?i)[-\s]*R\$\s*\d+(?:[.,]\d+)?', '', titulo_bruto)
                        titulo_limpo = re.sub(r'-\s*$', '', titulo_limpo).strip()
                        
                        qtd_ext = dados.get("quantidade_itens", 1)
                        return titulo_limpo, qtd_ext
            except Exception: 
                time.sleep(1)
    except Exception: 
        pass

    # Tentativa 2: CHATGPT (Fallback Blindado)
    if openai_client:
        print("🔄 Gemini falhou. Acionando ChatGPT como fallback...")
        try:
            for _ in range(2):
                try:
                    resp_gpt = openai_client.chat.completions.create(
                        model="gpt-4o-mini", 
                        messages=[{"role":"user","content":prompt_editor}], 
                        temperature=0.3,
                        response_format={"type": "json_object"}
                    )
                    texto_resposta = resp_gpt.choices[0].message.content
                    match = re.search(r'\{.*\}', texto_resposta, re.DOTALL)
                    if match:
                        dados = json.loads(match.group(0))
                        titulo_bruto = dados.get("titulo_resumido", titulo_emergencia)
                        
                        # Faxina Python pós-IA
                        titulo_limpo = re.sub(r'(?i)[-\s]*R\$\s*\d+(?:[.,]\d+)?', '', titulo_bruto)
                        titulo_limpo = re.sub(r'-\s*$', '', titulo_limpo).strip()
                        
                        qtd_ext = dados.get("quantidade_itens", 1)
                        return titulo_limpo, qtd_ext
                except Exception:
                    time.sleep(1)
        except Exception:
            pass
            
    # Se der o pior cenário do mundo (sem internet ou API fora do ar)
    print("⚠️ As duas IAs falharam. Usando título limpo de emergência.")
    return titulo_emergencia, 1

# ==========================================
# 🌐 MOTOR DE CÁLCULO E ATUALIZAÇÃO DE TELA
# ==========================================
def aplicar_desconto_na_tela(codigo, tipo, valor, maximo, local_aplicacao=""):
    texto_atual = st.session_state.get('area_edicao', st.session_state.get('texto_final_zap', ''))
    
    produto_salvo = st.session_state.get('produto_salvo', {})
    qtd_itens = produto_salvo.get('quantidade', 1)

    match = re.search(r'por R\$\s*([\d.,]+)', texto_atual, re.IGNORECASE)
    if match:
        try:
            preco_str = match.group(1)
            p_clean = re.sub(r'[^\d,.]', '', preco_str)
            if ',' in p_clean and '.' in p_clean: p_clean = p_clean.replace('.', '').replace(',', '.')
            elif ',' in p_clean: p_clean = p_clean.replace(',', '.')
            elif '.' in p_clean:
                if len(p_clean.split('.')[-1]) != 2: p_clean = p_clean.replace('.', '')
            
            p_float = float(p_clean)
            desconto = (p_float * (valor / 100)) if tipo == "% Porcentagem" else valor
            if tipo == "% Porcentagem" and maximo > 0: desconto = min(desconto, maximo)
            
            novo_p_float = max(0, p_float - desconto)
            novo_preco_str = formatar_moeda(novo_p_float)
            
            texto_atual = texto_atual.replace(f'por R$ {preco_str}', f'por R$ {novo_preco_str}')
            
            if qtd_itens > 1:
                novo_p_un = formatar_moeda(novo_p_float / qtd_itens)
                if re.search(r'\(R\$\s*[\d.,]+/unidade\)', texto_atual):
                    texto_atual = re.sub(r'\(R\$\s*[\d.,]+/unidade\)', f'(R$ {novo_p_un}/unidade)', texto_atual)
                else:
                    # Adiciona a unidade logo após a formatação de preço "*por R$ XX*"
                    texto_atual = re.sub(r'(\*por R\$\s*[\d.,]+\*)', r'\1' + f' (R$ {novo_p_un}/unidade)', texto_atual)
            
            texto_cupom = ""
            if codigo:
                texto_cupom = f"🎟️ Use o cupom: {codigo}"
                if local_aplicacao and local_aplicacao != "Nenhum":
                    texto_cupom += f" ({local_aplicacao})"
                    
            if "🎟️ Use o cupom:" in texto_atual:
                if texto_cupom:
                    texto_atual = re.sub(r'🎟️ Use o cupom: .*', texto_cupom, texto_atual)
                else:
                    texto_atual = re.sub(r'🎟️ Use o cupom: .*\n*', '', texto_atual)
            elif texto_cupom:
                texto_atual = texto_atual.replace("🔗 LINK MÁGICO", f"{texto_cupom}\n\n🔗 LINK MÁGICO")
            
            st.session_state.texto_final_zap = texto_atual
            st.session_state.area_edicao = texto_atual
            return True
        except Exception as e: 
            st.error(f"Erro ao calcular. (Erro: {e})")
            return False
    else: 
        st.error("Escreva o preço base no formato exato 'por R$ X'")
        return False

# ==========================================
# 🌐 FUNÇÕES DE CALLBACK (AÇÕES DE MEMÓRIA)
# ==========================================
def cb_aplicar_cupom_rapido(cod, tip, val, mx, loja):
    st.session_state['cupom_codigo'] = cod if cod else ""
    st.session_state['cupom_tipo'] = tip
    st.session_state['cupom_valor'] = float(val)
    st.session_state['cupom_max'] = float(mx)
    if aplicar_desconto_na_tela(cod, tip, val, mx, st.session_state.get('cupom_local', 'Nenhum')):
        salvar_cupom(loja, cod, tip, val, mx)

def cb_aplicar_selo(selo):
    texto = st.session_state.get('area_edicao', '')
    linhas = texto.split('\n')
    selos_possiveis = ["💥Oferta imperdível💥", "⚡Oferta relâmpago⚡", "🏴‍☠️ Preço de Bug 😱"]
    
    for i, linha in enumerate(linhas):
        if 'por r$' in linha.lower():
            # Limpa qualquer selo antigo (com ou sem parênteses)
            for s in selos_possiveis:
                linha = linha.replace(f" _{s}_", "").replace(f" _({s})_", "").replace(f" ({s})", "").strip()
            
            # Remove o foguinho original se existir
            linha = linha.replace("🔥", "").strip()
            
            # Adiciona o novo selo no final da linha (sem os parênteses)
            linhas[i] = linha + f" _{selo}_"
            break
            
    novo_texto = '\n'.join(linhas)
    st.session_state.texto_final_zap = novo_texto
    st.session_state.area_edicao = novo_texto

def cb_aplicar_frase_impacto():
    frase = st.session_state.get('input_frase_impacto', '').strip().upper()
    if not frase: return
    
    texto = st.session_state.get('area_edicao', '')
    linhas = texto.split('\n')
    
    idx_titulo = -1
    idx_preco_antigo = -1
    
    # Procura onde está o título e onde começam os preços
    for i, linha in enumerate(linhas):
        if '🔮' in linha: idx_titulo = i
        if '~de R$' in linha or '*por R$' in linha:
            if idx_preco_antigo == -1: idx_preco_antigo = i
            
    if idx_titulo != -1 and idx_preco_antigo != -1:
        # Substitui tudo que estiver entre o título e os preços pela nova frase
        novas_linhas = linhas[:idx_titulo+1] + ["", frase, ""] + linhas[idx_preco_antigo:]
        novo_texto = '\n'.join(novas_linhas)
        
        # Limpa quebras de linha duplas
        novo_texto = re.sub(r'\n{3,}', '\n\n', novo_texto)
        
        st.session_state.texto_final_zap = novo_texto
        st.session_state.area_edicao = novo_texto
        st.session_state['input_frase_impacto'] = ""

# ==========================================
# 🌐 INTERFACE WEB (STREAMLIT) E ESTADO
# ==========================================
st.set_page_config(page_title="Oráculo Web", page_icon="🔮", layout="wide")

if 'historico' not in st.session_state: st.session_state.historico = []
if 'texto_final_zap' not in st.session_state: st.session_state.texto_final_zap = ""
if 'area_edicao' not in st.session_state: st.session_state.area_edicao = ""

with st.sidebar:
    st.markdown("### 🛠️ Configurações Avançadas")
    ml_token_input = st.text_input("🔑 Token ML (Busqy):", value=ML_TOKEN if ML_TOKEN else "", type="password")
    
    st.markdown("---")
    st.markdown("### 🗂️ Últimos Gerados")
    if st.session_state.historico:
        for idx, item in enumerate(st.session_state.historico):
            if st.button(f"🕒 {item['produto']['titulo'][:25]}...", key=f"hist_{idx}"):
                st.session_state['produto_salvo'] = item['produto']
                st.session_state.texto_final_zap = item['txt_zap']
                st.session_state.area_edicao = item['txt_zap']
                st.session_state['cupom_codigo'] = ""
                st.session_state['cupom_tipo'] = "% Porcentagem"
                st.session_state['cupom_valor'] = 0.0
                st.session_state['cupom_max'] = 0.0
                st.session_state['cupom_local'] = "Nenhum"
                st.rerun()
    else: st.caption("Nenhum histórico salvo. Eles são resetados ao fechar a guia.")

st.title("🔮 Oráculo Web - Módulo Clean")
st.markdown("Postagens diretas e focadas na conversão.")

link_input = st.text_input("🔗 Link do Produto (ML, Amazon, Shopee, Magalu):")

if st.button("🚀 Gerar Postagem", type="primary", use_container_width=True):
    if not link_input: st.warning("Insira um link!")
    else:
        st.session_state['cupom_codigo'] = ""
        st.session_state['cupom_tipo'] = "% Porcentagem"
        st.session_state['cupom_valor'] = 0.0
        st.session_state['cupom_max'] = 0.0
        st.session_state['cupom_local'] = "Nenhum"

        with st.spinner("Decodificando a loja e formatando..."):
            produto = extrair_dados_loja(link_input, ml_token=ml_token_input)
            if not produto: produto = {"titulo": "Produto Não Identificado", "descricao": "", "preco_atual": "Ver no site", "preco_antigo": None, "foto_url": None, "link": link_input}
            
            titulo_resumo, qtd_itens = executar_pipeline_universal(produto["titulo"], produto.get("descricao", ""))
            produto['quantidade'] = qtd_itens
            
            txt_zap = f"🔮 {titulo_resumo}\n\n"
            p_antigo, p_atual = produto.get('preco_antigo', ''), produto.get('preco_atual', '')
            txt_zap += f"~de R$ {p_antigo}~\n" if p_antigo and p_antigo not in ["Ver no site", "", "0,00"] else f"~de R$ ~\n"
            
            str_unidade = ""
            if qtd_itens > 1 and p_atual and p_atual not in ["Ver no site", "", "0,00"]:
                p_clean = re.sub(r'[^\d,.]', '', str(p_atual))
                if ',' in p_clean and '.' in p_clean: p_clean = p_clean.replace('.', '').replace(',', '.')
                elif ',' in p_clean: p_clean = p_clean.replace(',', '.')
                elif '.' in p_clean:
                    if len(p_clean.split('.')[-1]) != 2: p_clean = p_clean.replace('.', '')
                try:
                    p_float = float(p_clean)
                    p_un = p_float / qtd_itens
                    str_unidade = f" (R$ {formatar_moeda(p_un)}/unidade)"
                except: pass
            
            txt_zap += f"*por R$ {p_atual}* 🔥{str_unidade}\n\n" if p_atual and p_atual not in ["Ver no site", "", "0,00"] else f"*por R$ * 🔥\n\n"
            txt_zap += f"🔗 LINK MÁGICO P/ COMPRAR: {produto['link']}\n\n_⚠️ O Oráculo avisa, mas a oferta voa._"
            
            st.session_state['produto_salvo'] = produto
            st.session_state.texto_final_zap = txt_zap
            st.session_state.area_edicao = txt_zap
            st.session_state.historico.insert(0, {'produto': produto, 'txt_zap': txt_zap})
            if len(st.session_state.historico) > 10: st.session_state.historico.pop()

if 'produto_salvo' in st.session_state:
    produto_salvo = st.session_state['produto_salvo']
    loja_atual = identificar_loja(produto_salvo.get('link', ''))

    st.markdown("---")
    col1, col2 = st.columns([1, 2])
    with col1:
        if produto_salvo.get("foto_url"): st.image(produto_salvo["foto_url"])
        else: st.info("A loja ocultou a imagem.")
    with col2:
        st.success("✅ Texto Gerado!")
        st.markdown("### 🎟️ Calculadora de Cupom")
        col_c1, col_c2, col_c3 = st.columns(3)
        
        with col_c1: 
            codigo_cupom = st.text_input("Código do Cupom/Oferta:", value=st.session_state.get('cupom_codigo', ''))
        with col_c2: 
            tipo_idx = 0 if st.session_state.get('cupom_tipo', '% Porcentagem') == "% Porcentagem" else 1
            tipo_desconto = st.selectbox("Tipo de Desconto", ["% Porcentagem", "$ Valor fixo"], index=tipo_idx)
        with col_c3:
            valor_desconto = st.number_input("Valor do Desconto", min_value=0.0, step=1.0, value=float(st.session_state.get('cupom_valor', 0.0)))
            desc_max_val = float(st.session_state.get('cupom_max', 0.0))
            if tipo_desconto == "% Porcentagem":
                desconto_maximo = st.number_input("Desconto Máximo (R$)", min_value=0.0, step=1.0, value=desc_max_val)
            else:
                desconto_maximo = 0.0

        opcoes_local = ["Nenhum", "Aplicar na página do produto", "Aplicar na página de compra"]
        loc_val = st.session_state.get('cupom_local', 'Nenhum')
        local_idx = opcoes_local.index(loc_val) if loc_val in opcoes_local else 0
        local_cupom = st.radio("Onde o cupom será inserido?", opcoes_local, horizontal=True, index=local_idx)

        if st.button("🔄 Aplicar Desconto", use_container_width=True):
            st.session_state['cupom_codigo'] = codigo_cupom
            st.session_state['cupom_tipo'] = tipo_desconto
            st.session_state['cupom_valor'] = valor_desconto
            st.session_state['cupom_max'] = desconto_maximo
            st.session_state['cupom_local'] = local_cupom
            if aplicar_desconto_na_tela(codigo_cupom, tipo_desconto, valor_desconto, desconto_maximo, local_cupom):
                salvar_cupom(loja_atual, codigo_cupom, tipo_desconto, valor_desconto, desconto_maximo)
                st.rerun()
                
        cupons_recentes = carregar_cupons_loja(loja_atual)
        if cupons_recentes:
            st.markdown(f"⚡ **Cupons Rápidos ({'Mercado Livre' if loja_atual=='ML' else 'Amazon' if loja_atual=='AMZ' else 'Loja'}):**")
            cols_cup = st.columns(len(cupons_recentes))
            for i, c in enumerate(cupons_recentes):
                cod, tip, val, mx = c[0], c[1], c[2], c[3]
                label_btn = f"{cod if cod else 'Sem Cód'} (-{val}{'%' if 'Porc' in tip else 'R$'})"
                if cols_cup[i].button(label_btn, key=f"quick_cup_{i}", on_click=cb_aplicar_cupom_rapido, args=(cod, tip, val, mx, loja_atual)):
                    pass # Bypass automático

        texto_editado = st.text_area("Bloco de Notas da Postagem:", value=st.session_state.get('area_edicao', ''), height=250)
        st.session_state['area_edicao'] = texto_editado 
        
        texto_url = urllib.parse.quote(texto_editado)
        st.link_button("📲 Enviar para o WhatsApp", f"https://api.whatsapp.com/send?text={texto_url}", use_container_width=True)
        
        st.markdown("---")
        
        st.markdown("### ✍️ Adicionar Frase de Impacto")
        col_f1, col_f2 = st.columns([10, 2])
        with col_f1:
            st.text_input("Frase (Aparecerá abaixo do título):", placeholder="Ex: ASSISTIR A COPA EM TELA DE CINEMA", label_visibility="collapsed", key="input_frase_impacto")
        with col_f2:
            if st.button("➕ Inserir", use_container_width=True, on_click=cb_aplicar_frase_impacto):
                pass
                
        st.markdown("🔖 **Selos Rápidos (Aparecem ao lado do preço):**")
        col_s1, col_s2, col_s3 = st.columns(3)
        if col_s1.button("💥 Oferta imperdível", use_container_width=True, on_click=cb_aplicar_selo, args=("💥Oferta imperdível💥",)): pass
        if col_s2.button("⚡ Oferta relâmpago", use_container_width=True, on_click=cb_aplicar_selo, args=("⚡Oferta relâmpago⚡",)): pass
        if col_s3.button("🏴‍☠️ Preço de Bug", use_container_width=True, on_click=cb_aplicar_selo, args=("🏴‍☠️ Preço de Bug 😱",)): pass
