"""Fábrica da Rede YouTube — roda na nuvem (GitHub Actions agora, servidor depois).

A cada rodada:
  1. pega os vídeos "na_fila" ou "refazer" -> faz roteiro (se faltar), voz, cenas,
     legenda e thumb -> sobe NÃO LISTADO no canal (pelo proxy do canal) -> "revisar"
  2. pega os vídeos "aprovado" -> deixa PÚBLICO (+ comentário com link, se o nicho
     tiver link) -> "publicado"

Grátis: voz Kokoro (roda aqui), vídeos Pixabay, imagens Cloudflare Workers AI.
Pago só a imagem da thumb (OpenAI), com reserva grátis na Cloudflare.
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "conector"))
import proxy_canal  # noqa: E402
import roteiro as roteirista  # noqa: E402

W, H, FPS = 1920, 1080, 30
MODELOS = Path(os.environ.get("KOKORO_DIR", Path.home() / ".cache" / "kokoro"))
FONTE = Path(os.environ.get("FONTE_THUMB", AQUI / "fontes" / "Anton-Regular.ttf"))
MAX_POR_RODADA = int(os.environ.get("MAX_POR_RODADA", "2"))
VOZES = ["am_michael", "af_heart", "bf_emma", "am_adam"]
MIN_SEGUNDOS = 425  # alvo: 7 min (com folga)
# conferência de imagens/vídeos de apoio usa SÓ os modelos "lite" do Gemini (cota separada da dos roteiros)
MODELOS_CONFERENCIA = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite")
ACEITA_SEGUNDOS = 390  # a dona aceita pouca variação: a partir de 6min30 não refaz nada


class RoteiroCurto(Exception):
    def __init__(self, seg):
        super().__init__(f"narração com {seg:.0f}s (mínimo {MIN_SEGUNDOS}s)")
        self.seg = seg


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ banco
class Banco:
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.s = requests.Session()
        self.s.headers.update({"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"})

    def get(self, tabela, **params):
        r = self.s.get(f"{self.url}/rest/v1/{tabela}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def patch(self, tabela, filtro: dict, dados: dict):
        r = self.s.patch(f"{self.url}/rest/v1/{tabela}", params=filtro, json=dados, timeout=60)
        r.raise_for_status()

    # arquivos pequenos (rosto da persona) no Storage do Supabase
    def baixar_arquivo(self, balde, nome, destino: Path) -> bool:
        r = self.s.get(f"{self.url}/storage/v1/object/{balde}/{nome}", timeout=60)
        if r.ok and r.content:
            destino.write_bytes(r.content)
            return True
        return False

    def guardar_arquivo(self, balde, nome, arq: Path):
        h = {"Content-Type": "image/png", "x-upsert": "true"}
        r = self.s.post(f"{self.url}/storage/v1/object/{balde}/{nome}", data=arq.read_bytes(), headers=h, timeout=60)
        if r.status_code in (400, 404) and "not found" in r.text.lower():
            self.s.post(f"{self.url}/storage/v1/bucket", json={"id": balde, "name": balde, "public": False}, timeout=60)
            r = self.s.post(f"{self.url}/storage/v1/object/{balde}/{nome}", data=arq.read_bytes(), headers=h, timeout=60)
        if not r.ok:
            log("  não guardei", nome, r.status_code, r.text[:120])

    def job(self, job_id, **dados):
        dados["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.patch("yt_jobs", {"id": f"eq.{job_id}"}, dados)


def sh(*cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} falhou: {r.stderr[-800:]}")
    return r.stdout


def duracao(arq) -> float:
    return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arq)).strip())


# ------------------------------------------------------------------ voz (Kokoro, grátis)
_KOKORO = None


def kokoro():
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        _KOKORO = Kokoro(str(MODELOS / "kokoro-v1.0.onnx"), str(MODELOS / "voices-v1.0.bin"))
    return _KOKORO


def falar(texto: str, voz: str, saida: Path, velocidade=0.95):
    import soundfile as sf
    amostras, sr = kokoro().create(texto, voice=voz, speed=velocidade, lang="en-us")
    sf.write(str(saida), amostras, sr)


# ------------------------------------------------------------------ cenas
def pixabay_videos(busca: str, pagina=1) -> list[dict]:
    r = requests.get("https://pixabay.com/api/videos/", params={
        "key": os.environ["PIXABAY_KEY"], "q": busca[:100], "per_page": 20, "page": pagina, "safesearch": "true"}, timeout=30)
    if r.status_code != 200:
        return []
    out = []
    for h in r.json().get("hits", []):
        v = h["videos"].get("large") or h["videos"].get("medium")
        if v and v.get("url") and v.get("width", 0) >= 1280:
            miniatura = (h["videos"].get("small") or {}).get("thumbnail") or v.get("thumbnail") or ""
            out.append({"id": h["id"], "url": v["url"], "dur": h.get("duration", 0), "thumb": miniatura,
                        "tags": h.get("tags", "")})
    return out


def escolher_relevantes(fala: str, busca: str, cands: list[dict]) -> list[dict] | None:
    """A IA olha a miniatura de cada vídeo e devolve SÓ os que mostram o que está sendo dito (na ordem de melhor).
    None = não deu para conferir (sem chave/cota): aí usa a ordem da busca, mas nunca vídeo sem relação aparente."""
    chave = os.environ.get("GEMINI_API_KEY")
    cands = [c for c in cands if c.get("thumb")][:6]
    if not chave or not cands:
        return None
    partes = []
    for k, c in enumerate(cands):
        try:
            img = requests.get(c["thumb"], timeout=20).content
        except Exception:
            continue
        partes += [{"text": f"Clip {k}:"}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}]
    if not partes:
        return None
    partes.append({"text": (
        f'Narration at this moment of a health video for seniors: "{fala[:400]}"\n'
        f'Wanted B-roll: "{busca}".\n'
        "Which clips VISUALLY show something directly related to what the narration says at this moment "
        "(the same object, food, body part, symptom or situation)? Reject anything unrelated (pets, makeup, "
        "random landscapes, sports, unrelated people) and anything childish or inappropriate. "
        'Reply ONLY JSON: {"ok": [clip numbers, best first]}')})
    corpo = {"contents": [{"parts": partes}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    for modelo in MODELOS_CONFERENCIA:  # só "lite": não gasta a cota dos modelos que escrevem os roteiros
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                              params={"key": chave}, json=corpo, timeout=90)
            if r.status_code == 429:
                continue
            if not r.ok:
                break
            ok = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"]).get("ok", [])
            return [cands[i] for i in ok if isinstance(i, int) and 0 <= i < len(cands)]
        except Exception:
            break
    return None


# thumb: rosto idoso bem de perto — gênero do público de cada nicho e a dor que aparece no rosto
GENERO_NICHO = {"queda_capilar": "woman", "pele": "woman", "emagrecimento": "woman", "disfuncao": "man"}
DOR_NICHO = {"diabetes": "high blood sugar", "pulmao": "shortness of breath", "pele": "sagging, wrinkled aging skin",
             "queda_capilar": "thinning hair falling out", "disfuncao": "men's health and prostate worries",
             "emagrecimento": "stubborn belly fat", "neuropatia": "burning, tingling feet", "memoria": "memory loss and confusion",
             "pressao": "dangerously high blood pressure"}
REVISAO_CLOSE = """Strict quality check of an AI close-up portrait for a YouTube thumbnail.
FAIL if ANY of: distorted, melted or asymmetric face; weird eyes, pupils or teeth; extra or deformed fingers/hands;
more than one face; text, letters or watermark; plastic/cartoon look; the person looks younger than about 55.
The top of the head or the chin being cropped by the frame is FINE (it is a close-up).
Reply ONLY JSON: {"ok": true/false, "problems": ["short reason", ...]}"""

# o que a foto grande da thumb TEM que mostrar (a IA confere)
MOSTRA_NICHO = {"diabetes": "a blood sugar test or glucose meter", "pulmao": "human lungs or a person struggling to breathe",
                "pele": "wrinkled or sagging aging skin on a face", "queda_capilar": "hair loss: hair falling out in a comb, brush or hand",
                "disfuncao": "the prostate or male health (anatomy or an older man)", "emagrecimento": "belly fat or a tape measure on a waist",
                "neuropatia": "feet in pain or a person holding their painful foot", "memoria": "the human brain",
                "pressao": "a blood pressure monitor or measurement"}
BUSCA_NICHO = {"diabetes": "blood sugar test finger", "pulmao": "lungs anatomy", "pele": "wrinkles face aging",
               "queda_capilar": "hair loss comb", "disfuncao": "prostate anatomy", "emagrecimento": "belly fat measuring",
               "neuropatia": "foot pain", "memoria": "brain glowing", "pressao": "blood pressure measurement"}


def pixabay_foto(busca: str, destino: Path, pessoa: bool = False, evitar: set | None = None, confere: str | None = None) -> bool:
    """Foto real e grátis do Pixabay (licença livre). Reserva quando a IA de imagem está sem cota."""
    r = requests.get("https://pixabay.com/api/", params={
        "key": os.environ["PIXABAY_KEY"], "q": busca[:100], "image_type": "photo", "per_page": 30,
        "safesearch": "true", "orientation": "vertical" if pessoa else "horizontal", "min_width": 1000}, timeout=30)
    if r.status_code != 200:
        return False
    hits = [h for h in r.json().get("hits", []) if not evitar or h["id"] not in evitar]
    if not hits:
        return False
    if confere:  # a IA confere se a foto mostra MESMO o assunto (na ordem de relevância da busca)
        pergunta = (f"Does this photo clearly and directly show: {confere}? It must be a realistic photo that a viewer "
                    "instantly recognizes as that, with no text, logo or watermark. "
                    'Reply ONLY JSON: {"ok": true/false, "problems": []}')
        melhores = hits[:15]
        random.shuffle(melhores)  # varia entre os bons resultados (não repete sempre o mesmo rosto/foto)
        for h in melhores[:6]:
            if evitar is not None:
                evitar.add(h["id"])
            if baixar(h.get("largeImageURL") or h["webformatURL"], destino) and revisar_imagem(destino, pergunta) is not False:
                return True
        destino.unlink(missing_ok=True)
        return False
    h = random.choice(hits[:10])
    if evitar is not None:
        evitar.add(h["id"])
    return baixar(h.get("largeImageURL") or h["webformatURL"], destino)


def baixar(url, destino: Path):
    for espera in (0, 5, 15):
        time.sleep(espera)
        try:
            with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status_code == 429:
                    continue
                r.raise_for_status()
                with open(destino, "wb") as f:
                    for parte in r.iter_content(1 << 20):
                        f.write(parte)
                return True
        except requests.RequestException:
            continue
    destino.unlink(missing_ok=True)
    return False


def imagem_openai(prompt: str, destino: Path, largura=1344, altura=768) -> bool:
    """Imagem do ChatGPT (paga, só para thumb e rosto da persona)."""
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    tam = "1024x1024" if largura == altura else ("1536x1024" if largura > altura else "1024x1536")
    try:
        r = requests.post("https://api.openai.com/v1/images/generations", timeout=240,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                          json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
                                "size": tam, "quality": "medium", "n": 1})
        if r.ok:
            destino.write_bytes(base64.b64decode(r.json()["data"][0]["b64_json"]))
            return True
        log("  ChatGPT imagem indisponível:", r.status_code, r.text[:120])
    except Exception as e:
        log("  ChatGPT imagem falhou:", str(e)[:120])
    return False


def imagem_cloudflare(prompt: str, destino: Path, largura=1344, altura=768):
    conta, token = os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]
    semente = random.randint(1, 2**31 - 1)  # semente nova a cada tentativa: se a 1ª sair defeituosa, a 2ª é outra imagem
    modelos = [("@cf/black-forest-labs/flux-1-schnell", {"prompt": prompt, "steps": 4}),
               ("@cf/leonardo/lucid-origin", {"prompt": prompt, "width": largura, "height": altura, "steps": 20, "seed": semente})]
    if os.environ.get("IMAGEM_CAPRICHADA"):
        modelos.reverse()
    for modelo, corpo in modelos:
        try:
            r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/{modelo}",
                              headers={"Authorization": f"Bearer {token}"}, json=corpo, timeout=120)
            img = r.json().get("result", {}).get("image") if r.ok else None
            if not img:
                log("  imagem recusada:", modelo, r.status_code, r.text[:160])
            if img:
                destino.write_bytes(base64.b64decode(img))
                return True
        except Exception as e:  # tenta o próximo modelo
            log("  imagem falhou em", modelo, str(e)[:80])
    # reserva grátis sem chave: Pollinations (Flux)
    try:
        from urllib.parse import quote
        r = requests.get(f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}",
                         params={"width": largura, "height": altura, "nologo": "true", "model": "flux",
                                 "seed": random.randint(1, 10**6)}, timeout=180)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            destino.write_bytes(r.content)
            return True
        log("  pollinations recusou:", r.status_code)
    except Exception as e:
        log("  pollinations falhou:", str(e)[:80])
    return False


ESTILO = ("photorealistic, cinematic lighting, vivid natural colors, shallow depth of field, no text, no watermark, "
          "anatomically correct, close framing, no hands in frame")


# ------------------------------------------------------------------ revisão das imagens (Gemini, grátis)
REVISAO_CENA = """You are a strict quality inspector for AI-generated photos used in a YouTube video.
FAIL the image if you see ANY of: deformed, extra, fused or missing fingers; malformed hands or arms; extra limbs;
a person's head or face cut off by the frame edge; body parts in impossible positions; distorted or melted face,
asymmetric eyes, weird teeth; gibberish text, letters or watermarks; obvious AI glitches or blur blobs.
Reply ONLY JSON: {"ok": true/false, "problems": ["short reason", ...]}"""

REVISAO_ROSTO = """You are a strict quality inspector for a YouTube thumbnail face (head-and-shoulders portrait).
FAIL if ANY of: the top of the head/hair is cut by the frame; eyes not natural or not looking roughly at camera;
distorted face, teeth or ears; hands, fingers or arms visible; more than one person; text or watermark;
plastic/cartoon look; background not plain. Shoulders being cut at the bottom is fine.
Reply ONLY JSON: {"ok": true/false, "problems": ["short reason", ...]}"""


def revisar_imagem(arq: Path, instrucao: str) -> bool | None:
    """True = aprovada, False = defeito, None = não deu para revisar (aí a imagem NÃO é usada)."""
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        return None
    mime = "image/png" if arq.read_bytes()[:4] == b"\x89PNG" else "image/jpeg"
    corpo = {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(arq.read_bytes()).decode()}},
                                     {"text": instrucao}]}],
             "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    for modelo in MODELOS_CONFERENCIA:  # só "lite": não gasta a cota dos modelos que escrevem os roteiros
        for espera in (0, 20):
            time.sleep(espera)
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                                  params={"key": chave}, json=corpo, timeout=90)
                if r.status_code == 429:
                    continue
                if not r.ok:
                    break
                res = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                if not res.get("ok"):
                    log("  imagem reprovada na revisão:", "; ".join(res.get("problems", []))[:160])
                return bool(res.get("ok"))
            except Exception as e:
                log("  revisão falhou:", str(e)[:100])
                break
    return None


def gerar_imagem_revisada(prompt: str, destino: Path, instrucao=REVISAO_CENA, tentativas=2, premium=False, **kw) -> bool:
    """Gera, revisa e só devolve True se a imagem passou na revisão (sem braço torto, corpo cortado etc.).
    premium=True (thumb e rosto): tenta o ChatGPT primeiro, depois a reserva grátis."""
    for _ in range(tentativas):
        if not ((premium and imagem_openai(prompt, destino, **kw)) or imagem_cloudflare(prompt, destino, **kw)):
            return False
        if revisar_imagem(destino, instrucao):
            return True
    destino.unlink(missing_ok=True)
    return False


def transicao(tipo: str) -> str:
    """Entrada do clipe: flash branco (gancho), punch-in (zoom seco), fade rápido do preto ou corte seco."""
    if tipo == "flash":
        return ",fade=t=in:st=0:d=0.14:color=white"
    if tipo == "punch":
        return f",crop=iw/1.08:ih/1.08,scale={W}:{H}"
    if tipo == "preto":
        return ",fade=t=in:st=0:d=0.2"
    return ""


def visual_do_canal(channel_id: str) -> dict:
    """Cada canal tem uma 'cara' própria: o mesmo clipe de banco sai com outra cor/enquadramento em outro canal."""
    import hashlib
    h = int(hashlib.md5((channel_id or "x").encode()).hexdigest(), 16)
    return {"sat": 1.0 + (h % 25) / 100, "gamma": 0.94 + (h // 25 % 12) / 100, "matiz": (h // 300 % 13) - 6,
            "espelho": 0.25 + (h // 4000 % 4) / 10}


def clip_de_video(src: Path, dur: float, saida: Path, inicio=0.0, efeito="", visual: dict | None = None):
    # enquadramento próprio de cada cena (zoom 0-12% e posição sorteada) + espelho às vezes + cor do canal
    z = random.uniform(1.0, 1.12)
    px, py = random.random(), random.random()
    cw, ch = int(W / z) // 2 * 2, int(H / z) // 2 * 2
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"crop={cw}:{ch}:{int((W - cw) * px)}:{int((H - ch) * py)},scale={W}:{H},fps={FPS},setsar=1")
    if visual:
        if random.random() < visual["espelho"]:
            vf += ",hflip"
        vf += f",hue=h={visual['matiz']},eq=saturation={visual['sat']:.2f}:gamma={visual['gamma']:.2f}"
    vf += transicao(efeito)
    sh("ffmpeg", "-y", "-ss", f"{inicio:.2f}", "-stream_loop", "-1", "-i", str(src), "-t", f"{dur:.3f}", "-vf", vf,
       "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", str(saida))


def clip_de_imagem(src: Path, dur: float, saida: Path, efeito=""):
    frames = max(1, int(dur * FPS))
    zoom = random.choice([
        f"zoompan=z='min(zoom+0.0007,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}",
        f"zoompan=z='1.15-0.0007*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}",
        f"zoompan=z=1.12:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}",
    ])
    vf = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},{zoom},setsar=1" + transicao(efeito if efeito != "punch" else "")
    sh("ffmpeg", "-y", "-loop", "1", "-i", str(src), "-t", f"{dur:.3f}", "-vf", vf,
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", str(saida))


# ------------------------------------------------------------------ legenda (ASS)
def _ts(s: float) -> str:
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


AMARELO_ASS = r"{\c&H0000E1FF&\fscx118\fscy118}"  # BGR: amarelo vivo


def legenda_ass(blocos: list[tuple[float, float, str]], saida: Path, destaques: list[tuple[float, str]] = ()):
    """Legenda dinâmica: 3 palavras por vez, a palavra falada acende em amarelo e cresce.
    `destaques`: palavras de efeito que SALTAM grandes no meio da tela (com animação de pop)."""
    linhas = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
              "[V4+ Styles]",
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
              "Style: L,Anton,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,7,4,2,120,120,110,1",
              "Style: P,Anton,170,&H0000E1FF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,10,6,8,80,80,140,1",
              "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for ini, fim, txt in blocos:
        palavras = [p.upper().replace("{", "").replace("}", "") for p in txt.split()]
        if not palavras:
            continue
        # tempo de cada palavra proporcional ao tamanho (sílabas ~ letras)
        pesos = [len(p) + 2 for p in palavras]
        total, t, tempos = sum(pesos), ini, []
        for p in pesos:
            d = (fim - ini) * p / total
            tempos.append((t, t + d))
            t += d
        for g in range(0, len(palavras), 3):
            grupo = palavras[g:g + 3]
            for k, pal in enumerate(grupo):
                a, b = tempos[g + k]
                texto = " ".join((AMARELO_ASS + w + r"{\r}") if j == k else w for j, w in enumerate(grupo))
                linhas.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},L,,0,0,0,,{texto}")
    for t0, palavra in destaques:
        pop = r"{\fad(60,180)\fscx30\fscy30\t(0,160,\fscx112\fscy112)\t(160,260,\fscx100\fscy100)}"
        linhas.append(f"Dialogue: 1,{_ts(t0)},{_ts(t0 + 2.2)},P,,0,0,0,,{pop}{palavra.upper()}")
    saida.write_text("\n".join(linhas), encoding="utf-8")


def whoosh(saida: Path):
    """Efeito 'whoosh' sintetizado (sem arquivo de terceiros)."""
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.5:c=pink:a=0.6:r=44100",
       "-af", "highpass=f=400,lowpass=f=5000,afade=t=in:d=0.22,afade=t=out:st=0.22:d=0.28,volume=0.5", str(saida))


# ------------------------------------------------------------------ thumb
# Persona fixa de cada canal (o mesmo rosto em TODAS as thumbs do canal = marca reconhecível).
# Não usamos jaleco/estetoscópio: o canal não diz ser médico (política do YouTube para saúde).
PERSONAS = [
    {"rosto": "a warm 62-year-old American woman with short silver hair and small gold earrings, wearing a teal blouse", "voz": "af_heart"},
    {"rosto": "a friendly 65-year-old American man with grey hair, trimmed grey beard and glasses, wearing a navy sweater", "voz": "am_michael"},
    {"rosto": "a 58-year-old British woman with shoulder-length blonde-grey hair, wearing a burgundy cardigan", "voz": "bf_emma"},
    {"rosto": "a 60-year-old Black American man with short grey hair and a kind face, wearing a light blue shirt", "voz": "am_adam"},
    {"rosto": "a 64-year-old Latina woman with dark hair with grey streaks pulled back, wearing a coral blouse", "voz": "af_heart"},
    {"rosto": "a 67-year-old American man, bald with white stubble, wearing a dark green polo shirt", "voz": "am_michael"},
]
# Cor viva de fundo por nicho (degradê) — destaca no feed
CORES_NICHO = {"diabetes": ((255, 61, 0), (120, 0, 30)), "pulmao": ((0, 170, 255), (0, 30, 110)),
               "pele": ((255, 70, 160), (90, 0, 90)), "queda_capilar": ((0, 220, 160), (0, 70, 60)),
               "disfuncao": ((255, 40, 40), (20, 20, 90))}


def persona_do_canal(channel_id: str) -> dict:
    import hashlib
    return PERSONAS[int(hashlib.md5((channel_id or "x").encode()).hexdigest(), 16) % len(PERSONAS)]


def rosto_da_persona(banco: "Banco", channel_id: str, pasta: Path, so_ia: bool = False) -> Path | None:
    """Recorte (fundo transparente) do rosto da persona do canal. Gera UMA vez, revisa, guarda e reaproveita.
    so_ia=True: só serve a persona DEFINITIVA (IA); se a IA estiver sem cota, devolve None."""
    destino = pasta / "persona.png"
    nome = f"{channel_id}.png"            # persona definitiva (IA)
    provisoria = f"provisoria_{channel_id}.png"  # foto de banco enquanto a IA está sem cota
    if banco.baixar_arquivo("personas", nome, destino):
        return destino
    p = persona_do_canal(channel_id)
    prompt = (f"head and shoulders portrait photo of {p['rosto']}, shocked surprised expression, eyebrows raised, "
              "mouth slightly open, looking straight at the camera, entire head visible with space above the hair, "
              "plain light grey studio background, soft studio lighting, sharp focus, photorealistic, 85mm, no hands")
    bruto = pasta / "persona_bruto.png"
    for _ in range(5 if so_ia else 2):
        if gerar_imagem_revisada(prompt, bruto, REVISAO_ROSTO, tentativas=1, premium=True, largura=1024, altura=1024):
            from rembg import new_session, remove
            from PIL import Image
            recorte = remove(Image.open(bruto).convert("RGB"), session=new_session("isnet-general-use"))
            recorte = so_cabeca_e_ombros(recorte.crop(recorte.getbbox()))
            recorte.save(destino)
            banco.guardar_arquivo("personas", nome, destino)
            log("  persona nova do canal criada e guardada")
            return destino
    if so_ia:
        return None
    # reserva: retrato real do Pixabay, PROVISÓRIO (quando a IA voltar, a definitiva substitui)
    if banco.baixar_arquivo("personas", provisoria, destino):
        return destino
    homem = p["voz"].startswith(("am_", "bm_"))
    buscas = (["shocked senior man", "surprised old man face", "worried elderly man"] if homem
              else ["shocked senior woman", "surprised old woman face", "worried elderly woman"])
    vistos: set = set()
    REVISAO_FOTO = ("Strict check for a YouTube health thumbnail. ok=true ONLY if ALL are true: exactly ONE person aged 55+; "
                    "the whole head including the top of the hair is inside the frame; looking roughly at the camera; "
                    "the expression is clearly SHOCKED, SURPRISED or WORRIED (not smiling); no hat, no sunglasses; "
                    "NO hands or fingers anywhere near the face or head; plain everyday modern clothing (no costume); "
                    "no text, logo or watermark; realistic photo (not illustration). "
                    'Reply ONLY JSON: {"ok": true/false, "problems": []}')
    for k in range(9):
        if pixabay_foto(buscas[k % 3], bruto, pessoa=True, evitar=vistos) and revisar_imagem(bruto, REVISAO_FOTO):
            from rembg import new_session, remove
            from PIL import Image
            recorte = remove(Image.open(bruto).convert("RGB"), session=new_session("isnet-general-use"))
            recorte = so_cabeca_e_ombros(recorte.crop(recorte.getbbox()))
            recorte.save(destino)
            banco.guardar_arquivo("personas", provisoria, destino)
            log("  persona provisória do canal criada com foto real (Pixabay)")
            return destino
    log("  não consegui um rosto aprovado para a persona")
    return None


def so_cabeca_e_ombros(img):
    """Corta o recorte para cabeça + ombros (tira braços, mãos e corpo que sobram)."""
    alfa = img.split()[3]
    w, h = img.size
    topo = alfa.crop((0, 0, w, max(1, int(h * 0.12)))).getbbox() or (0, 0, w, h)
    cabeca = max(40, topo[2] - topo[0])
    cx = (topo[0] + topo[2]) // 2
    lado = min(w, int(cabeca * 2.0))
    x0 = max(0, min(w - lado, cx - lado // 2))
    out = img.crop((x0, 0, x0 + lado, min(h, int(cabeca * 2.1))))
    return out.crop(out.getbbox() or (0, 0, *out.size))


def _degrade(w, h, c1, c2):
    from PIL import Image
    g = Image.new("RGB", (w, h))
    px = g.load()
    for x in range(w):
        for y in range(0, h):
            t = min(1, (x / w) * 0.6 + (y / h) * 0.4)
            px[x, y] = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
    return g


def thumb(texto: str, destaque: str, prompt_img: str, saida: Path, pasta: Path,
          rosto: Path | None = None, nicho: str = "", busca: str = "", so_ia: bool = False) -> bool:
    """Padrão dos virais de saúde: fundo de cor VIVA, palavras ENORMES à esquerda (uma em tarja vermelha),
    o problema em foto à direita e o ROSTO da persona do canal na frente, com contorno branco."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    c1, c2 = CORES_NICHO.get(nicho, ((255, 120, 0), (90, 0, 40)))
    base = _degrade(1280, 720, c1, c2)

    # 1) foto do problema: fundo da metade direita + bolinha em destaque (anel amarelo)
    img_path = pasta / "thumb_img.png"
    foto = None
    # padrão que mais puxa clique (pedido da dona): ROSTO de uma pessoa mais velha BEM DE PERTO, com a emoção da dor do vídeo
    genero = GENERO_NICHO.get(nicho) or random.choice(["woman", "man"])
    emocao = random.choice(["worried", "shocked", "distressed", "anxious"])
    prompt_rosto = (f"extreme close-up photo of the face of a {random.choice([62, 65, 68, 70, 72])}-year-old American {genero} "
                    f"with a {emocao} expression, troubled by {DOR_NICHO.get(nicho, 'a health problem')}, looking at the camera, "
                    "face filling the frame, natural skin texture, dramatic soft side light, vivid colors, photorealistic, "
                    "sharp focus, no text, no hands")
    ok = gerar_imagem_revisada(prompt_rosto, img_path, REVISAO_CLOSE, tentativas=4 if so_ia else 2, premium=True,
                               largura=1024, altura=1024)
    if so_ia and not ok:
        return False  # refazendo com IA e a IA está sem cota: não troca nada
    ia_usada = ok
    if not ok:  # IA sem cota: foto real de rosto idoso bem de perto (conferida); em último caso, a imagem do problema
        quem = "senior woman" if genero == "woman" else "senior man"
        ok = pixabay_foto(f"{quem} worried face", img_path, pessoa=True, confere=(
                 "a close-up of ONE older person's face (55+), worried, sad, shocked or in pain, the face filling most of the frame")) \
            or pixabay_foto(f"elderly {genero} sad face", img_path, pessoa=True, confere=(
                 "a close-up of ONE older person's face (55+) with a sad or worried expression")) \
            or (bool(busca) and pixabay_foto(busca, img_path, confere=busca)) \
            or pixabay_foto(BUSCA_NICHO.get(nicho, "health senior"), img_path, confere=MOSTRA_NICHO.get(nicho))
    if ok:
        foto = Image.open(img_path).convert("RGB")
        foto = ImageEnhance.Color(foto).enhance(1.45)
        foto = ImageEnhance.Contrast(foto).enhance(1.15)
        esc = max(720 / foto.width, 720 / foto.height)  # cobre a metade inteira (foto vertical ou horizontal)
        fundo = foto.resize((max(720, int(foto.width * esc) + 1), max(720, int(foto.height * esc) + 1)))
        x = (fundo.width - 720) // 2
        y = int((fundo.height - 720) * 0.25)  # rosto costuma estar na parte de cima: corta mais embaixo
        fundo = fundo.crop((x, y, x + 720, y + 720))
        if rosto:
            fundo = fundo.filter(ImageFilter.GaussianBlur(3))
        mascara = Image.new("L", (720, 720), 255)
        dm = ImageDraw.Draw(mascara)
        for i in range(260):  # funde com o fundo colorido à esquerda
            dm.line([(i, 0), (i, 720)], fill=int(255 * i / 260))
        base.paste(fundo, (560, 0), mascara)

    # 2) ROSTO da persona ocupando a METADE DIREITA, contorno branco + sombra
    if rosto and rosto.exists():
        r = Image.open(rosto).convert("RGBA")
        alt = 700
        r = r.resize((int(r.width * alt / r.height), alt))
        if r.width > 660:
            r = r.resize((660, int(r.height * 660 / r.width)))
        alfa = r.split()[3]
        contorno = alfa.filter(ImageFilter.MaxFilter(19))
        sombra = contorno.filter(ImageFilter.GaussianBlur(16))
        x0 = 640 + max(0, (640 - r.width) // 2)
        y0 = 720 - r.height + 6
        base.paste((0, 0, 0), (x0 + 12, y0 + 12), sombra.point(lambda v: int(v * 0.6)))
        base.paste((255, 255, 255), (x0, y0), contorno)
        base.paste(r, (x0, y0), r)
        # bolinha com o problema, em cima do ombro esquerdo da persona
        if foto is not None:
            dia = 250
            q = foto.resize((int(foto.width * dia / min(foto.size)), int(foto.height * dia / min(foto.size))))
            q = q.crop(((q.width - dia) // 2, (q.height - dia) // 2, (q.width - dia) // 2 + dia, (q.height - dia) // 2 + dia))
            m = Image.new("L", (dia, dia), 0)
            ImageDraw.Draw(m).ellipse([0, 0, dia - 1, dia - 1], fill=255)
            cx, cy = 600, 455
            ImageDraw.Draw(base).ellipse([cx - 12, cy - 12, cx + dia + 12, cy + dia + 12], fill=(255, 230, 0))
            base.paste(q, (cx, cy), m)

    # 2b) desligado: o padrão fixo é só palavras + UMA imagem (sem bolinha)
    if False:
        ing = pasta / "thumb_ing.jpg"
        if busca and pixabay_foto(busca, ing, confere=busca):
            q = Image.open(ing).convert("RGB")
            dia = 300
            q = q.resize((int(q.width * dia / min(q.size)) + 1, int(q.height * dia / min(q.size)) + 1))
            q = q.crop(((q.width - dia) // 2, (q.height - dia) // 2, (q.width - dia) // 2 + dia, (q.height - dia) // 2 + dia))
            m = Image.new("L", (dia, dia), 0)
            ImageDraw.Draw(m).ellipse([0, 0, dia - 1, dia - 1], fill=255)
            cx, cy = 940, 390
            ImageDraw.Draw(base).ellipse([cx - 14, cy - 14, cx + dia + 14, cy + dia + 14], fill=(255, 230, 0))
            base.paste(q, (cx, cy), m)

    # 3) palavras ENORMES à esquerda
    d = ImageDraw.Draw(base)
    linhas = [l.strip() for l in texto.upper().split("|") if l.strip()][:5]
    # mede o texto DE VERDADE (caixa da letra + contorno + tarja) e diminui até caber com folga — nunca corta letra
    X0, LARG, TOPO, BASE_Y = 36, 560 if not rosto else 520, 24, 696
    TRACO, PAD_TARJA = 7, 14

    def medir(tamanhos):
        caixas = []
        for l, tam in zip(linhas, tamanhos):
            f = ImageFont.truetype(str(FONTE), tam)
            x0, y0, x1, y1 = d.textbbox((0, 0), l, font=f, stroke_width=TRACO)
            caixas.append((f, x0, y0, x1, y1))
        return caixas

    tamanhos = [150 if len(linhas) <= 3 else 118] * len(linhas)
    for _ in range(120):
        caixas = medir(tamanhos)
        larg_ok = all((x1 - x0) + 2 * PAD_TARJA <= LARG for _, x0, _, x1, _ in caixas)
        alt_total = sum((y1 - y0) + 2 * PAD_TARJA for *_, y0, _, y1 in [(c[0], c[1], c[2], c[3], c[4]) for c in caixas]) \
            + 14 * (len(linhas) - 1)
        if larg_ok and alt_total <= BASE_Y - TOPO:
            break
        tamanhos = [max(40, t - 4) if ((c[3] - c[1]) + 2 * PAD_TARJA > LARG or alt_total > BASE_Y - TOPO) else t
                    for t, c in zip(tamanhos, caixas)]
    caixas = medir(tamanhos)
    alt_total = sum((c[4] - c[2]) + 2 * PAD_TARJA for c in caixas) + 14 * (len(linhas) - 1)
    y = TOPO + max(0, ((BASE_Y - TOPO) - alt_total) // 2)
    cores = [(255, 230, 0), (255, 255, 255)]
    for k, (l, (f, bx0, by0, bx1, by1)) in enumerate(zip(linhas, caixas)):
        topo_linha = y + PAD_TARJA
        ox, oy = X0 + PAD_TARJA - bx0, topo_linha - by0  # posiciona pela caixa real da letra
        if destaque and destaque.upper() in l:
            d.rectangle([X0, y, X0 + (bx1 - bx0) + 2 * PAD_TARJA, y + (by1 - by0) + 2 * PAD_TARJA],
                        fill=(230, 20, 20), outline=(255, 255, 255), width=5)
            d.text((ox, oy), l, font=f, fill=(255, 255, 255))
        else:
            d.text((ox + 6, oy + 6), l, font=f, fill=(0, 0, 0))  # sombra
            d.text((ox, oy), l, font=f, fill=cores[k % 2], stroke_width=TRACO, stroke_fill=(0, 0, 0))
        y += (by1 - by0) + 2 * PAD_TARJA + 14
    base.save(saida, "JPEG", quality=93)
    return ia_usada


# ------------------------------------------------------------------ link + QR
class LinkErrado(Exception):
    pass


_PAGINAS_OK: dict[str, bool] = {}


def conferir_link(banco: "Banco", job: dict) -> str:
    """Trava antes de publicar: nicho do vídeo == nicho ATUAL do canal, link == produto desse nicho,
    e a página abre e é MESMO esse produto (código do link + afiliada cg_pub da dona). Senão, não publica."""
    c = banco.get("yt_channels", channel_id=f"eq.{job['channel_id']}", select="niche_id")
    if not c or not c[0].get("niche_id"):
        raise LinkErrado("canal sem nicho")
    n = banco.get("yt_niches", id=f"eq.{c[0]['niche_id']}", select="key,affiliate_link,link_code")
    if not n or n[0]["key"] != job["niche_key"]:
        raise LinkErrado(f"o vídeo é de '{job['niche_key']}' mas o canal agora é de '{n[0]['key'] if n else '?'}'")
    base, code = (n[0].get("affiliate_link") or "").strip(), n[0].get("link_code")
    if not base or not code:
        raise LinkErrado("nicho sem link ou sem código do link")
    if base not in _PAGINAS_OK:
        try:
            h = requests.get(base, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
            _PAGINAS_OK[base] = f"|{code}" in h and "cg_pub" in h
        except Exception:
            _PAGINAS_OK[base] = False
    if not _PAGINAS_OK[base]:
        raise LinkErrado(f"a página {base} não abriu ou não é o produto certo (código {code})")
    link = link_do_job(banco, job)
    if not link or not link.startswith(base) or f"pg={job.get('pg') or job['id'].replace('-', '')[:12]}" not in link:
        raise LinkErrado("link montado sem o rastreio do vídeo")
    return link


def link_do_job(banco: "Banco", job: dict) -> str | None:
    """Link do produto do nicho com o rastreio do vídeo: ?pg=<código do job>&utm_source=youtube."""
    n = banco.get("yt_niches", key=f"eq.{job['niche_key']}", user_id=f"eq.{job['user_id']}", select="affiliate_link")
    link = (n[0].get("affiliate_link") or "").strip() if n else ""
    if not link:
        return None
    pg = job.get("pg") or job["id"].replace("-", "")[:12]
    sep = "&" if "?" in link else "?"
    # pg = o vídeo (s3 na 001) · utm_source = origem (s4) · utm_campaign = o CANAL (s5)
    return f"{link}{sep}pg={pg}&utm_source=youtube&utm_campaign={canal_slug(banco, job['channel_id'])}"


_SLUGS: dict[str, str] = {}


def canal_slug(banco: "Banco", channel_id: str) -> str:
    """Código do canal nos links (a-z, 0-9 e hífen), CONGELADO na 1ª vez (app_secrets slug:<canal>):
    se a dona trocar o nome do canal, o rastreio continua o mesmo e nenhum clique se perde."""
    if channel_id not in _SLUGS:
        salvo = banco.get("app_secrets", key=f"eq.slug:{channel_id}", select="value")
        if salvo:
            _SLUGS[channel_id] = salvo[0]["value"]
        else:
            import unicodedata
            c = banco.get("yt_channels", channel_id=f"eq.{channel_id}", select="name")
            nome = (c[0]["name"] if c else channel_id) or channel_id
            nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
            _SLUGS[channel_id] = (re.sub(r"[^a-z0-9]+", "-", nome).strip("-") or "canal")[:40]
            banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"}, timeout=60,
                         headers={"Prefer": "resolution=ignore-duplicates"},
                         json={"key": f"slug:{channel_id}", "value": _SLUGS[channel_id]})
    return _SLUGS[channel_id]


def cartao_qr(link: str, saida: Path):
    """Cartão branco com QR code + 'SCAN FOR THE RECIPE' (aparece no canto durante as chamadas)."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
    qr = qrcode.QRCode(border=1, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(link)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((300, 300))
    card = Image.new("RGB", (340, 420), (255, 255, 255))
    card.paste(img, (20, 20))
    d = ImageDraw.Draw(card)
    d.rectangle([0, 330, 340, 420], fill=(230, 20, 20))
    f = ImageFont.truetype(str(FONTE), 40)
    for k, linha in enumerate(["SCAN FOR", "THE RECIPE"]):
        w = d.textlength(linha, font=f)
        d.text(((340 - w) / 2, 330 + 2 + k * 42), linha, font=f, fill=(255, 255, 255))
    card.save(saida)


# ------------------------------------------------------------------ montagem
def montar(banco: "Banco", job: dict, roteiro: dict, pasta: Path, velocidade: float = 0.95) -> tuple[Path, Path, int]:
    persona = persona_do_canal(job.get("channel_id", ""))
    voz = persona["voz"]  # a voz combina com o rosto da persona e é sempre a mesma no canal
    blocos = roteiro["segments"]
    # memória do canal: clipes de banco já usados nos vídeos anteriores DESTE canal não se repetem
    memoria = pasta / "broll_usados.json"
    usados: set[int] = set()
    if banco.baixar_arquivo("broll", f"{job.get('channel_id', 'x')}.json", memoria):
        try:
            usados = set(json.loads(memoria.read_text()))
        except Exception:
            usados = set()
    ja_no_canal = len(usados)
    visual = visual_do_canal(job.get("channel_id", ""))
    clipes, falas, legendas, destaques, ctas, cortes, t = [], [], [], [], [], [], 0.0
    bons_do_bloco: dict[int, list] = {}  # vídeos de apoio aprovados pela IA, por trecho
    # voz primeiro: confere os 7 minutos ANTES de gastar tempo com cenas
    def narrar(vel):
        tot = 0.0
        for i, b in enumerate(blocos):
            wav = pasta / f"fala_{i:03d}.wav"
            falar(b["text"], voz, wav, vel)
            tot += duracao(wav) + 0.25
        return tot

    total = narrar(velocidade)
    if total < ACEITA_SEGUNDOS and velocidade > 0.88:
        # ficou curto: primeiro só deixa a voz um pouco mais lenta (sem reescrever nada)
        log(f"  narração com {total:.0f}s — refazendo só a voz, mais devagar")
        total = narrar(0.86)
    if total < ACEITA_SEGUNDOS:
        raise RoteiroCurto(total)  # muito curto mesmo: aí sim o roteiro é aumentado
    log(f"  narração: {total / 60:.1f} min")
    for i, b in enumerate(blocos):
        wav = pasta / f"fala_{i:03d}.wav"
        dur = duracao(wav) + 0.25
        falas.append((wav, dur))
        legendas.append((t, t + dur - 0.25, b["text"]))
        if b.get("on_screen"):
            destaques.append((t + 0.15, b["on_screen"]))
        if str(b.get("cta", "")).lower() in ("true", "1", "yes"):
            ctas.append((t, t + max(dur, 9.0)))
        t += dur
        # gancho (primeiros blocos): cortes rápidos de ~2,5s; depois cenas de ~4,5s
        n = max(1, round(dur / (2.5 if i < 4 else 4.5)))
        cena_dur = dur / n
        for j in range(n):
            saida = pasta / f"cena_{i:03d}_{j}.mp4"
            vis = b.get("visual", {})
            # gancho: flash + whoosh a cada corte; depois varia punch-in / fade rápido / corte seco
            efeito = "flash" if i < 4 else random.choice(["punch", "punch", "preto", "", ""])
            if i < 4 and (i or j):
                cortes.append(t - dur + j * cena_dur)
            usar_img = vis.get("type") == "image" and j == 0 or (j % 2 == 1 and vis.get("prompt"))
            feito = False
            # imagens de IA nas cenas só se ligado (decisão da dona: por enquanto IA só na thumb)
            if usar_img and vis.get("prompt") and os.environ.get("IMAGENS_NAS_CENAS") == "1":
                png = pasta / f"img_{i:03d}_{j}.png"
                # só entra se passar na revisão (sem mão/braço deformado, corpo cortado, texto estranho)
                if gerar_imagem_revisada(vis["prompt"] + ", " + ESTILO, png):
                    clip_de_imagem(png, cena_dur, saida, efeito)
                    feito = True
            if not feito:
                if i not in bons_do_bloco:
                    # uma vez por trecho: busca e a IA confere quais vídeos mostram o que está sendo dito
                    buscas = [q for q in (vis.get("queries") or []) + [vis.get("query")] if q]
                    bons = []
                    for q in buscas:  # a mais específica primeiro
                        cand = []
                        for pagina in (1, 2, 3):  # se o canal já usou os primeiros, busca mais fundo
                            cand += [c for c in pixabay_videos(q, pagina) if c["id"] not in usados]
                            if len(cand) >= 4:
                                break
                        if not cand:
                            continue
                        escolhidos = escolher_relevantes(b["text"], q, cand)
                        if escolhidos is None:  # sem conferência possível: só os 2 primeiros da busca específica
                            escolhidos = cand[:2]
                        bons += [c for c in escolhidos if c["id"] not in {x["id"] for x in bons}]
                        if len(bons) >= n:
                            break
                    bons_do_bloco[i] = bons
                    if not bons:
                        log(f"  trecho {i + 1}: nenhum vídeo de apoio combinou — repito uma cena do próprio assunto")
                cand = [c for c in bons_do_bloco[i] if c["id"] not in usados]
                for c in cand[:3]:
                    usados.add(c["id"])
                    mp4 = pasta / f"px_{c['id']}.mp4"
                    if mp4.exists() or baixar(c["url"], mp4):
                        clip_de_video(mp4, cena_dur, saida, inicio=random.uniform(0, max(0, c["dur"] - cena_dur - 0.5)), efeito=efeito, visual=visual)
                        feito = True
                        break
            if not feito:  # último recurso: repete uma cena anterior ou um fundo escuro
                if clipes:
                    clip_de_video(random.choice(clipes), cena_dur, saida)
                else:
                    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x14161a:s={W}x{H}:r={FPS}", "-t", f"{cena_dur:.3f}",
                       "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(saida))
            clipes.append(saida)
        log(f"  bloco {i + 1}/{len(blocos)} pronto ({dur:.1f}s)")

    # guarda os clipes usados (até os 3000 mais recentes) para o próximo vídeo do canal não repetir
    try:
        memoria.write_text(json.dumps(list(usados)[-3000:]))
        h = {"Content-Type": "application/json", "x-upsert": "true"}
        r = banco.s.post(f"{banco.url}/storage/v1/object/broll/{job.get('channel_id', 'x')}.json", data=memoria.read_bytes(), headers=h, timeout=60)
        if r.status_code in (400, 404) and "not found" in r.text.lower():
            banco.s.post(f"{banco.url}/storage/v1/bucket", json={"id": "broll", "name": "broll", "public": False}, timeout=60)
            banco.s.post(f"{banco.url}/storage/v1/object/broll/{job.get('channel_id', 'x')}.json", data=memoria.read_bytes(), headers=h, timeout=60)
        log(f"  vídeos de apoio: {len(usados) - ja_no_canal} novos (canal já tinha usado {ja_no_canal})")
    except Exception as e:
        log("  não guardei a memória de clipes:", str(e)[:100])

    # áudio único: junta as falas com 0.25s de pausa entre os blocos (já contada no tempo das cenas)
    partes = []
    for wav, dur in falas:
        partes += ["-i", str(wav)]
    filtro = "".join(f"[{k}:a]apad=pad_dur=0.25[a{k}];" for k in range(len(falas))) + \
             "".join(f"[a{k}]" for k in range(len(falas))) + f"concat=n={len(falas)}:v=0:a=1[out]"
    sh("ffmpeg", "-y", *partes, "-filter_complex", filtro, "-map", "[out]", "-ar", "44100", str(pasta / "voz.wav"))

    # som: voz + "whoosh" em cada palavra que salta + trilha (se houver músicas em fabrica/musicas)
    entradas, mix = ["-i", str(pasta / "voz.wav")], ["[0:a]"]
    sons = []  # sem som de transição (decisão da dona); as palavras continuam saltando na tela
    if sons:
        whoosh(pasta / "whoosh.wav")
        entradas += ["-i", str(pasta / "whoosh.wav")]
        filtros = f"[1:a]asplit={len(sons)}" + "".join(f"[w{k}]" for k in range(len(sons))) + ";"
        for k, (t0, _) in enumerate(sons):
            ms = max(0, int((t0 - 0.2) * 1000))
            filtros += f"[w{k}]adelay={ms}|{ms}[wd{k}];"
            mix.append(f"[wd{k}]")
    else:
        filtros = ""
    musicas = sorted((AQUI / "musicas").glob("*.mp3")) if (AQUI / "musicas").exists() else []
    if musicas:
        idx = entradas.count("-i")
        entradas += ["-stream_loop", "-1", "-i", str(random.choice(musicas))]
        filtros += f"[{idx}:a]volume=0.07[mus];"
        mix.append("[mus]")
    if len(mix) > 1:
        filtros += "".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[som]"
        sh("ffmpeg", "-y", *entradas, "-filter_complex", filtros, "-map", "[som]", "-ar", "44100", str(pasta / "som.wav"))
    else:
        shutil.copy(pasta / "voz.wav", pasta / "som.wav")

    lista_v = pasta / "cenas.txt"
    with open(lista_v, "w") as f:
        for c in clipes:
            f.write(f"file '{c.name}'\n")
    sh("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lista_v), "-c", "copy", str(pasta / "imagem.mp4"))

    legenda_ass(legendas, pasta / "legenda.ass", destaques)
    final = pasta / "final.mp4"
    (pasta / "fontes").mkdir(exist_ok=True)
    shutil.copy(FONTE, pasta / "fontes" / FONTE.name)
    # cor mais viva + vinheta leve (cara de vídeo editado) e a legenda dinâmica por cima
    capa = pasta / "thumb.jpg"
    rosto = None
    try:
        rosto = None  # padrão fixo da dona: palavras numa metade + UMA imagem da situação na outra (sem rosto)
    except Exception as e:
        log("  rosto da persona falhou:", str(e)[:150])
    ia = thumb(roteiro.get("thumb_text") or job["title"], roteiro.get("thumb_highlight", ""),
               roteiro.get("thumb_prompt") or "close-up of the health problem", capa, pasta,
               rosto=rosto, nicho=job.get("niche_key", ""), busca=roteiro.get("thumb_query", ""))
    # thumb 100% IA (persona definitiva + imagem de IA)? Se não, o refazedor troca quando a cota voltar
    if ia:
        banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"}, timeout=60,
                     headers={"Prefer": "resolution=merge-duplicates"}, json={"key": f"thumb_ia:{job['id']}", "value": "ok"})
    c = banco.get("yt_channels", channel_id=f"eq.{job.get('channel_id', '')}", select="long_uploads_status")
    verificado = bool(c) and c[0].get("long_uploads_status") == "allowed"
    link = link_do_job(banco, job)
    entradas_v = ["-i", str(pasta / "imagem.mp4"), "-i", str(pasta / "som.wav")]
    grade = "eq=saturation=1.18:contrast=1.06,vignette=PI/5"
    fc, atual, k = f"[0:v]{grade}[g0];", "g0", 2
    if link and ctas:
        cartao_qr(link, pasta / "qr.png")
        entradas_v += ["-loop", "1", "-t", f"{t:.2f}", "-i", str(pasta / "qr.png")]
        quando = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in ctas)
        fc += f"[{atual}][{k}:v]overlay=x=W-w-50:y=50:enable='{quando}'[q{k}];"
        atual, k = f"q{k}", k + 1
    if verificado:
        fc += f"[{atual}]subtitles=legenda.ass:fontsdir=fontes[v]"
    else:
        # canal sem verificação não aceita thumb personalizada: o YouTube escolhe a capa entre quadros
        # por volta de 1/4, 1/2 e 3/4 do vídeo — a nossa thumb aparece ~1,2s nesses pontos (limpa, sem legenda)
        entradas_v += ["-loop", "1", "-t", f"{t:.2f}", "-i", str(capa)]
        pontos = "+".join(f"between(t,{t * f - 0.6:.2f},{t * f + 0.6:.2f})" for f in (0.25, 0.5, 0.75))
        fc += (f"[{atual}]subtitles=legenda.ass:fontsdir=fontes[s];"
               f"[{k}:v]scale={W}:{H}[capa];[s][capa]overlay=0:0:enable='{pontos}'[v]")
        log("  canal sem verificação: capa colocada dentro do vídeo (1/4, 1/2 e 3/4)")
    filtro_v = ["-filter_complex", fc, "-map", "[v]", "-map", "1:a"]
    sh("ffmpeg", "-y", *entradas_v, *filtro_v, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(final))

    return final, capa, int(duracao(final))


def refazer_thumbs(banco: "Banco", limite: int = 40) -> int:
    """Troca a thumb provisória dos vídeos publicados (últimos 3 dias) por uma 100% IA, assim que houver cota."""
    from datetime import timedelta
    from googleapiclient.http import MediaFileUpload
    desde = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    feitos_ok = {r["key"].split(":", 1)[1] for r in banco.get("app_secrets", key="like.thumb_ia:*", select="key")}
    jobs = [j for j in banco.get("yt_jobs", status="eq.publicado", published_at=f"gte.{desde}", select="*", order="published_at")
            if j["id"] not in feitos_ok and j.get("video_id")]
    trocadas = 0
    for job in jobs[:limite]:
        pasta = Path(tempfile.mkdtemp(prefix="thumb_"))
        try:
            r = job.get("script") or {}
            rosto = None  # padrão fixo: sem rosto
            capa = pasta / "thumb.jpg"
            ia = thumb(r.get("thumb_text") or job["title"], r.get("thumb_highlight", ""),
                                       r.get("thumb_prompt") or "close-up of the health problem", capa, pasta, rosto=rosto,
                                       nicho=job["niche_key"], busca=r.get("thumb_query", ""), so_ia=True)
            if not ia:
                # sem cota de IA: só troca se ainda está com a versão de rosto de banco (marca thumb_foto)
                if banco.get("app_secrets", key=f"eq.thumb_foto:{job['id']}", select="key"):
                    continue
                thumb(r.get("thumb_text") or job["title"], r.get("thumb_highlight", ""), "", capa, pasta, rosto=None,
                      nicho=job["niche_key"], busca=r.get("thumb_query", ""))
                youtube_do_canal(banco, job["channel_id"]).thumbnails().set(
                    videoId=job["video_id"], media_body=MediaFileUpload(str(capa), mimetype="image/jpeg")).execute()
                banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"}, timeout=60,
                             headers={"Prefer": "resolution=merge-duplicates"}, json={"key": f"thumb_foto:{job['id']}", "value": "ok"})
                log(f"  thumb sem rosto aplicada (a de IA entra quando a cota voltar): {job['video_id']}")
                continue
            youtube_do_canal(banco, job["channel_id"]).thumbnails().set(
                videoId=job["video_id"], media_body=MediaFileUpload(str(capa), mimetype="image/jpeg")).execute()
            banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"}, timeout=60,
                         headers={"Prefer": "resolution=merge-duplicates"}, json={"key": f"thumb_ia:{job['id']}", "value": "ok"})
            trocadas += 1
            log(f"  thumb nova (IA) aplicada: {job['video_id']}")
        except Exception as e:
            log("  falhou ao refazer thumb:", str(e)[:150])
        finally:
            shutil.rmtree(pasta, ignore_errors=True)
    return trocadas


# ------------------------------------------------------------------ YouTube
def youtube_do_canal(banco: Banco, channel_id: str):
    t = banco.get("yt_tokens", channel_id=f"eq.{channel_id}", select="token")
    if not t:
        raise RuntimeError("Canal sem token na nuvem")
    tok = t[0]["token"]
    return proxy_canal.youtube(tok)  # sem proxy: só na nuvem, pelo IP do servidor (SEM_PROXY_PELO_SERVIDOR=1)


_URL = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|co|info|link|site)\b\S*)", re.I)


def sem_links(texto: str) -> str:
    """Tira QUALQUER link que a IA tenha inventado (ex.: example.com): o único link é o nosso, rastreado."""
    texto = _URL.sub("", texto or "")
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


INTERVALO_MIN = (8, 15)  # minutos entre uma publicação e outra na rede inteira (sorteado)


def reservar_horario(banco: Banco) -> datetime:
    """Próximo horário livre para publicar (as várias máquinas disputam: troca atômica em app_secrets)."""
    agora = datetime.now(timezone.utc)
    for _ in range(8):
        r = banco.get("app_secrets", key="eq.proximo_horario", select="value")
        if not r:
            banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"}, timeout=60,
                         headers={"Prefer": "resolution=ignore-duplicates"},
                         json={"key": "proximo_horario", "value": (agora - timedelta(hours=1)).isoformat()})
            continue
        antigo = r[0]["value"]
        ultimo = datetime.fromisoformat(antigo)
        horario = max(agora, ultimo + timedelta(minutes=random.randint(*INTERVALO_MIN))).replace(microsecond=0)
        ok = banco.s.patch(f"{banco.url}/rest/v1/app_secrets", params={"key": "eq.proximo_horario", "value": f"eq.{antigo}"},
                           json={"value": horario.isoformat()}, headers={"Prefer": "return=representation"}, timeout=60)
        if ok.ok and ok.json():
            return horario
        time.sleep(random.uniform(0.5, 2))
    return agora + timedelta(minutes=random.randint(*INTERVALO_MIN))


def descricao(banco: Banco, job: dict, roteiro: dict) -> str:
    """Link no TOPO da descrição (é o que aparece antes do 'mais'), depois o texto do roteiro."""
    link = link_do_job(banco, job)
    texto = sem_links(roteiro.get("description", ""))
    if not link:
        return texto
    gancho = sem_links(roteiro.get("link_hook") or "") or "The full recipe I mentioned in the video, step by step, is here 👇"
    return (f"{gancho}\n👉 {link}\n\n{texto}\n\n"
            f"📌 Also in the first comment below and in the QR code on screen.")


def subir(banco: Banco, job: dict, roteiro: dict, video: Path, capa: Path, publico: bool = False, privado: bool = False) -> str:
    from googleapiclient.http import MediaFileUpload
    yt = youtube_do_canal(banco, job["channel_id"])
    corpo = {
        "snippet": {"title": job["title"][:100], "description": descricao(banco, job, roteiro)[:4900],
                    "tags": roteiro.get("tags", [])[:15], "categoryId": "26", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True,
                   "embeddable": True, "publicStatsViewable": True},
    }
    if publico:
        corpo["status"]["privacyStatus"] = "public"
    elif privado:
        corpo["status"]["privacyStatus"] = "private"
    req = yt.videos().insert(part="snippet,status", body=corpo,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(capa), mimetype="image/jpeg")).execute()
    except Exception as e:
        log("  thumb não aplicada:", str(e)[:150])
    return vid


def publicar(banco: Banco, job: dict):
    """Deixa PÚBLICO e põe o 1º comentário com o link rastreado (fixar comentário não existe na API do YouTube)."""
    conferir_link(banco, job)  # última conferência: nicho do canal, produto certo e página no ar
    yt = youtube_do_canal(banco, job["channel_id"])
    yt.videos().update(part="status", body={"id": job["video_id"], "status": {
        "privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True,
        "embeddable": True, "publicStatsViewable": True}}).execute()
    link = link_do_job(banco, job)
    if link:
        texto = sem_links((job.get("script") or {}).get("comment") or "") or "I left the full recipe for how to get rid of this right here 👇"
        yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": job["video_id"], "topLevelComment": {
            "snippet": {"textOriginal": comentario_com_link(texto, link)}}}}).execute()


def comentario_com_link(texto: str, link: str) -> str:
    """Link logo depois da 1ª frase (antes do 'Ler mais'); o resto do texto vem embaixo."""
    m = re.match(r"(.+?[.!?…](?:\s*[^\w\s]{1,3})?)\s+(.*)", texto.strip(), re.S)
    primeira, resto = (m.group(1), m.group(2)) if m else (texto.strip(), "")
    # frases do resto que só apontavam pro link ("está no link aqui 👇") saem: o link já está em cima
    frases = re.split(r"(?<=[.!?…👇])\s+|\n+", resto)
    resto = " ".join(f for f in frases if not re.search(r"\blink\b|👇", f, re.I)).strip()
    resto = re.sub(r"\s*\n\s*\n\s*", "\n\n", resto)
    return f"{primeira} The full step-by-step is here 👇\n👉 {link}\n\n{resto}".strip()


def marcar_publicado(banco: Banco, job: dict):
    agora = datetime.now(timezone.utc).isoformat()
    # este vídeo substitui outro (ex.: versão com a capa dentro do vídeo)? a versão antiga vira PRIVADA (não apaga)
    try:
        sub = banco.get("app_secrets", key=f"eq.substituir:{job['id']}", select="value")
        if sub:
            youtube_do_canal(banco, job["channel_id"]).videos().update(part="status", body={"id": sub[0]["value"], "status": {
                "privacyStatus": "private", "selfDeclaredMadeForKids": False}}).execute()
            banco.s.patch(f"{banco.url}/rest/v1/yt_videos", params={"video_id": f"eq.{sub[0]['value']}"},
                          json={"privacy": "private"}, timeout=60)
            # some da lista de publicados no painel (fica registrado como substituído)
            banco.s.patch(f"{banco.url}/rest/v1/yt_jobs", params={"video_id": f"eq.{sub[0]['value']}"}, timeout=60,
                          json={"status": "descartado", "error": "substituído pela versão nova (antigo ficou privado)"})
            log("  versão antiga ficou privada:", sub[0]["value"])
    except Exception as e:
        log("  não deixei a versão antiga privada:", str(e)[:150])
    banco.job(job["id"], status="publicado", published_at=job.get("published_at") or agora, error=None)
    # já aparece no painel (Vídeos, números do dia) sem esperar o coletor de hora em hora
    try:
        banco.s.post(f"{banco.url}/rest/v1/yt_videos", params={"on_conflict": "video_id"},
                     headers={"Prefer": "resolution=merge-duplicates"}, timeout=60, json={
            "video_id": job["video_id"], "channel_id": job["channel_id"], "user_id": job["user_id"],
            "title": job["title"][:100], "published_at": job.get("published_at") or agora,
            "thumbnail_url": f"https://i.ytimg.com/vi/{job['video_id']}/mqdefault.jpg",
            "duration_s": job.get("duration_s"), "is_short": False, "privacy": "public", "synced_at": agora})
    except Exception as e:
        log("  não registrei no painel:", str(e)[:120])
    if job.get("title_id"):
        banco.patch("yt_title_bank", {"id": f"eq.{job['title_id']}"},
                    {"status": "usado", "used_at": agora, "video_id": job["video_id"]})


# ------------------------------------------------------------------ rodada
def processar(banco: Banco, job: dict):
    jid = job["id"]
    if not job.get("pg"):
        job["pg"] = jid.replace("-", "")[:12]
    banco.job(jid, status="gerando", attempts=job["attempts"] + 1, error=None, pg=job["pg"])
    if os.environ.get("PUBLICAR_DIRETO") == "1":
        conferir_link(banco, job)  # nicho/link/página errados: nem começa (vira "erro" com o motivo no painel)
    roteiro = job.get("script")
    if not roteiro or job["status"] == "refazer":
        log("  escrevendo roteiro...")
        roteiro = roteirista.escrever(job, banco)
        banco.job(jid, script=roteiro, description=roteiro.get("description"), tags=roteiro.get("tags"),
                  thumb_text=roteiro.get("thumb_text"))
    cwd = str(AQUI)
    for tentativa in range(3):
        pasta = Path(tempfile.mkdtemp(prefix="video_"))
        try:
            os.chdir(pasta)
            video, capa, dur = montar(banco, job, roteiro, pasta, velocidade=0.95 if tentativa < 2 else 0.87)
            break
        except RoteiroCurto as e:
            os.chdir(cwd)
            shutil.rmtree(pasta, ignore_errors=True)
            if tentativa == 2:
                raise
            falta = int((MIN_SEGUNDOS + 30 - e.seg) * 1.9) + 60
            log(f"  roteiro curto ({e}) — pedindo +{falta} palavras")
            roteiro = roteirista.escrever({**job, "script": roteiro, "feedback":
                f"The previous version was too short ({e.seg:.0f} seconds). The video MUST be at least 7 minutes. "
                f"Keep everything that works and ADD about {falta} more words: more explanation, a short story, "
                f"one more practical tip and more detail on each point. Return the COMPLETE new script."}, banco)
            banco.job(jid, script=roteiro, description=roteiro.get("description"), tags=roteiro.get("tags"),
                      thumb_text=roteiro.get("thumb_text"))
        finally:
            os.chdir(cwd)
    try:
        if job.get("video_id") and job["status"] == "refazer":
            try:  # apaga a versão anterior (não listada) para não acumular
                youtube_do_canal(banco, job["channel_id"]).videos().delete(id=job["video_id"]).execute()
            except Exception as e:
                log("  não apaguei a versão antiga:", str(e)[:100])
        direto = os.environ.get("PUBLICAR_DIRETO") == "1" and bool(link_do_job(banco, job))
        horario = reservar_horario(banco) if direto else None
        ja = direto and horario <= datetime.now(timezone.utc) + timedelta(minutes=2)
        log("  subindo", "PÚBLICO" if ja else (f"PRIVADO, vira público às {horario:%H:%M} UTC" if direto else "não listado"), "...")
        vid = subir(banco, job, roteiro, video, capa, publico=ja, privado=direto and not ja)
        if direto and not ja:
            # espera o horário dele como PRIVADO (não é o agendamento do YouTube): a fábrica deixa público na hora
            banco.job(jid, status="aprovado", video_id=vid, duration_s=dur, feedback=None, published_at=horario.isoformat(),
                      thumb_url=f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
        elif direto:  # já sobe público: só falta o 1º comentário com o link
            feito = {**job, "video_id": vid, "script": roteiro, "duration_s": dur}
            banco.job(jid, video_id=vid, duration_s=dur, feedback=None, thumb_url=f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
            try:
                publicar(banco, feito)
                marcar_publicado(banco, feito)
            except Exception as e:  # o vídeo já está no ar; a próxima rodada tenta o comentário de novo
                banco.job(jid, status="aprovado", error=f"comentário: {str(e)[:300]}")
        else:
            banco.job(jid, status="revisar", video_id=vid, duration_s=dur, feedback=None,
                      thumb_url=f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                      error=None if not os.environ.get("PUBLICAR_DIRETO") else "Nicho sem link de afiliado: ficou não listado esperando o link")
        log(f"  PRONTO: https://youtu.be/{vid} ({dur}s)")
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def publicar_devidos(banco: Banco) -> int:
    """Publica NO MÁXIMO 1 vídeo vencido por vez. Se a nuvem atrasou e vários venceram juntos,
    os outros ganham horários novos espaçados (nunca sai um monte de uma vez)."""
    agora = datetime.now(timezone.utc).isoformat()
    vencidos = banco.get("yt_jobs", status="eq.aprovado", select="*", order="published_at.nullsfirst",
                         **{"or": f"(published_at.is.null,published_at.lte.{agora})"})
    if not vencidos:
        return 0
    # respeita o intervalo desde a ÚLTIMA publicação real da rede
    ult = banco.get("yt_jobs", status="eq.publicado", select="updated_at", order="updated_at.desc", limit="1")
    if ult and datetime.now(timezone.utc) - datetime.fromisoformat(ult[0]["updated_at"]) < timedelta(minutes=INTERVALO_MIN[0]):
        for j in vencidos:
            banco.job(j["id"], published_at=reservar_horario(banco).isoformat())
        log(f"  {len(vencidos)} vídeo(s) remarcados: a última publicação foi há menos de {INTERVALO_MIN[0]} min")
        return 0
    job, resto = vencidos[0], vencidos[1:]
    for j in resto:
        banco.job(j["id"], published_at=reservar_horario(banco).isoformat())
    if resto:
        log(f"  {len(resto)} vídeo(s) atrasados ganharam horários novos espaçados")
    # "pega" o vídeo de forma atômica: duas máquinas nunca publicam (nem comentam) o mesmo vídeo
    r = banco.s.patch(f"{banco.url}/rest/v1/yt_jobs", params={"id": f"eq.{job['id']}", "status": "eq.aprovado"},
                      json={"status": "publicando"}, headers={"Prefer": "return=representation"}, timeout=60)
    if not (r.ok and r.json()):
        return 0
    for tentativa in range(3):  # o YouTube às vezes dá erro 503 passageiro
        try:
            log("publicando", job["title"])
            publicar(banco, job)
            marcar_publicado(banco, job)
            return 1
        except LinkErrado as e:
            banco.job(job["id"], status="erro", error=f"link: {e}")
            return 0
        except Exception as e:
            if tentativa == 2:
                banco.job(job["id"], status="aprovado", published_at=reservar_horario(banco).isoformat(),
                          error=f"publicar (vou tentar de novo): {str(e)[:300]}")
                log("ERRO publicando:", e)
            else:
                time.sleep(20 * (tentativa + 1))
    return 0


def main():
    banco = Banco()
    publicar_devidos(banco)
    fila = banco.get("yt_jobs", status="in.(na_fila,refazer)", select="*", order="created_at")
    random.shuffle(fila)  # ordem e horários de postagem variam todo dia
    feitos = 0
    for job in fila:
        if feitos >= MAX_POR_RODADA:
            break
        # pega o vídeo só se ninguém pegou (várias máquinas trabalham juntas)
        r = banco.s.patch(f"{banco.url}/rest/v1/yt_jobs", params={"id": f"eq.{job['id']}", "status": f"eq.{job['status']}"},
                          json={"status": "gerando"}, headers={"Prefer": "return=representation"}, timeout=60)
        if not (r.ok and r.json()):
            continue
        feitos += 1
        log("fazendo:", job["title"])
        try:  # bio do canal com o link e palavras-chave do nicho (uma vez por nicho)
            import perfil
            perfil.garantir_perfil(banco, sys.modules[__name__], job["channel_id"])
        except Exception as e:
            log("  bio não atualizada:", str(e)[:150])
        try:
            processar(banco, job)
            publicar_devidos(banco)  # entre um vídeo e outro, publica o que venceu (1 por vez)
        except Exception as e:
            traceback.print_exc()
            msg = str(e)
            # falha passageira (Gemini sobrecarregado, rede, proxy): volta pra fila e tenta de novo (até 6 tentativas)
            passageira = any(k in msg for k in ("Gemini não escreveu", "503", "429", "timed out", "Connection", "SOCKS", "Max retries"))
            if passageira and job.get("attempts", 0) < 6:
                banco.job(job["id"], status="na_fila", error=f"vou tentar de novo: {msg[:300]}")
            else:
                banco.job(job["id"], status="erro", error=msg[:900])
    log("rodada terminada")


if __name__ == "__main__":
    if "--thumbs" in sys.argv:  # troca thumbs provisórias por thumbs 100% IA quando a cota voltar
        b = Banco()
        publicar_devidos(b)  # redundância: se a fábrica estiver parada, este relógio também publica (1 por vez)
        # garante: vídeo substituto publicado => versão antiga privada (caso a máquina que publicou fosse anterior)
        for s_ in b.get("app_secrets", key="like.substituir:*", select="key,value"):
            j = b.get("yt_jobs", id=f"eq.{s_['key'].split(':', 1)[1]}", select="id,channel_id,status")
            if j and j[0]["status"] == "publicado":
                try:
                    youtube_do_canal(b, j[0]["channel_id"]).videos().update(part="status", body={"id": s_["value"], "status": {
                        "privacyStatus": "private", "selfDeclaredMadeForKids": False}}).execute()
                    b.s.delete(f"{b.url}/rest/v1/app_secrets", params={"key": f"eq.{s_['key']}"}, timeout=60)
                    b.s.patch(f"{b.url}/rest/v1/yt_jobs", params={"video_id": f"eq.{s_['value']}"}, timeout=60,
                              json={"status": "descartado", "error": "substituído pela versão nova (antigo ficou privado)"})
                    log("versão antiga ficou privada:", s_["value"])
                except Exception as e:
                    log("não deixei privada:", str(e)[:120])
        log("thumbs trocadas:", refazer_thumbs(b))
    else:
        main()
