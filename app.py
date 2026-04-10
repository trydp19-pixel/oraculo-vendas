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
    cursor.execute('''CREATE TABLE IF NOT EXISTS feedback (frase TEXT UNIQUE, produto TEXT, gostou INTEGER)''')
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

def registrar_feedback(frase, produto, gostou):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO feedback (frase, produto, gostou) VALUES (?, ?, ?)", (frase, produto, gostou))
        conn.commit()
        conn.close()
    except: pass

def carregar_exemplos():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT produto, frase FROM feedback WHERE gostou = 1 ORDER BY ROWID DESC LIMIT 15")
        positivos = [f"- [{row[0]}] -> \"{row[1]}\"" for row in cursor.fetchall()][::-1]
        cursor.execute("SELECT produto, frase FROM feedback WHERE gostou = 0 ORDER BY ROWID DESC LIMIT 15")
        negativos = [f"- [{row[0]}] -> \"{row[1]}\"" for row in cursor.fetchall()]
        conn.close()
        return positivos, negativos
    except: return [], []

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

# --- MERCADO LIVRE (INTACTO) ---
def extrair_mercadolivre(url, ml_token=None):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO MERCADO LIVRE")
    print(f"🔗 Link Original: {url}")
    
    headers_base = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'pt-BR,pt;q=0.9'
    }
    
    try:
        print("🕵️ Resolvendo link curto como cliente fantasma...")
        resp_resolve = requests.get(url, headers=headers_base, allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
        print(f"🔗 Link Real Encontrado: {url_final}")
    except:
        url_final = url
        print("⚠️ Falha ao resolver link curto. Usando original.")

    sessao = requests.Session()
    if ml_token:
        print("🔑 Token VIP ML detectado. Injetando cookies...")
        try:
            cookie_str = base64.b64decode(ml_token).decode('utf-8')
            if "=" in cookie_str:
                k, v = cookie_str.split("=", 1)
                sessao.cookies.set(k, v, domain=".mercadolivre.com.br")
                sessao.cookies.set(k, v, domain=".mercadolibre.com")
                print("✅ Cookie injetado.")
        except: pass

    try:
        resp = sessao.get(url_final, headers=headers_base, allow_redirects=True, timeout=15)
        html_resp = resp.text
        print(f"🌐 HTML Status Final: {resp.status_code}")
        
        soup = BeautifulSoup(html_resp, 'html.parser')
        
        if '/social/' in url_final:
            url_real = None
            links = soup.find_all('a', href=True)
            for a in links:
                href = a['href']
                if '/p/' in href or 'MLB' in href:
                    url_real = href
                    print(f"🎯 Botão 'Ir para Produto' encontrado: {url_real}")
                    break
            
            if not url_real:
                canonico = soup.find('link', rel='canonical')
                if canonico and canonico.get('href'):
                    url_real = canonico['href']
                    print(f"🎯 Link Canônico Encontrado: {url_real}")

            if url_real:
                if url_real.startswith('/'):
                    url_real = "https://www.mercadolivre.com.br" + url_real
                url_final = url_real
                print("🔄 Baixando página original do produto...")
                resp = sessao.get(url_final, headers=headers_base, timeout=15)
                html_resp = resp.text
                soup = BeautifulSoup(html_resp, 'html.parser')
                print("✅ Página original carregada.")
    except Exception as e:
        print(f"❌ Erro ao baixar HTML: {e}")
        return None

    titulo, preco_atual, preco_antigo, foto_url = "Produto Mercado Livre", None, None, None

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
        
        if not preco_atual:
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if '@type' in data and data['@type'] in ['Product', 'ProductGroup'] and 'offers' in data:
                        offers = data['offers']
                        if isinstance(offers, dict): p = offers.get('price') or offers.get('lowPrice')
                        elif isinstance(offers, list) and len(offers) > 0: p = offers[0].get('price')
                        if preco_valido(p): preco_atual = p
                except: pass
                
        print(f"👁️‍🗨️ Preço HTML: Atual={preco_atual} | Antigo={preco_antigo}")
    except: pass

    if not preco_atual or not foto_url or not preco_antigo:
        print("🤖 Acionando API ML...")
        match = re.search(r'MLB[-_]?(\d+)', url_final, re.IGNORECASE)
        if match:
            mlb_id = match.group(0).upper().replace('-', '').replace('_', '')
            try:
                if '/p/' in url_final or '/product/' in url_final:
                    api_url = f"https://api.mercadolibre.com/products/{mlb_id}"
                    dados = sessao.get(api_url, headers=headers_base, timeout=10).json()
                    if titulo == "Produto Mercado Livre": titulo = dados.get('name', titulo)
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

    print(f"🏁 FIM ML -> Titulo: {titulo[:15]} | Atual: {preco_atual} | Antigo: {preco_antigo}")
    print("="*50 + "\n")
    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    return {
        "titulo": titulo, "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, "foto_url": foto_url, "link": url
    }

# --- AMAZON ---
def extrair_amazon(url, token=None):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO AMAZON (MODO DEBUG PROFUNDO)")
    print(f"🔗 Link Original: {url}")
    
    headers_list = [
        {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9',
        },
        {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9'
        }
    ]
    
    try:
        resp_resolve = requests.get(url, headers=headers_list[0], allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
    except:
        url_final = url

    # LIMPEZA DE URL
    match_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url_final)
    if not match_asin:
        match_asin = re.search(r'([A-Z0-9]{10})', url_final)
    if match_asin:
        url_final = f"https://www.amazon.com.br/dp/{match_asin.group(1)}"
        print(f"🧹 URL Limpa: {url_final}")

    sessao = requests.Session()

    if token:
        print("🔑 [DEBUG] Token VIP Amazon detectado. Injetando...")
        try:
            cookie_str = base64.b64decode(token).decode('utf-8')
            for cookie_part in cookie_str.split(';'):
                if "=" in cookie_part:
                    k, v = cookie_part.strip().split("=", 1)
                    sessao.cookies.set(k, v, domain=".amazon.com.br")
                    sessao.cookies.set(k, v, domain="www.amazon.com.br")
            print("✅ [DEBUG] Cookie(s) Amazon injetado(s) com sucesso.")
        except Exception as e:
            print(f"⚠️ [DEBUG] Falha ao injetar Token da Amazon: {e}")

    html = ""
    for idx, headers in enumerate(headers_list):
        print(f"🤖 [DEBUG] Tentando acessar com máscara {idx+1}...")
        try:
            resp = sessao.get(url_final, headers=headers, timeout=15)
            html = resp.text
            print(f"🌐 [DEBUG] Código de Status HTTP: {resp.status_code}")
            print(f"📦 [DEBUG] Tamanho do HTML retornado: {len(html)} caracteres")
            
            if "api-services-support@amazon.com" not in html and "captcha" not in html.lower() and "bot check" not in html.lower():
                print("✅ [DEBUG] Acesso concedido! (Sem CAPTCHA explícito)")
                break
            else:
                print("🚨 [DEBUG] ALERTA: CAPTCHA ou Bloqueio detectado no HTML retornado!")
        except Exception as e: 
            print(f"❌ [DEBUG] Erro de conexão: {e}")
            
    try:
        with open("debug_amazon_html.txt", "w", encoding="utf-8") as f:
            f.write(html)
        print("📁 [DEBUG] HTML bruto salvo no arquivo 'debug_amazon_html.txt'. Verifique este arquivo para ver se os preços estão lá!")
    except Exception as e:
        print(f"⚠️ [DEBUG] Não foi possível salvar o arquivo de log do HTML: {e}")

    if "R$" in html:
        print("💵 [DEBUG] Símbolo 'R$' ENCONTRADO no código-fonte.")
    else:
        print("🛑 [DEBUG] Símbolo 'R$' NÃO ENCONTRADO em nenhum lugar da página! A Amazon ocultou o preço do HTML bruto.")

    soup = BeautifulSoup(html, 'html.parser')
    
    titulo_tag = soup.find(id='productTitle') or soup.find('span', id='productTitle')
    titulo = titulo_tag.text.strip() if titulo_tag else 'Produto Amazon'
    print(f"🏷️ [DEBUG] Título capturado: {titulo[:30]}...")
    
    preco_atual, preco_antigo = None, None

    def parse_brl(texto):
        if not texto: return None
        match = re.search(r'(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+(?:\.\d{2})?)', texto.replace('\xa0', ' '))
        if not match: return None
        clean = match.group(1)
        if ',' in clean and '.' in clean:
            clean = clean.replace('.', '').replace(',', '.')
        elif ',' in clean:
            clean = clean.replace(',', '.')
        try:
            val = float(clean)
            return str(val) if val > 0 else None
        except: return None

    print("🕵️ [DEBUG] Iniciando varredura por Regex (Twister/Attach/JS)...")
    match_twister = re.search(r'id="twister-plus-price-data-price"[^>]+value="([\d.]+)"', html)
    print(f"   [DEBUG] Resultado Regex Twister: {match_twister.group(1) if match_twister else 'Nada'}")
    if match_twister:
        try:
            val = float(match_twister.group(1))
            if val > 0: preco_atual = str(val)
        except: pass
        
    if not preco_atual:
        match_attach = re.search(r'id="attach-base-product-price"[^>]+value="([\d.]+)"', html)
        print(f"   [DEBUG] Resultado Regex Attach: {match_attach.group(1) if match_attach else 'Nada'}")
        if match_attach:
            try:
                val = float(match_attach.group(1))
                if val > 0: preco_atual = str(val)
            except: pass

    if not preco_atual:
        match_js = re.search(r'"priceAmount":\s*([\d.]+)', html)
        print(f"   [DEBUG] Resultado Regex JS priceAmount: {match_js.group(1) if match_js else 'Nada'}")
        if match_js:
            try:
                val = float(match_js.group(1))
                if val > 0: preco_atual = str(val)
            except: pass

    print("🕵️ [DEBUG] Iniciando varredura por DOM (Zonas Seguras)...")
    def extract_from_block(block, nome_bloco=""):
        if not block: return None
        offscreen = block.find('span', class_='a-offscreen')
        if offscreen: 
            print(f"   [DEBUG] Encontrado no bloco {nome_bloco} via 'a-offscreen': {offscreen.text}")
            return parse_brl(offscreen.text)
        
        whole = block.find('span', class_='a-price-whole')
        frac = block.find('span', class_='a-price-fraction')
        if whole:
            w = re.sub(r'[^\d]', '', whole.text)
            f = re.sub(r'[^\d]', '', frac.text) if frac else '00'
            if w: 
                print(f"   [DEBUG] Encontrado no bloco {nome_bloco} via 'a-price-whole': {w},{f}")
                return f"{w}.{f}"
        
        print(f"   [DEBUG] Tentando Parse_BRL direto no texto do bloco {nome_bloco}...")
        return parse_brl(block.text)

    safe_zones = [
        ('centerCol', soup.find('div', id='centerCol')),
        ('rightCol', soup.find('div', id='rightCol')),
        ('desktop_buybox', soup.find('div', id='desktop_buybox')),
        ('buybox', soup.find('div', id='buybox'))
    ]

    if not preco_atual:
        for nome_zona, zone in safe_zones:
            if not zone: continue
            apex = zone.find('span', class_=re.compile(r'apexPriceToPay|priceToPay'))
            if apex:
                print(f"   [DEBUG] Tag apexPriceToPay encontrada na zona {nome_zona}")
                p = extract_from_block(apex, "apexPriceToPay")
                if p: 
                    preco_atual = p
                    break
    
    if not preco_atual:
        for nome_zona, zone in safe_zones:
            if not zone: continue
            core = zone.find('div', id=re.compile(r'corePriceDisplay_desktop_feature_div|corePrice_desktop|corePrice_feature_div'))
            if core:
                print(f"   [DEBUG] Container corePrice encontrado na zona {nome_zona}")
                for price_span in core.find_all('span', class_='a-price'):
                    if 'a-text-price' in price_span.get('class', []): continue
                    p = extract_from_block(price_span, "corePrice > a-price")
                    if p:
                        preco_atual = p
                        break
            if preco_atual: break

    print("🕵️ [DEBUG] Buscando Preço Antigo...")
    match_basis = re.search(r'class="a-text-price"[^>]*>\s*<span class="a-offscreen">R\$\s*([\d.,]+)</span>', html)
    if match_basis:
        print(f"   [DEBUG] Preço antigo achado via Regex basis: {match_basis.group(1)}")
        p_ant = parse_brl(match_basis.group(1))
        if p_ant: preco_antigo = p_ant

    if not preco_antigo:
        for nome_zona, zone in safe_zones:
            if not zone: continue
            basis = zone.find('span', class_=re.compile(r'basisPrice'))
            if basis:
                preco_antigo = extract_from_block(basis, "basisPrice")
                if preco_antigo: break
                
    if not preco_antigo:
        for nome_zona, zone in safe_zones:
            if not zone: continue
            for old_span in zone.find_all('span', class_=re.compile(r'a-text-price|a-strike')):
                pt = old_span.parent.text.lower() if old_span.parent else ""
                if 'de:' in pt or 'a-strike' in old_span.get('class', []) or old_span.find('span', class_='a-strike'):
                    p = extract_from_block(old_span, "a-strike / de:")
                    if p:
                        preco_antigo = p
                        break
            if preco_antigo: break

    if preco_atual and preco_antigo:
        try:
            if float(preco_antigo) <= float(preco_atual):
                print("   [DEBUG] Rejeitado: Preço antigo menor ou igual ao atual.")
                preco_antigo = None
        except: pass

    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    img = soup.find(id='landingImage') or soup.find('img', id='imgBlkFront') or soup.find('img', id='main-image') or soup.find('img', class_='a-dynamic-image')
    foto_url = img.get('data-old-hires') or img.get('src') if img else None
    
    if not foto_url:
        meta_img = soup.find('meta', property='og:image')
        if meta_img: foto_url = meta_img.get('content')
        
    if not foto_url:
        match_img = re.search(r'"large":"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        if match_img: foto_url = match_img.group(1)
    
    print(f"🏁 FIM AMAZON -> Titulo: {titulo[:15]}... | Atual: {preco_atual} | Antigo: {preco_antigo}")
    print("="*50 + "\n")
    
    return {
        "titulo": titulo, 
        "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, 
        "foto_url": foto_url, 
        "link": url
    }

# --- SHOPEE E MAGALU ---
def extrair_shopee(url):
    print("\n" + "="*50)
    print(f"🕵️ INICIANDO RASTREIO SHOPEE")
    print(f"🔗 Link Original: {url}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9',
        'Connection': 'keep-alive'
    }
    
    try:
        resp_resolve = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        url_final = resp_resolve.url
        print(f"🔗 Link Real Encontrado: {url_final}")
    except:
        url_final = url
        print("⚠️ Falha ao resolver link curto. Usando original.")

    url_limpa = url_final.split('?')[0]
    print(f"🧹 URL Limpa para extração: {url_limpa}")

    titulo = "Produto Shopee"
    preco_atual = None
    preco_antigo = None
    foto_url = None

    match = re.search(r'i\.(\d+)\.(\d+)', url_limpa)
    if not match:
        shop_match = re.search(r'shopid=(\d+)', url_limpa, re.IGNORECASE)
        item_match = re.search(r'itemid=(\d+)', url_limpa, re.IGNORECASE)
        if shop_match and item_match:
            shop_id, item_id = shop_match.group(1), item_match.group(1)
        else:
            shop_id, item_id = None, None
    else:
        shop_id, item_id = match.group(1), match.group(2)

    if shop_id and item_id:
        print(f"🎯 ShopID: {shop_id} | ItemID: {item_id}")
        
        print("🕵️ Acionando API Pública da Shopee...")
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
                    
                    p_atual_raw = item_data.get('price')
                    p_antigo_raw = item_data.get('price_before_discount')
                    
                    if p_atual_raw: preco_atual = str(p_atual_raw / 100000)
                    if p_antigo_raw: preco_antigo = str(p_antigo_raw / 100000)
                    
                    foto_id = item_data.get('image')
                    if foto_id: foto_url = f"https://cf.shopee.com.br/file/{foto_id}"
        except Exception as e:
            print(f"❌ Erro na API Pública Shopee: {e}")

    if not preco_atual:
        print("🕵️ Acionando Fallback: Leitura Bruta de Código HTML...")
        try:
            html_resp = requests.get(url_limpa, headers=headers, timeout=15).text
            
            soup = BeautifulSoup(html_resp, 'html.parser')
            t = soup.find('title')
            if t: titulo = t.text.replace(' | Shopee Brasil', '').strip()
            
            match_price = re.search(r'"price":\s*(\d{5,})', html_resp)
            if match_price and not preco_atual:
                preco_atual = str(float(match_price.group(1)) / 100000)
                
            match_old = re.search(r'"price_before_discount":\s*(\d{5,})', html_resp)
            if match_old and not preco_antigo:
                preco_antigo = str(float(match_old.group(1)) / 100000)
                
            if not foto_url:
                i_meta = soup.find('meta', property='og:image')
                if i_meta: foto_url = i_meta['content']
        except: pass

    if SHOPEE_APP_ID and SHOPEE_APP_SECRET:
        print("🔑 Chaves de API detectadas! Solicitando Link Curto de Afiliado...")
        try:
            timestamp = int(time.time())
            path_link = "/api/v2/affiliate/generate_short_link"
            
            base_string_link = f"{SHOPEE_APP_ID}{path_link}{timestamp}"
            sign_link = hmac.new(SHOPEE_APP_SECRET.encode('utf-8'), base_string_link.encode('utf-8'), hashlib.sha256).hexdigest()
            
            api_link_url = f"https://partner.shopeemobile.com{path_link}?partner_id={SHOPEE_APP_ID}&timestamp={timestamp}&sign={sign_link}"
            
            payload = {"originUrl": url_limpa}
            headers_post = {'Content-Type': 'application/json'}
            
            link_resp = requests.post(api_link_url, json=payload, headers=headers_post, timeout=10).json()
            
            if link_resp.get("response") and link_resp["response"].get("shortLink"):
                url = link_resp["response"]["shortLink"]
                print(f"✅ Link de afiliado gerado com sucesso: {url}")
            else:
                erro_api = link_resp.get('error', '')
                msg_api = link_resp.get('message', '')
                print(f"⚠️ AVISO da Shopee: Não foi possível gerar o link de afiliado. Erro: '{erro_api}' - {msg_api}")
        except Exception as e_link:
            print(f"⚠️ Erro interno ao tentar gerar link de afiliado: {e_link}")

    if str(preco_atual) == str(preco_antigo): preco_antigo = None

    print(f"🏁 FIM SHOPEE -> Titulo: {titulo[:15]}... | Atual: {preco_atual} | Antigo: {preco_antigo}")
    print("="*50 + "\n")

    return {
        "titulo": titulo, 
        "preco_atual": formatar_moeda(preco_atual) if preco_atual else "Ver no site", 
        "preco_antigo": formatar_moeda(preco_antigo) if preco_antigo else None, 
        "foto_url": foto_url, 
        "link": url
    }

def extrair_magalu(url):
    return {"titulo": "Produto Magalu", "preco_atual": "Ver no site", "preco_antigo": None, "foto_url": None, "link": url}

def extrair_dados_loja(url, ml_token=None):
    url = url.strip() 
    if "mercadolivre" in url or "meli.la" in url:
        return extrair_mercadolivre(url, ml_token)
    elif "amazon" in url or "amzn.to" in url:
        return extrair_amazon(url, ml_token) 
    elif "shopee" in url or "shp.ee" in url or "shope.ee" in url:
        return extrair_shopee(url)
    elif "magazineluiza" in url or "magalu" in url:
        return extrair_magalu(url)
    return None

# ==========================================
# 🧠 MÓDULO DE INTELIGÊNCIA ARTIFICIAL
# ==========================================
PROMPT_CRIADOR_DINAMICO = """
Aja como um copywriter de WhatsApp focado em alta conversão no Brasil. O tom deve ser persuasivo, direto, maduro e MUITO CRIATIVO. NADA de frases genéricas que servem para qualquer produto.

# 🚨 PASSO 1: ENTENDA O PRODUTO E SEU USO REAL
Produto: {PRODUTO}
REGRA ABSOLUTA: Analise para que serve este produto, quem usa e qual o benefício real ou a dor que ele resolve no dia a dia. A frase DEVE ter total contexto com a utilidade do produto.

# 🚨 PASSO 2: GERAÇÃO DAS FRASES
Gere 8 FRASES INÉDITAS (entre 3 a 10 palavras), divididas RIGOROSAMENTE em dois estilos:

🎯 ESTILO 1: VENDEDOR AGRESSIVO E DIRETO (Exatamente 4 opções)
Foco EXCLUSIVO em urgência real, benefício matador ou queda drástica de preço. Tom profissional e de impacto.
(NÃO COPIE ESTES EXEMPLOS): "O preço despencou de verdade hoje", "Qualidade premium que você precisa ter".

🎯 ESTILO 2: HUMOR CONTEXTUAL E CRIATIVO (Exatamente 4 opções)
Foco: Frases brilhantes, específicas para a utilidade do produto, com humor ácido, cotidiano ou provocativo. Pense no uso real do produto e faça uma piada madura ou comentário sagaz sobre isso.
(USE ESTES COMO INSPIRAÇÃO DO NÍVEL EXIGIDO):
- Talheres -> "JÁ PODE PARAR DE COMER COM A MÃO"
- Camisa de Treino -> "NOVO INCENTIVO PRA TU IR TREINAR"
- Perfume Masculino Doce -> "CHEIRINHO DE HOMEM QUE NÃO PRESTA"
- Sabonete Facial -> "JÁ FEZ SUA SKIN CARE HOJE?"
- Tênis -> "SÓ PRA QUEM TEM ESTILO"
- Cueca Boxer -> "CHEGA DE USAR ASA DELTA"
- Lavadora de Alta Pressão -> "PRA DAR AQUELE TRATO NO SEU FUSCA"
        
# 🚨 REGRAS DE OURO MÁXIMAS: 
1. É ESTRITAMENTE PROIBIDO criar frases genéricas como "MUITO BARATO HOJE" no Estilo 2. O Estilo 2 precisa ser sobre O PRODUTO.
2. SEM PONTO DE EXCLAMAÇÃO (!).
3. PALAVRAS E EXPRESSÕES EXPRESSAMENTE PROIBIDAS: JAMAIS use "preço camarada", "sem estourar o orçamento", "precinho", "cabe no bolso", "oferta imperdível", "estoque ilimitado", "estoque limitado", "últimas unidades" ou frases bobas.
4. O usuário ODEIA e reprovou as seguintes frases no passado (NUNCA as repita):
{EXEMPLOS_NEGATIVOS}

# ✅ INSPIRAÇÃO GERAL (Frases que o usuário gostou): 
{EXEMPLOS_POSITIVOS}
"""

PROMPT_JUIZ_EDITOR = """
Você é o Editor-Chefe.
Sua missão é extrair as frases geradas e formatar estritamente no JSON solicitado.
OBRIGATÓRIO: O Array 'frases_vendedor' DEVE conter EXATAMENTE 4 frases e o Array 'frases_zoeira' DEVE conter EXATAMENTE 4 frases criativas e com contexto total ao produto. 
As frases devem ter entre 3 a 10 palavras e não conter ponto de exclamação.
É ESTRITAMENTE PROIBIDO aprovar frases bobinhas, sem graça, repetitivas ou com as expressões: "preço camarada", "sem estourar o orçamento", "cabe no bolso", "oferta imperdível", "estoque", "últimas unidades".
Também é proibido aprovar frases parecidas com estas que o usuário já reprovou:
{EXEMPLOS_NEGATIVOS}

# PRODUTO ORIGINAL: {PRODUTO}
# RASCUNHOS GERADOS: {FRASES_CANDIDATAS}
# REGRA DO TÍTULO: OBRIGATÓRIO iniciar com o TIPO DO PRODUTO (ex: "Celular", "Esmerilhadeira", "Notebook"). MANTENHA a Marca, o Modelo, a quantidade (se for Kit) e Destaque 1 a 2 ESPECIFICAÇÕES TÉCNICAS RELEVANTES (ex: "GPS Integrado", "256GB"). REMOVA palavras inúteis de enfeite (ex: "Original", "Premium"). Formate TUDO separando por hífen (Ex: Tipo do Produto - Marca Modelo - Especificação 1).
# REGRA DA QUANTIDADE: Identifique a quantidade de PRODUTOS para fins de cálculo de custo por unidade. ATENÇÃO: NUNCA conte peças internas de um jogo (ex: "Dominó 28 Peças" = 1), peças de um conjunto (ex: "Jogo de Panelas 5 Peças" = 1), ferramentas de um estojo ou acessórios. MUITO IMPORTANTE: Itens vendidos em "Pares" (ex: meias, sapatos, brincos) contam como 1 unidade. Se o kit diz "3 Pares", a quantidade total é 3. SÓ FRACIONE se for um kit de produtos idênticos repetidos. Retorne apenas o número inteiro.
"""

def executar_pipeline_universal(nome_produto):
    fallback_frases = [
        "PREÇO BOM DEMAIS PRA DEIXAR PASSAR", 
        "RESOLVE A VIDA NO DIA A DIA", 
        "MUITO BARATO HOJE", 
        "VOCÊ NÃO PODE PERDER ESSA"
    ]
    
    try:
        positivos, negativos = carregar_exemplos()
        texto_positivos = "\n".join(positivos)
        texto_negativos = "\n".join(negativos)
        
        prompt_gpt = PROMPT_CRIADOR_DINAMICO.replace("{PRODUTO}", nome_produto).replace("{EXEMPLOS_POSITIVOS}", texto_positivos).replace("{EXEMPLOS_NEGATIVOS}", texto_negativos)
        
        candidatas_brutas = None
        if openai_client:
            for _ in range(3):
                try:
                    resp_gpt = openai_client.chat.completions.create(
                        model="gpt-4o-mini", 
                        messages=[{"role":"user","content":prompt_gpt}], 
                        temperature=0.75
                    )
                    candidatas_brutas = resp_gpt.choices[0].message.content
                    break
                except Exception as e: 
                    time.sleep(1)
        
        if not candidatas_brutas: 
            return fallback_frases, nome_produto, 1

        prompt_editor = PROMPT_JUIZ_EDITOR.replace("{PRODUTO}", nome_produto).replace("{FRASES_CANDIDATAS}", candidatas_brutas).replace("{EXEMPLOS_NEGATIVOS}", texto_negativos)
        schema = {
            "type": "OBJECT", 
            "properties": {
                "frases_vendedor": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Exatamente 4 frases de vendas diretas"},
                "frases_zoeira": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Exatamente 4 frases focadas em humor acido e deboche"},
                "titulo_resumido": {"type": "STRING"},
                "quantidade_itens": {"type": "INTEGER", "description": "Quantas unidades vêm no pacote? (Padrão: 1)"}
            }, 
            "required": ["frases_vendedor", "frases_zoeira", "titulo_resumido", "quantidade_itens"]
        }
        
        gemini_payload = {
            "contents": [{"parts": [{"text": prompt_editor}]}], 
            "generationConfig": {
                "temperature": 0.4, 
                "responseMimeType": "application/json", 
                "responseSchema": schema
            }
        }
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
        
        for _ in range(3):
            try:
                r = requests.post(gemini_url, json=gemini_payload, timeout=15)
                if r.status_code == 200:
                    dados_api = r.json()
                    texto_resposta = dados_api['candidates'][0]['content']['parts'][0]['text']
                    
                    match = re.search(r'\{.*\}', texto_resposta, re.DOTALL)
                    if match:
                        try:
                            dados = json.loads(match.group(0))
                            
                            vendedor = dados.get("frases_vendedor", [])
                            zoeira = dados.get("frases_zoeira", [])
                            todas_frases = []
                            
                            for i in range(max(len(zoeira), len(vendedor))):
                                if i < len(zoeira): todas_frases.append(zoeira[i])
                                if i < len(vendedor): todas_frases.append(vendedor[i])
                            
                            if not todas_frases:
                                todas_frases = fallback_frases
                                
                            frases_limpas = [f.replace('!', '').replace('"', '').upper() for f in todas_frases]
                            qtd_ext = dados.get("quantidade_itens", 1)
                            return frases_limpas, dados.get("titulo_resumido", nome_produto), qtd_ext
                        except json.JSONDecodeError:
                            pass
            except Exception as e: 
                time.sleep(1)
    except Exception as e: 
        pass
        
    return fallback_frases, nome_produto, 1

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
                    texto_atual = texto_atual.replace('🔥', f'🔥 (R$ {novo_p_un}/unidade)')
            
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

def cb_usar_salvar_frase(titulo_produto):
    frase = st.session_state.get('input_frase_custom', '')
    if frase.strip():
        linhas = st.session_state.get('area_edicao', '').split('\n')
        if linhas: 
            frase_formatada = frase.strip().strip('*').upper()
            linhas[0] = f"*{frase_formatada}*"
            novo_texto = '\n'.join(linhas)
            st.session_state.texto_final_zap = novo_texto
            st.session_state.area_edicao = novo_texto
        registrar_feedback(frase_formatada, titulo_produto, 1)
        st.session_state['input_frase_custom'] = ""

def cb_trocar_frase(nova_frase):
    linhas = st.session_state.get('area_edicao', '').split('\n')
    if linhas: 
        frase_limpa = nova_frase.strip().strip('*')
        linhas[0] = f"*{frase_limpa}*"
        novo_texto = '\n'.join(linhas)
        st.session_state.texto_final_zap = novo_texto
        st.session_state.area_edicao = novo_texto

def cb_aplicar_selo(selo):
    texto = st.session_state.get('area_edicao', '')
    linhas = texto.split('\n')
    selos_possiveis = ["⚡Oferta relâmpago⚡", "💥Oferta imperdível💥", "🏴‍☠️ Preço de Bug 😱"]
    for i, linha in enumerate(linhas):
        if 'por r$' in linha.lower():
            # Limpa qualquer selo antigo que já esteja na linha
            for s in selos_possiveis:
                linha = linha.replace(f" _({s})_", "").replace(f" _({s})", "").replace(f"({s})", "").strip()
            # Adiciona o novo selo no final da linha
            linhas[i] = linha + f" _({selo})_"
            break
    novo_texto = '\n'.join(linhas)
    st.session_state.texto_final_zap = novo_texto
    st.session_state.area_edicao = novo_texto

# ==========================================
# 🌐 INTERFACE WEB (STREAMLIT) E ESTADO
# ==========================================
st.set_page_config(page_title="Oráculo Gerador", page_icon="🔮", layout="wide")

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
                st.session_state['frases_salvas'] = item['frases']
                st.session_state.texto_final_zap = item['txt_zap']
                st.session_state.area_edicao = item['txt_zap']
                st.session_state['cupom_codigo'] = ""
                st.session_state['cupom_tipo'] = "% Porcentagem"
                st.session_state['cupom_valor'] = 0.0
                st.session_state['cupom_max'] = 0.0
                st.session_state['cupom_local'] = "Nenhum"
                st.rerun()
    else: st.caption("Nenhum histórico salvo. Eles são resetados ao fechar a guia.")

st.title("🔮 Oráculo Web - Módulo Pro")
st.markdown("Crie postagens perfeitas com IA.")

link_input = st.text_input("🔗 Link do Produto (ML, Amazon, Shopee, Magalu):")

if st.button("🚀 Gerar Postagem", type="primary", use_container_width=True):
    if not link_input: st.warning("Insira um link!")
    else:
        st.session_state['cupom_codigo'] = ""
        st.session_state['cupom_tipo'] = "% Porcentagem"
        st.session_state['cupom_valor'] = 0.0
        st.session_state['cupom_max'] = 0.0
        st.session_state['cupom_local'] = "Nenhum"

        with st.spinner("Decodificando a loja..."):
            produto = extrair_dados_loja(link_input, ml_token=ml_token_input)
            if not produto: produto = {"titulo": "Produto Não Identificado", "preco_atual": "Ver no site", "preco_antigo": None, "foto_url": None, "link": link_input}
            
            frases, titulo_resumo, qtd_itens = executar_pipeline_universal(produto["titulo"])
            produto['quantidade'] = qtd_itens
            
            frase_vencedora = frases[0] if frases else "BAIXOU MUITO HOJE"
            
            txt_zap = f"*{frase_vencedora}*\n\n🔮 {titulo_resumo}\n\n"
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
            st.session_state['frases_salvas'] = frases
            st.session_state.texto_final_zap = txt_zap
            st.session_state.area_edicao = txt_zap
            st.session_state.historico.insert(0, {'produto': produto, 'txt_zap': txt_zap, 'frases': frases})
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
        
        st.markdown("🔖 **Selos Rápidos (Adiciona ao lado do preço):**")
        col_s1, col_s2, col_s3 = st.columns(3)
        if col_s1.button("⚡ Oferta relâmpago", use_container_width=True, on_click=cb_aplicar_selo, args=("⚡Oferta relâmpago⚡",)): pass
        if col_s2.button("💥 Oferta imperdível", use_container_width=True, on_click=cb_aplicar_selo, args=("💥Oferta imperdível💥",)): pass
        if col_s3.button("🏴‍☠️ Preço de Bug", use_container_width=True, on_click=cb_aplicar_selo, args=("🏴‍☠️ Preço de Bug 😱",)): pass

    st.markdown("---")
    st.markdown("### 🧠 Treine a IA ou troque de frase:")
    
    c_nova1, c_nova2 = st.columns([10, 2])
    with c_nova1:
        st.text_input("Crie sua própria frase:", placeholder="Ex: PREÇO IMPERDÍVEL HOJE", label_visibility="collapsed", key="input_frase_custom")
    with c_nova2:
        if st.button("➕ Usar e Salvar", use_container_width=True, on_click=cb_usar_salvar_frase, args=(produto_salvo.get('titulo', ''),)):
            if st.session_state.get('input_frase_custom', '').strip():
                st.toast("✅ Frase aplicada e salva na memória!")

    for i, f in enumerate(st.session_state.get('frases_salvas', [])[1:]):
        c_frase, c_up, c_down = st.columns([10, 1, 1])
        with c_frase:
            if st.button(f"🔄 {f}", key=f"btn_trocar_frase_{i}", use_container_width=True, on_click=cb_trocar_frase, args=(f,)):
                pass # Bypass automático
        with c_up:
            if st.button("👍", key=f"btn_up_{i}"): registrar_feedback(f, produto_salvo.get('titulo', ''), 1); st.toast("✅ Aprendido e Salvo!")
        with c_down:
            if st.button("👎", key=f"btn_down_{i}"): registrar_feedback(f, produto_salvo.get('titulo', ''), 0); st.toast("❌ Evitado e Salvo!")
