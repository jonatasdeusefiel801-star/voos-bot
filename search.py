"""
Busca de voos diretos RJ <-> SP via Google Flights (Playwright headless).
"""
import base64
import os
import re
import struct
import threading
from queue import Queue, Empty

from playwright.sync_api import sync_playwright

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

# ── Chromium ──────────────────────────────────────────────────────────────────
CHROMIUM_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-sync',
    '--disable-translate',
    '--hide-scrollbars',
    '--mute-audio',
    '--no-first-run',
    '--safebrowsing-disable-auto-update',
    '--disable-setuid-sandbox',
    '--single-process',
]

_BLOQUEAR_TIPOS = {'image', 'media', 'font', 'stylesheet'}


def _bloquear_recursos(route, request):
    if request.resource_type in _BLOQUEAR_TIPOS:
        route.abort()
    else:
        route.continue_()


def _tfs(ori: str, dst: str, data: str) -> str:
    """Gera parâmetro tfs (protobuf base64url) para URL do Google Flights."""
    year, month, day = map(int, data.split('-'))

    def s(field, val):
        v = val.encode()
        return bytes([(field << 3) | 2, len(v)]) + v

    def i(field, val):
        return bytes([(field << 3) | 0, val])

    def n(field, payload):
        return bytes([(field << 3) | 2, len(payload)]) + payload

    date_msg = i(1, year) + i(2, month) + i(3, day)
    leg = s(1, ori) + s(2, dst) + n(3, date_msg) + i(9, 1)
    return base64.urlsafe_b64encode(n(1, leg)).decode().rstrip('=')


def _parse_card(card) -> dict | None:
    try:
        cia_el = card.query_selector('.Ir0Voe .sSHqwe')
        cia = cia_el.inner_text().strip() if cia_el else ''

        times = card.query_selector_all('.zxVSec .OcaeR span:not(.EfT7Ae)')
        partida = times[0].inner_text().strip() if len(times) > 0 else ''
        chegada = times[1].inner_text().strip() if len(times) > 1 else ''

        dur_el = card.query_selector('.gvkrdb')
        dur_txt = dur_el.inner_text().strip() if dur_el else ''

        dur_min = 0
        m = re.search(r'(\d+)\s*h', dur_txt)
        if m:
            dur_min += int(m.group(1)) * 60
        m = re.search(r'(\d+)\s*min', dur_txt)
        if m:
            dur_min += int(m.group(1))

        if dur_min > DURACAO_MAX_MIN:
            return None

        stops_el = card.query_selector('.EfT7Ae span')
        stops_txt = stops_el.inner_text().strip() if stops_el else ''
        if stops_txt and 'direto' not in stops_txt.lower() and 'nonstop' not in stops_txt.lower():
            return None

        preco_el = card.query_selector('.FpEdX span, .YMlIz span')
        preco_txt = preco_el.inner_text().strip() if preco_el else ''
        preco = int(re.sub(r'[^\d]', '', preco_txt))
        if preco <= 0:
            return None

        return {'cia': cia, 'partida': partida, 'chegada': chegada,
                'duracao': dur_txt, 'preco': preco}
    except Exception:
        return None


def _buscar_no_browser(browser, ori, dst, data, on_log=None,
                       sel_timeout=25_000, extra_espera=0, debug=False):
    tfs = _tfs(ori, dst, data)
    link = f'https://www.google.com/travel/flights/search?tfs={tfs}&hl=pt-BR&curr=BRL'

    ctx = browser.new_context(
        locale='pt-BR',
        timezone_id='America/Sao_Paulo',
        user_agent=(
            'Mozilla/5.0 (X11; Linux x86_64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        viewport={'width': 1366, 'height': 768},
        extra_http_headers={'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8'},
        java_script_enabled=True,
        bypass_csp=True,
    )
    debug_info = {'url': link, 'title': '', 'cards': 0, 'error': '', 'parsed': 0}
    try:
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )
        ctx.route('**/*', _bloquear_recursos)
        page = ctx.new_page()

        try:
            page.goto(link, wait_until='domcontentloaded', timeout=35_000)
            debug_info['title'] = page.title()
            print(f'[search] {ori}→{dst} | title={debug_info["title"]} | url={page.url[:80]}', flush=True)
            page.wait_for_selector('.pIav2d', timeout=sel_timeout)
        except Exception as e:
            debug_info['error'] = str(e)[:120]
            print(f'[search] {ori}→{dst} | FALHOU: {debug_info["error"]}', flush=True)
            if debug:
                return [], debug_info
            return []

        if extra_espera:
            page.wait_for_timeout(extra_espera)

        prev = -1
        for _ in range(14):
            n = len(page.query_selector_all('.pIav2d'))
            if n > 0 and n == prev:
                break
            prev = n
            page.wait_for_timeout(300)

        debug_info['cards'] = len(page.query_selector_all('.pIav2d'))

        voos = []
        for card in page.query_selector_all('.pIav2d'):
            v = _parse_card(card)
            if v:
                v['origem']  = ori
                v['destino'] = dst
                v['data']    = data
                voos.append(v)

        debug_info['parsed'] = len(voos)
        print(f'[search] {ori}→{dst} | cards={debug_info["cards"]} parsed={len(voos)}', flush=True)

        if on_log:
            on_log(f'{ori}→{dst} {data}: {len(voos)} voos')

        if debug:
            return voos, debug_info
        return voos
    finally:
        try:
            ctx.close()
        except Exception:
            pass


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
    tfs = _tfs(ori, dst, data)
    link = f'https://www.google.com/travel/flights/search?tfs={tfs}&hl=pt-BR&curr=BRL'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            ctx = browser.new_context(
                locale='pt-BR',
                timezone_id='America/Sao_Paulo',
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 800},
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = ctx.new_page()
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
                    except Exception:
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
