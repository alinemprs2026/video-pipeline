# /// script
# requires-python = ">=3.10"
# dependencies = ["google-auth-oauthlib>=1.2", "google-api-python-client>=2.150", "requests[socks]>=2.31", "tzdata"]
# ///
"""Coletor da Rede YouTube: lê views, inscritos e vídeos de cada canal conectado
(SEMPRE pelo proxy do próprio canal) e grava no Supabase do painel.

Uso:  uv run coletar.py           -> coleta e grava
      uv run coletar.py --seco    -> só mostra o que leu, não grava nada

Custo de cota da API: ~3 unidades por canal por rodada (cabe rodar de hora em hora).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "conector"))
import proxy_canal  # noqa: E402

TOKENS = Path.home() / ".youtube-uploader" / "tokens"
RAIZ = AQUI.parent.parent
BR = ZoneInfo("America/Sao_Paulo")
SECO = "--seco" in sys.argv
DIAS_COM_FOTO = 30  # vídeos mais velhos só atualizam o total (economiza banco)
GUARDAR_FOTOS_DIAS = 100  # fotos diárias mais velhas que isso são apagadas


def ler_env() -> dict[str, str]:
    env = {}
    for nome in (".env.local", ".env"):
        arq = RAIZ / nome
        if arq.exists():
            for linha in arq.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*)$", linha)
                if m and m.group(1) not in env:
                    env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


class Banco:
    def __init__(self, env: dict[str, str]):
        self.url = env["SUPABASE_URL"].rstrip("/")
        chave = env["SUPABASE_SERVICE_ROLE_KEY"]
        self.s = requests.Session()
        self.s.headers.update({"apikey": chave, "Authorization": f"Bearer {chave}"})
        admins = [e.strip().lower() for e in env.get("ADMIN_EMAILS", "").split(",") if e.strip()]
        if not admins:  # na nuvem: o dono é quem já tem canais (não precisa do e-mail)
            r = self.s.get(f"{self.url}/rest/v1/yt_channels", params={"select": "user_id", "limit": "1"}, timeout=30)
            r.raise_for_status()
            if r.json():
                self.user_id = r.json()[0]["user_id"]
                return
            raise SystemExit("Nenhum canal no Supabase para descobrir o dono.")
        r = self.s.get(f"{self.url}/auth/v1/admin/users", params={"per_page": 200}, timeout=30)
        r.raise_for_status()
        donos = [u for u in r.json().get("users", []) if (u.get("email") or "").lower() in admins]
        if not donos:
            raise SystemExit("Não achei o usuário dono (ADMIN_EMAILS) no Supabase.")
        self.user_id = donos[0]["id"]

    def leituras_por_hora(self) -> dict:
        r = self.s.get(f"{self.url}/rest/v1/yt_channels", params={"select": "channel_id,hourly"}, timeout=30)
        r.raise_for_status()
        return {c["channel_id"]: c.get("hourly") or [] for c in r.json()}

    def upsert(self, tabela: str, linhas: list[dict], conflito: str):
        for i in range(0, len(linhas), 500):
            r = self.s.post(f"{self.url}/rest/v1/{tabela}", params={"on_conflict": conflito},
                            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                            json=linhas[i:i + 500], timeout=60)
            if r.status_code >= 300:
                raise RuntimeError(f"{tabela}: {r.status_code} {r.text[:300]}")


def segundos(iso: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def coletar_canal(token: dict) -> tuple[dict, list[dict]]:
    yt = proxy_canal.youtube(token)
    itens = yt.channels().list(part="snippet,statistics,contentDetails,status", mine=True).execute().get("items") or []
    if not itens:
        raise RuntimeError("accountClosed: a conta não tem mais canal (removido ou suspenso)")
    canal = itens[0]
    uploads = canal["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = []
    try:
        pl = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50).execute()
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
    except Exception as e:  # canal sem nenhum vídeo: a playlist ainda não existe
        if "playlistNotFound" not in str(e):
            raise
    videos = []
    if ids:
        videos = yt.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(ids)).execute()["items"]
    try:
        canal["_analytics"] = ler_analytics(token)
    except Exception as e:  # Analytics nunca derruba a coleta
        print(f"     (analytics indisponível: {str(e)[:80]})")
    return canal, videos


def ler_analytics(token: dict) -> dict:
    """Últimos 28 dias do YouTube Analytics (chega com ~2 dias de atraso)."""
    ya = proxy_canal.youtube(token, "youtubeAnalytics", "v2")
    fim = datetime.now(BR).date()
    ini = fim - timedelta(days=28)
    base = {"ids": "channel==MINE", "startDate": ini.isoformat(), "endDate": fim.isoformat()}
    q = lambda **kw: ya.reports().query(**base, **kw).execute().get("rows") or []
    dias = q(metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost", dimensions="day", sort="day")
    fontes = q(metrics="views", dimensions="insightTrafficSourceType", sort="-views")
    paises = q(metrics="views", dimensions="country", sort="-views", maxResults=8)
    top = q(metrics="views,estimatedMinutesWatched,averageViewDuration", dimensions="video", sort="-views", maxResults=10)
    return {
        "de": ini.isoformat(), "ate": fim.isoformat(), "lido_em": datetime.now(timezone.utc).isoformat(),
        "dias": [{"day": d, "views": v, "minutos": m, "duracao_media": a, "ganhos": g, "perdidos": p} for d, v, m, a, g, p in dias],
        "fontes": [{"fonte": f, "views": v} for f, v in fontes],
        "paises": [{"pais": c, "views": v} for c, v in paises],
        "top": [{"video_id": i, "views": v, "minutos": m, "duracao_media": a} for i, v, m, a in top],
    }


def main():
    env = {**os.environ, **ler_env()}
    banco = None if SECO else Banco(env)
    horas = banco.leituras_por_hora() if banco else {}
    agora = datetime.now(timezone.utc)
    hoje = agora.astimezone(BR).date().isoformat()
    limite_foto = agora - timedelta(days=DIAS_COM_FOTO)
    ok = erro = 0

    if os.environ.get("TOKENS_DA_NUVEM") and banco:  # na nuvem: tokens na tabela protegida
        r = banco.s.get(f"{banco.url}/rest/v1/yt_tokens", params={"select": "token"}, timeout=60)
        r.raise_for_status()
        lista_tokens = [x["token"] for x in r.json()]
    else:
        lista_tokens = [json.loads(a.read_text(encoding="utf-8")) for a in sorted(TOKENS.glob("*.json"))]
    for t in lista_tokens:
        px = t.get("proxy")
        base = {"channel_id": t["canal_id"], "name": t.get("canal_nome") or t["canal_id"],
                "api_project": t.get("projeto"), "proxy_host": px["host"] if px else None,
                "adspower_profile": ", ".join(px.get("perfis", [])) if px else None}
        if banco:
            base["user_id"] = banco.user_id
        if not px and os.environ.get("SEM_PROXY_PELO_SERVIDOR") != "1":
            print(f"  [SEM PROXY] {base['name']} — não leio pelo IP de casa, pulei")
            if banco:
                banco.upsert("yt_channels", [base], "channel_id")
            continue
        try:
            canal, videos = coletar_canal(t)
        except Exception as e:
            erro += 1
            msg = str(e)[:300]
            caiu = any(k in msg for k in ("invalid_grant", "revoked", "unauthorized", "401", "suspended",
                                          "accountClosed", "youtubeSignupRequired", "forbidden", "disabled", "IndexError"))
            if "invalid_grant" in msg or "revoked" in msg:
                msg = "Conexão perdida: o acesso foi cancelado ou a senha mudou. Reconecte pelo conector."
            elif any(k in msg for k in ("suspended", "accountClosed", "disabled", "IndexError")):
                msg = "CANAL DERRUBADO ou suspenso pelo YouTube/Google. Confira e substitua."
            print(f"  [ERRO] {base['name']}: {msg[:140]}")
            if banco:
                banco.upsert("yt_channels", [{**base, "token_ok": not caiu, "token_error": msg}], "channel_id")
            continue

        sn, st, status = canal["snippet"], canal.get("statistics", {}), canal.get("status", {})
        publicados = [v["snippet"].get("publishedAt") for v in videos if v["snippet"].get("publishedAt")]
        linha_canal = {
            **base, "name": sn["title"], "handle": sn.get("customUrl"),
            "thumbnail_url": (sn.get("thumbnails", {}).get("default") or {}).get("url"),
            "language": sn.get("defaultLanguage"),
            "subscribers": int(st.get("subscriberCount", 0)), "total_views": int(st.get("viewCount", 0)),
            "video_count": int(st.get("videoCount", 0)), "last_upload_at": max(publicados) if publicados else None,
            "token_ok": True, "token_error": None, "synced_at": agora.isoformat(),
            "long_uploads_status": status.get("longUploadsStatus"),
            "monetization": status.get("isChannelMonetizationEnabled"),
            # leituras de hora em hora do total de views (as últimas 72) = "views em tempo real"
            "hourly": (horas.get(canal["id"]) or [])[-71:] + [[agora.isoformat(), int(st.get("viewCount", 0))]],
        }
        if canal.get("_analytics"):
            linha_canal["analytics"] = canal["_analytics"]
        linhas_v, fotos = [], []
        for v in videos:
            vs, vst = v["snippet"], v.get("statistics", {})
            dur = segundos(v.get("contentDetails", {}).get("duration", ""))
            thumbs = vs.get("thumbnails", {})
            linhas_v.append({
                "video_id": v["id"], "channel_id": canal["id"], "title": vs["title"],
                "thumbnail_url": (thumbs.get("medium") or thumbs.get("default") or {}).get("url"),
                "published_at": vs.get("publishedAt"), "duration_s": dur, "is_short": 0 < dur <= 180,
                "privacy": v.get("status", {}).get("privacyStatus"),
                "views": int(vst.get("viewCount", 0)), "likes": int(vst.get("likeCount", 0)),
                "comments": int(vst.get("commentCount", 0)), "synced_at": agora.isoformat(),
                **({"user_id": banco.user_id} if banco else {}),
            })
            pub = vs.get("publishedAt")
            if pub and datetime.fromisoformat(pub.replace("Z", "+00:00")) >= limite_foto:
                fotos.append({"video_id": v["id"], "day": hoje, "views": int(vst.get("viewCount", 0)),
                              "likes": int(vst.get("likeCount", 0)), "comments": int(vst.get("commentCount", 0))})

        print(f"  [OK] {linha_canal['name']}: {linha_canal['subscribers']} inscritos, "
              f"{linha_canal['total_views']} views, {len(videos)} vídeos lidos ({'proxy ' + px['host'] if px else 'IP do servidor'})")
        ok += 1
        if banco:
            banco.upsert("yt_channels", [linha_canal], "channel_id")
            if linhas_v:
                banco.upsert("yt_videos", linhas_v, "video_id")
            if fotos:
                banco.upsert("yt_video_daily", fotos, "video_id,day")
            banco.upsert("yt_channel_daily", [{"channel_id": canal["id"], "day": hoje,
                                               "subscribers": linha_canal["subscribers"],
                                               "total_views": linha_canal["total_views"]}], "channel_id,day")

    if banco:  # limpeza: o banco grátis tem 500 MB
        corte = (agora.astimezone(BR).date() - timedelta(days=GUARDAR_FOTOS_DIAS)).isoformat()
        for tabela in ("yt_video_daily", "yt_channel_daily"):
            banco.s.delete(f"{banco.url}/rest/v1/{tabela}", params={"day": f"lt.{corte}"}, timeout=60)

    print(f"\n  Pronto: {ok} canal(is) coletado(s), {erro} com erro{' (modo seco: nada gravado)' if SECO else ''}.")


if __name__ == "__main__":
    main()
