from flask import Flask, render_template, jsonify, send_from_directory
import requests
import yt_dlp
import random
import os
import time
from threading import Thread, Event, Lock
from datetime import datetime, timedelta

app = Flask(__name__, template_folder='templates')

# ——————————————————————————————————————————————
# CONFIGURAÇÕES GLOBAIS
# ——————————————————————————————————————————————

LASTFM_API_KEY = '9d7d79a952c5e5805a0decb0ccf1c9fd'

# Lista de vinhetas (caminhos relativos a /static)
vinhetas = [
    "vinhetas/vinheta_milenio.mp3",
    "vinhetas/vinheta_rock.mp3",
    "vinhetas/uma_hora.mp3"
]

# Cronograma: cada bloco tem um estilo (tag Last.fm) e duração em minutos
cronograma = [
    {"estilo": "brazilian rock",   "duracao": 3},
    {"estilo": "alternative rock", "duracao": 3},
    {"estilo": "metalcore",        "duracao": 3},
    {"estilo": "alt-rock",         "duracao": 3},
    {"estilo": "indie rock",       "duracao": 3},
    {"estilo": "brazilian rock",   "duracao": 3},
]

# Estado compartilhado (thread‐safe) da “faixa atual da rádio”
status_lock   = Lock()
status_data   = {
    "tipo": None,            # 'vinheta' ou 'musica'
    "url": None,             # caminho relativo em /static (ex.: "musicas/LinkinPark.mp3" ou "vinhetas/...")
    "nome": None,            # nome da música (ou None, se for vinheta)
    "artista": None,         # nome do artista (ou None)
    "capa": None,            # URL da capa
    "estilo": None,          # estilo Last.fm atual (sempre válido, mesmo durante vinheta)
    "start_time": None       # datetime quando começou esta faixa
}

# Controle de escalonamento
cronograma_index = 0
cronograma_event = Event()

# ——————————————————————————————————————————————
# AUXILIARES (busca Last.fm, download, normalização)
# ——————————————————————————————————————————————

def buscar_musicas_por_estilo(estilo):
    """Consulta Last.fm e retorna lista de (nome, artista) para o tag dado."""
    url = (
        f'http://ws.audioscrobbler.com/2.0/'
        f'?method=tag.gettoptracks'
        f'&tag={estilo}'
        f'&api_key={LASTFM_API_KEY}'
        f'&format=json'
    )
    try:
        resp = requests.get(url)
        data = resp.json()
        if 'tracks' in data and data['tracks'].get('track'):
            return [
                (t['name'], t['artist']['name'])
                for t in data['tracks']['track']
            ]
    except Exception:
        pass
    return []


def normalize_audio(input_file, output_file):
    """Usa ffmpeg para normalizar volume (1.5x) e gerar MP3."""
    command = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', input_file,
        '-filter:a', 'volume=1.5',
        '-acodec', 'libmp3lame', '-q:a', '0',
        output_file
    ]
    # se der erro, a exceção sobe
    import subprocess
    subprocess.run(command, check=True)


