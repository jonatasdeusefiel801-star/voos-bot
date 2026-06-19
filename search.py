"""
Busca de voos diretos RJ <-> SP via Google Flights (Playwright headless).
Implementações extraídas de voos_monitor.py.
"""
import base64
import re
import threading
from queue import Queue, Empty

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Rotas ────────────────────────────────────────────────────────────────────
RJ = ['GIG', 'SDU']
SP = ['CGH', 'GRU']
DURACAO_MAX_MIN = 150

SIGLA = {
    'GIG': 'Galeão (GIG)',
    'SDU': 'Santos Dumont (SDU)',
    'CGH': 'Congonhas (CGH)',
    'GRU': 'Guarulhos (GRU)',
}

# ── Chromium ─────────────────────────────────────────────────────────────────
UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['pt-BR','pt','en-US']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
    "window.chrome={runtime:{}};"
)

CHROMIUM_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--disable-extensions', '--disable-background-networking',
    '--disable-features=Translate,BackForwardCache,InterestFeedContentSuggestions',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--mute-audio', '--no-first-run', '--no-default-browser-check',
    '--single-process', '--disable-setuid-sandbox',
]

_BLOQUEAR_TIPOS = {'image', 'media', 'font', 'stylesheet'}


def _bloquear_recursos(route, request):
    if request.resource_type in _BLOQUEAR_TIPOS:
        route.abort()
    else:
        try:
            route.continue_()
        except Exception:
            pass


# ── URL Google Flights ────────────────────────────────────────────────────────
def _airport_bytes(iata: str) -> bytes:
    c = iata.encode('ascii')
    return b'\x08\x01\x12' + bytes([len(c)]) + c


def gerar_tfs(origin: str, destination: str, date_str: str) -> str:
    """Gera parâmetro tfs (protobuf base64url) para URL do Google Flights.
    date_str: formato YYYY-MM-DD
    """
    db = date_str.encode('ascii')
    ob = _airport_bytes(origin)
    de = _airport_bytes(destination)
    leg = b'\x12' + bytes([len(db)]) + db + b'\x6a' + bytes([len(ob)]) + ob + b'\x72' + bytes([len(de)]) + de
    root = b'\x08\x1c\x10\x02\x1a' + bytes([len(leg)]) + leg
    return base64.urlsafe_b64encode(root).decode('ascii').rstrip('=')


def url_gf(origin: str, destination: str, date_str: str) -> str:
    return (
        f'https://www.google.com/travel/flights/search'
        f'?tfs={gerar_tfs(origin, destination, date_str)}'
        f'&hl=pt-BR&curr=BRL&tfu=EgQIARAAOAFQAQ%3D%3D'
    )


# ── Parsing de cards ──────────────────────────────────────────────────────────
def _fmt_dur(m: int) -> str:
    h, mn = divmod(m, 60)
    return f'{h}h {mn:02d}m' if h else f'{mn}min'


def _parse_card(texto: str, ori: str, dst: str, data: str, link: str) -> dict | None:
    t = texto.replace('\xa0', ' ').replace('–', '-').replace('—', '-')
    linhas = [l.strip() for l in t.split('\n') if l.strip()]

    if not linhas:
        return None

    horarios = [l for l in linhas if re.fullmatch(r'\d{2}:\d{2}', l)]
    partida  = horarios[0] if horarios else '?'
    chegada  = horarios[1] if len(horarios) > 1 else '?'

    cia = 'N/D'
    for l in linhas:
        if re.fullmatch(r'\d{2}:\d{2}', l) or re.fullmatch(r'[-\s]+', l):
            continue
        if re.search(r'\d+h', l) and 'emis' not in l.lower() and 'co2' not in l.lower():
            break
        if l and not re.search(r'R\$|kg|%|GIG|CGH|GRU|SDU|escala|Sem|\d', l, re.I):
            cia = l
            break

    dur_txt, dur_min = '', 9999
    for l in linhas:
        m = re.search(r'(\d+)\s*h(?:\s*(\d+)\s*min?)?', l)
        if m and 'co2' not in l.lower():
            dur_min = int(m.group(1)) * 60 + int(m.group(2) or 0)
            dur_txt = _fmt_dur(dur_min)
            break

    m = re.search(r'R\$\s*([\d.,]+)', t)
    preco = int(re.sub(r'[^\d]', '', m.group(1))) if m else 0
    if preco < 50:
        return None

    return {
        'preco': preco, 'cia': cia,
        'partida': partida, 'chegada': chegada,
        'duracao': dur_txt or _fmt_dur(dur_min),
        'duracao_min': dur_min,
        'origem': ori, 'destino': dst,
        'data': data, 'url': link,
    }


