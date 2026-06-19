"""
Bot Telegram de monitoramento de voos RJ <-> SP.
Integração: Telegram Bot API  |  Backend: Flask + Playwright
"""
import os
import re
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request

from search import RJ, SP, buscar_paralelo, buscar_debug

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_URL   = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'

def send(chat_id, text):
    try:
        requests.post(f'{TELEGRAM_URL}/sendMessage',
                      json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                      timeout=12)
    except Exception as e:
        print(f'[send] Erro: {e}')


# ── Parsing ───────────────────────────────────────────────────────────────────
def _parse_data(text: str):
    t = text.strip().lower()
    hoje = datetime.now().date()

    if 'hoje' in t:
        return hoje.strftime('%Y-%m-%d')
    if 'amanhã' in t or 'amanha' in t:
        return (hoje + timedelta(days=1)).strftime('%Y-%m-%d')
    if 'depois de amanhã' in t or 'depois de amanha' in t:
        return (hoje + timedelta(days=2)).strftime('%Y-%m-%d')

    m = re.search(r'(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?', t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else hoje.year
        try:
            dt = datetime(y, mo, d).date()
            if dt < hoje:
                dt = dt.replace(year=dt.year + 1)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


def _parse_sentido(text: str) -> str:
    t = text.lower()
    tem_rj = any(w in t for w in ['gig', 'sdu', ' rj', 'rio', 'galeão', 'galeao', 'dumont'])
    tem_sp = any(w in t for w in ['cgh', 'gru', ' sp', 'são paulo', 'sao paulo', 'congonhas', 'guarulhos'])
    if tem_rj and not tem_sp:
        return 'RJ→SP'
    if tem_sp and not tem_rj:
        return 'SP→RJ'
    return 'Ambos'


# ── Formatação ────────────────────────────────────────────────────────────────
def _formatar(todos, data_iso, sentido):
    data_br = datetime.strptime(data_iso, '%Y-%m-%d').strftime('%d/%m/%Y')

    if not todos:
        return (f'✈️ Nenhum voo direto para *{data_br}*.\n'
                'Tente outra data ou sentido (ex: `rj 20/06` ou `sp 20/06`).')

    por_rota = {}
    for v in todos:
        key = f"{v['origem']}→{v['destino']}"
        if key not in por_rota or v['preco'] < por_rota[key]['preco']:
            por_rota[key] = v

    linhas = [f'✈️ *Voos diretos — {data_br}* ({sentido})\n']
    for rota, v in sorted(por_rota.items(), key=lambda x: x[1]['preco']):
        preco = f"R$ {v['preco']:,}".replace(',', '.')
        linhas.append(f'*{rota}*')
        linhas.append(f"  {v['cia']}  {v['partida']} → {v['chegada']} ({v['duracao']})")
        linhas.append(f'  💰 {preco}')
        linhas.append('')

    melhor = min(todos, key=lambda v: v['preco'])
    preco_m = f"R$ {melhor['preco']:,}".replace(',', '.')
    linhas.append(f"🏆 Menor: *{preco_m}*  ({melhor['origem']}→{melhor['destino']} · {melhor['cia']})")
    return '\n'.join(linhas)


HELP_TEXT = """\
✈️ *Monitor de Voos RJ ↔ SP*

Envie uma data para ver voos diretos mais baratos:

• `20/06` — voos para dia 20/06
• `hoje` — voos para hoje
• `amanhã` — voos para amanhã
• `rj 25/06` — somente RJ → SP
• `sp 25/06` — somente SP → RJ

Sem prefixo busco nos dois sentidos.
A busca leva ~30s — aguarde ✅"""


# ── Background ────────────────────────────────────────────────────────────────
def _buscar_e_responder(chat_id, data_iso, sentido):
    if sentido == 'RJ→SP':
        rotas = [(o, d) for o in RJ for d in SP]
    elif sentido == 'SP→RJ':
        rotas = [(o, d) for o in SP for d in RJ]
    else:
        rotas = [(o, d) for o in RJ for d in SP] + [(o, d) for o in SP for d in RJ]

    try:
        todos = buscar_paralelo(rotas, data_iso, workers=3)
    except Exception as e:
        send(chat_id, f'❌ Erro na busca: {e}')
        return

    send(chat_id, _formatar(todos, data_iso, sentido))


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}
    msg  = data.get('message') or data.get('edited_message')
    if not msg:
        return jsonify({'ok': True})

    chat_id = msg['chat']['id']
    texto   = msg.get('text', '').strip()
    if not texto:
        return jsonify({'ok': True})

    tl = texto.lower()

    if any(w in tl for w in ['/start', '/help', 'ajuda', 'oi', 'olá', 'ola', 'menu']):
        send(chat_id, HELP_TEXT)
        return jsonify({'ok': True})

    if tl.startswith('/debug'):
        data_iso = _parse_data(texto[6:].strip()) or datetime.now().strftime('%Y-%m-%d')
        send(chat_id, f'🔧 Debug GIG→CGH para `{data_iso}`...')
        def _run_debug():
            info = buscar_debug('GIG', 'CGH', data_iso)
            msg = (
                f'🔧 *Debug* `GIG→CGH` `{data_iso}`\n'
                f'URL: `{info["url"][-60:]}`\n'
                f'Título: `{info.get("title","?")}`\n'
                f'Cards `.pIav2d`: `{info.get("cards","?")}`\n'
                f'Parsed: `{info.get("parsed","?")}`\n'
                f'Erro: `{info.get("error","nenhum")}`'
            )
            send(chat_id, msg)
        threading.Thread(target=_run_debug, daemon=True).start()
        return jsonify({'ok': True})

    data_iso = _parse_data(texto)
    if not data_iso:
        send(chat_id, '❓ Não entendi a data.\nExemplos: `20/06`, `amanhã`, `hoje`\nDigite /help para mais opções.')
        return jsonify({'ok': True})

    sentido = _parse_sentido(texto)
    data_br = datetime.strptime(data_iso, '%Y-%m-%d').strftime('%d/%m/%Y')

    send(chat_id, f'🔍 Buscando voos *{sentido}* para *{data_br}*...\nAguarde ~30 segundos ⏳')

    threading.Thread(target=_buscar_e_responder,
                     args=(chat_id, data_iso, sentido), daemon=True).start()

    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Bot Telegram iniciado na porta {port}')
    app.run(host='0.0.0.0', port=port)