def download_music(music_name, artist_name, result_container):
    """
    Baixa e normaliza a música via yt_dlp. 
    Coloca caminho final em result_container['path'] ou None se falhar.
    """
    # gera nome de arquivo “Artist - Music.mp3”
    sanitized = f"{artist_name} - {music_name}"
    for c in ["/", "\\", ":", "!", "?"]:
        sanitized = sanitized.replace(c, "_")
    output_path = os.path.join('static', 'musicas', f"{sanitized}.mp3")

    if os.path.exists(output_path):
        result_container["path"] = output_path
        return True

    ydl_opts = {
        'quiet': True,
        'extract_audio': True,
        'format': 'bestaudio/best',
        'outtmpl': 'static/musicas/%(title)s.%(ext)s',
        'noplaylist': True
    }

    def attempt(query):
        from yt_dlp import YoutubeDL
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch:{query}", download=True)
                if 'entries' in info and info['entries']:
                    video = info['entries'][0]
                    temp_ext = video['ext']  # provavelmente 'webm' ou 'm4a'
                    temp_file = f"static/musicas/{video['title']}.{temp_ext}"
                    final_mp3 = output_path
                    # converte para MP3
                    import subprocess
                    subprocess.run(
                        ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                         '-i', temp_file,
                         '-q:a', '0', '-map', 'a', final_mp3],
                        check=True
                    )
                    os.remove(temp_file)
                    result_container["path"] = final_mp3
                    return True
        except Exception:
            pass
        result_container["path"] = None
        return False

    # tenta com “official music video”, se falhar, tenta “official audio”
    q1 = f"{music_name} {artist_name} official music video"
    if attempt(q1):
        return True
    q2 = f"{music_name} {artist_name} official audio"
    return attempt(q2)


def buscar_capa_do_album(musica, artista):
    """Tenta obter capa pela API track.getInfo do Last.fm. Se falhar, devolve placeholder."""
    url = (
        f"http://ws.audioscrobbler.com/2.0/"
        f"?method=track.getInfo"
        f"&api_key={LASTFM_API_KEY}"
        f"&artist={requests.utils.quote(artista)}"
        f"&track={requests.utils.quote(musica)}"
        f"&format=json"
    )
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        if 'track' in data and data['track'].get('album') and data['track']['album'].get('image'):
            imgs = data['track']['album']['image']
            if imgs and imgs[-1].get('#text'):
                return imgs[-1]['#text']
    except Exception:
        pass
    # se não achar, devolve cenário genérico
    return "https://via.placeholder.com/300?text=Sem+Capa"


# ——————————————————————————————————————————————
# FUNÇÕES PRINCIPAIS DE “RÁDIO”
# ——————————————————————————————————————————————

def rodar_programa(estilo, duracao_minutos):
    """
    Este laço funciona DENTRO do bloco atual do cronograma.
    Ele escolhe aleatoriamente música+artista, faz download e "atualiza" status_data:
      1) sinaliza vinheta
      2) sinaliza música
    Cada etapa registra o start_time e o servidor simplesmente “marca” o status atual.
    O HTML (cliente) irá perguntar a /status e sincronizar.
    """
    global status_data

    fim_programa = datetime.now() + timedelta(minutes=duracao_minutos)
    while datetime.now() < fim_programa:
        # 1) escolhe random música+artista da tag
        musicas = buscar_musicas_por_estilo(estilo)
        if not musicas:
            # sem música, aguarda 5s e repete
            time.sleep(5)
            continue

        musica, artista = random.choice(musicas)
        vinheta_rel = random.choice(vinhetas)  # e.g. "vinhetas/vinheta_rock.mp3"

        # 2) faz download (ou pega do cache) da música
        cont = {"path": None}
        sucesso = download_music(musica, artista, cont)
        if not sucesso or not cont["path"]:
            # falhou no download, pula para o próximo loop
            print(f"[RÁDIO] Falha ao baixar '{musica}' de {artista}. Pulando...")
            time.sleep(5)
            continue

        music_path = cont["path"].replace("\\", "/")  # ex: "static/musicas/Artist - Song.mp3"

        # —————————————— VINHETA ——————————————
        with status_lock:
            status_data["tipo"]       = "vinheta"
            status_data["url"]        = vinheta_rel
            status_data["nome"]       = None
            status_data["artista"]    = None
            status_data["estilo"]     = estilo
            status_data["capa"]       = None
            status_data["start_time"] = datetime.now()

        # Tenta descobrir duração da vinheta pelo ffprobe (em segundos)
        duracao_vinheta = 5.0  # fallback: 5s
        try:
            import subprocess, json
            cmd = [
                'ffprobe', '-v', 'error',
                '-print_format', 'json',
                '-show_entries', 'format=duration',
                os.path.join('static', vinheta_rel)
            ]
            p = subprocess.run(cmd, capture_output=True, text=True)
            j = json.loads(p.stdout)
            duracao_vinheta = float(j['format']['duration'])
        except Exception:
            pass

        time.sleep(duracao_vinheta + 0.1)  # espera a vinheta tocar

        # —————————————— MÚSICA ——————————————
        capa_url = buscar_capa_do_album(musica, artista)

        with status_lock:
            status_data["tipo"]       = "musica"
            status_data["url"]        = music_path[len("static/"):]  # ex: "musicas/Artist - Song.mp3"
            status_data["nome"]       = musica
            status_data["artista"]    = artista
            status_data["estilo"]     = estilo
            status_data["capa"]       = capa_url
            status_data["start_time"] = datetime.now()

        # Tenta descobrir duração da música (em segundos)
        duracao_musica = 180.0  # fallback 3m
        try:
            import subprocess, json
            cmd = [
                'ffprobe', '-v', 'error',
                '-print_format', 'json',
                '-show_entries', 'format=duration',
                music_path
            ]
            p = subprocess.run(cmd, capture_output=True, text=True)
            j = json.loads(p.stdout)
            duracao_musica = float(j['format']['duration'])
        except Exception:
            pass

        # Este laço mantém a música tocando até terminar, mas NÃO “toca” no servidor.
        # É apenas uma simulação de tempo: o cliente “escuta” esse arquivo.
        segundos_restantes = (fim_programa - datetime.now()).total_seconds()
        seg_a_tocar = min(duracao_musica, segundos_restantes)
        time.sleep(seg_a_tocar + 0.1)

        # Ao fim desse sono, o while repete e possivelmente troca de faixa
    # Quando acabar a duração total do bloco, o rodar_radio se encarrega de avançar
    return


