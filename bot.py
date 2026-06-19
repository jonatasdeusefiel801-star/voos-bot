"""
Bot WhatsApp de monitoramento de voos RJ <-> SP.
Integração: Evolution API  |  Backend: Flask + Playwright
"""
import os
import re
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request

from search import RJ, SP, SIGLA, buscar_paralelo

app = Flask(__name__)

# ── Config via variáveis de ambiente ─────────────────────────────────────────
EVOLUTION_URL      = os.environ.get('EVOLUTION_URL', '').rstrip('/')
EVOLUTION_KEY      = os.environ.get('EVOLUTION_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'voos')

# Número autorizado (deixe vazio para aceitar qualquer número)
NUMERO_AUTORIZADO = os.environ.get('NUMERO_AUTORIZADO', '')

# ── Evolution API ─────────────────────────────────────────────────────────────
def _send(to: str, text: str):
    """Envia mensagem de texto via Evolution API."""
    url = f'{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}'
    try:
        requests.post(
            url,
            json={'number': to, 'text': text},
            headers={'apikey': EVOLUTION_KEY, 'Content-Type': 'application/json'},
            timeout=12,
        )
    except Exception as e:
        print(f'[send] Erro: {e}')


# ── Parsing ───────────────────────────────────────────────────────────────────
def _parse_data(text: str) -> str | None:
    """Extrai data do texto. Retorna ISO YYYY-MM-DD ou None."""
    t = text.strip().lower()
    hoje = datetime.now().date()

    if 'hoje' in t:
        return hoje.strftime('%Y-%m-%d')
    if 'amanhã' in t or 'amanha' in t:
        return (hoje + timedelta(days=1)).strftime('%Y-%m-%d')
    if 'depois de amanhã' in t or 'depois de amanha' in t:
        return (hoje + timedelta(days=2)).strftime('%Y-%m-%d')

    # DD/MM/YYYY, DD/MM, DD-MM-YYYY, DD-MM
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
    """Detecta sentido: RJ→SP, SP→RJ ou Ambos."""
    t = text.lower()
    tem_rj = any(w in t for w in ['gig', 'sdu', ' rj', 'rio', 'galeão', 'galeao', 'dumont'])
    tem_sp = any(w in t for w in ['cgh', 'gru', ' sp', 'são paulo', 'sao paulo', 'congonhas', 'guarulhos'])
    if tem_rj and not tem_sp:
        return 'RJ→SP'
    if tem_sp and not tem_rj:
        return 'SP→RJ'
    return 'Ambos'


# ── Formatação de resposta ────────────────────────────────────────────────────
def _formatar(todos: list, data_iso: str, sentido: str) -> str:
    data_br = datetime.strptime(data_iso, '%Y-%m-%d').strftime('%d/%m/%Y')

    if not todos:
        return (
            f'✈️ Nenhum voo direto encontrado para *{data_br}*.\n'
            'Tente outra data ou outro sentido (ex: `rj 20/06` ou `sp 20/06`).'
        )

    # Menor por rota
    por_rota: dict = {}
    for v in todos:
        key = f"{v['origem']}→{v['destino']}"
        if key not in por_rota or v['preco'] < por_rota[key]['preco']:
            por_rota[key] = v

    linhas = [f'✈️ *Voos diretos — {data_br}* ({sentido})\n']
    for rota, v in sorted(por_rota.items(), key=lambda x: x[1]['preco']):
        preco_fmt = f"R$ {v['preco']:,}".replace(',', '.')
        linhas.append(f'*{rota}*')
        linhas.append(f"  {v['cia']}  {v['partida']} → {v['chegada']} ({v['duracao']})")
        linhas.append(f'  💰 {preco_fmt}')
        linhas.append('')

    melhor = min(todos, key=lambda v: v['preco'])
    preco_melhor = f"R$ {melhor['preco']:,}".replace(',', '.')
    linhas.append(
        f"🏆 Menor preço: *{preco_melhor}*  "
        f"({melhor['origem']}→{melhor['destino']} · {melhor['cia']})"
    )
    return '\n'.join(linhas)


HELP_TEXT = """\
✈️ *Monitor de Voos RJ ↔ SP*

Envie uma data para ver os voos diretos mais baratos:

• `20/06` — voos para dia 20/06
• `hoje` — voos para hoje
• `amanhã` — voos para amanhã
• `rj 25/06` — somente RJ → SP
• `sp 25/06` — somente SP → RJ

Sem prefixo busco nos dois sentidos. \
A busca leva ~30s — aguarde a resposta ✅"""


# ── Background worker ─────────────────────────────────────────────────────────
def _buscar_e_responder(numero: str, data_iso: str, sentido: str):
    if sentido == 'RJ→SP':
        rotas = [(o, d) for o in RJ for d in SP]
    elif sentido == 'SP→RJ':
        rotas = [(o, d) for o in SP for d in RJ]
    else:
        rotas = [(o, d) for o in RJ for d in SP] + [(o, d) for o in SP for d in RJ]

    try:
        todos = buscar_paralelo(rotas, data_iso, workers=3)
    except Exception as e:
        _send(numero, f'❌ Erro na busca: {e}')
        return

    _send(numero, _formatar(todos, data_iso, sentido))


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}

    # Filtra apenas mensagens recebidas
    if data.get('event') != 'messages.upsert':
        return jsonify({'ok': True})

    msg_data = data.get('data', {})
    key = msg_data.get('key', {})

    if key.get('fromMe', False):
        return jsonify({'ok': True})

    remote_jid = key.get('remoteJid', '')
    # Ignora grupos
    if '@g.us' in remote_jid:
        return jsonify({'ok': True})

    numero = remote_jid.replace('@s.whatsapp.net', '')

    # Verifica autorização (se configurado)
    if NUMERO_AUTORIZADO and numero != NUMERO_AUTORIZADO.lstrip('+').replace(' ', ''):
        return jsonify({'ok': True})

    message = msg_data.get('message', {})
    texto = (
        message.get('conversation') or
        message.get('extendedTextMessage', {}).get('text') or
        ''
    ).strip()

    if not texto:
        return jsonify({'ok': True})

    texto_lower = texto.lower()

    # Comandos de ajuda
    if any(w in texto_lower for w in ['ajuda', 'help', 'oi', 'olá', 'ola', 'menu', 'inicio', 'início']):
        _send(numero, HELP_TEXT)
        return jsonify({'ok': True})

    # Busca por data
    data_iso = _parse_data(texto)
    if not data_iso:
        _send(numero, '❓ Não entendi a data.\nExemplos: `20/06`, `amanhã`, `hoje`\nDigite *ajuda* para mais opções.')
        return jsonify({'ok': True})

    sentido = _parse_sentido(texto)
    data_br = datetime.strptime(data_iso, '%Y-%m-%d').strftime('%d/%m/%Y')

    _send(numero, f'🔍 Buscando voos *{sentido}* para *{data_br}*...\nAguarde ~30 segundos ⏳')

    threading.Thread(
        target=_buscar_e_responder,
        args=(numero, data_iso, sentido),
        daemon=True,
    ).start()

    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Bot iniciado na porta {port}')
    app.run(host='0.0.0.0', port=port)