# ── Busca no browser ──────────────────────────────────────────────────────────
def _buscar_no_browser(browser, ori: str, dst: str, data: str,
                       on_log=None, sel_timeout: int = 20_000,
                       extra_espera: int = 0, debug: bool = False):
    link = url_gf(ori, dst, data)
    debug_info = {'url': link, 'title': '', 'cards': 0, 'error': '', 'parsed': 0}

    voos = []
    ctx = None
    try:
        ctx = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent=UA, locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            ignore_https_errors=True,
        )
        ctx.set_extra_http_headers({'Accept-Language': 'pt-BR,pt;q=0.9'})
        ctx.route('**/*', _bloquear_recursos)
        page = ctx.new_page()
        page.add_init_script(STEALTH_JS)

        try:
            page.goto(link, wait_until='domcontentloaded', timeout=30_000)
        except PWTimeout:
            pass

        debug_info['title'] = page.title()
        print(f'[search] {ori}→{dst} | title={debug_info["title"][:60]} | url={page.url[:80]}', flush=True)

        try:
            page.wait_for_selector('.pIav2d', timeout=sel_timeout)
        except PWTimeout:
            debug_info['error'] = 'timeout .pIav2d'
            print(f'[search] {ori}→{dst} | TIMEOUT esperando .pIav2d', flush=True)

        # Espera adaptativa
        prev = -1
        for _ in range(14):
            n = len(page.query_selector_all('.pIav2d'))
            if n > 0 and n == prev:
                break
            prev = n
            page.wait_for_timeout(300)

        if extra_espera > 0:
            page.wait_for_timeout(extra_espera)

        cards = page.query_selector_all('.pIav2d')
        debug_info['cards'] = len(cards)

        for card in cards:
            try:
                v = _parse_card(card.inner_text(), ori, dst, data, link)
                if v and v['duracao_min'] <= DURACAO_MAX_MIN:
                    voos.append(v)
            except Exception:
                pass

        debug_info['parsed'] = len(voos)
        print(f'[search] {ori}→{dst} | cards={debug_info["cards"]} parsed={len(voos)}', flush=True)

        if on_log:
            on_log(f'{ori}→{dst} {data}: {len(voos)} voos')

    except Exception as e:
        debug_info['error'] = str(e)[:120]
        print(f'[search] {ori}→{dst} | ERRO: {e}', flush=True)
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass

    if debug:
        return voos, debug_info
    return voos


def buscar_paralelo(rotas: list, data: str, on_log=None, workers: int = 3) -> list:
    """Busca múltiplas rotas para uma data, um browser por worker."""
    fila = Queue()
    for o, d in rotas:
        fila.put((o, d, data))

    resultados = []
    lock = threading.Lock()

    def worker():
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                while True:
                    try:
                        ori, dst, dt = fila.get_nowait()
                    except Empty:
                        break
                    try:
                        voos = _buscar_no_browser(browser, ori, dst, dt, on_log)
                    except Exception as e:
                        print(f'[worker] erro {ori}→{dst}: {e}', flush=True)
                        voos = []
                    with lock:
                        resultados.extend(voos)
                    fila.task_done()
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    n = min(workers, max(1, len(rotas)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return resultados


def buscar_debug(ori: str, dst: str, data: str) -> dict:
    """Busca uma rota retornando info de diagnóstico."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            voos, info = _buscar_no_browser(browser, ori, dst, data, debug=True)
            info['voos'] = len(voos)
            return info
        finally:
            try:
                browser.close()
            except Exception:
                pass


def tirar_screenshot(ori: str, dst: str, data: str) -> bytes | None:
    """Abre Google Flights e retorna screenshot PNG como bytes."""
    link = url_gf(ori, dst, data)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            ctx = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent=UA, locale='pt-BR',
                timezone_id='America/Sao_Paulo',
                ignore_https_errors=True,
            )
            ctx.set_extra_http_headers({'Accept-Language': 'pt-BR,pt;q=0.9'})
            page = ctx.new_page()
            page.add_init_script(STEALTH_JS)
            try:
                page.goto(link, wait_until='domcontentloaded', timeout=35_000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
            print(f'[screenshot] title={page.title()} url={page.url[:80]}', flush=True)
            return page.screenshot(full_page=False)
        finally:
            try:
                browser.close()
            except Exception:
                pass