def rodar_radio():
    """
    Laço principal: percorre o 'cronograma', chama rodar_programa, e avança índices.
    """
    global cronograma_index
    while True:
        prog = cronograma[cronograma_index]
        estilo = prog["estilo"]
        dur    = prog["duracao"]
        print(f"[RÁDIO] Iniciando bloco '{estilo}' por {dur} minutos →", datetime.now())
        rodar_programa(estilo, dur)
        # sinaliza ao controlador externo que mudou de bloco
        with status_lock:
            cronograma_index = (cronograma_index + 1) % len(cronograma)
        # volta ao início e chama o próximo bloco imediatamente


# Inicializa a thread que toca a rádio “para sempre”
t = Thread(target=rodar_radio, daemon=True)
t.start()


# ——————————————————————————————————————————————
# ROTAS DO FLASK
# ——————————————————————————————————————————————

@app.route('/')
def index():
    # serve o HTML principal
    return render_template('index.html')


@app.route('/status')
def rota_status():
    """
    Retorna um JSON com:
      tipo: 'vinheta' ou 'musica'
      url: caminho relativo dentro de /static (ex.: 'musicas/Artist - Song.mp3' ou 'vinhetas/...')
      nome, artista e capa (se música)
      estilo (sempre válido)
      start_time (ISO8601, UTC)
    """
    with status_lock:
        sd = status_data.copy()
    if sd["start_time"] is not None:
        sd["start_time"] = sd["start_time"].isoformat()
    return jsonify(sd)


@app.route('/static/<path:filename>')
def rota_static(filename):
    """
    Garante que qualquer URL /static/... aponte para a pasta estática correta.
    """
    return send_from_directory('static', filename)


if __name__ == '__main__':
    # Garante que existam as pastas necessárias
    os.makedirs('static/musicas', exist_ok=True)
    os.makedirs('static/vinhetas', exist_ok=True)

    # Roda o Flask em 127.0.0.1:5000 (por padrão). 
    # Quando você abrir http://127.0.0.1:5000/, vai carregar o HTML sem interferir no fluxo da rádio.
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
