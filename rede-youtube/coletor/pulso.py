"""Pulso: a cada 5 min atualiza views/inscritos de TODOS os canais e as views dos vídeos dos últimos 30 dias.

Barato: usa a conexão de UM canal (pelo proxy dele) para ler os números públicos de todos de uma vez
(channels.list e videos.list com até 50 ids por pedido = ~8 pontos de cota por rodada).
O coletor de hora em hora continua fazendo a parte pesada (Analytics, verificação, canal derrubado).
"""
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "conector"))
import proxy_canal  # noqa: E402

BR = ZoneInfo("America/Sao_Paulo")


def main():
    url = os.environ["SUPABASE_URL"].rstrip("/")
    k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    s = requests.Session()
    s.headers.update({"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"})
    get = lambda t, **p: s.get(f"{url}/rest/v1/{t}", params=p, timeout=60).json()  # noqa: E731

    canais = get("yt_channels", select="channel_id,user_id,hourly", paused="eq.false")
    tokens = [t["token"] for t in get("yt_tokens", select="token") if (t["token"] or {}).get("proxy")]
    if not canais or not tokens:
        print("nada para ler")
        return
    random.shuffle(tokens)
    yt = None
    for tok in tokens[:4]:  # um canal saudável basta para ler os números públicos de todos
        try:
            yt = proxy_canal.youtube(tok)
            break
        except Exception as e:
            print("token não serviu:", str(e)[:80])
    if yt is None:
        return

    agora = datetime.now(timezone.utc)
    hoje = agora.astimezone(BR).date().isoformat()
    linhas_c, diarias_c = [], []
    por_id = {c["channel_id"]: c for c in canais}
    ids = list(por_id)
    for i in range(0, len(ids), 50):
        for it in yt.channels().list(part="statistics", id=",".join(ids[i:i + 50]), maxResults=50).execute().get("items", []):
            st = it.get("statistics", {})
            c = por_id[it["id"]]
            views = int(st.get("viewCount", 0))
            horas = (c.get("hourly") or [])[-287:] + [[agora.isoformat(), views]]
            s.patch(f"{url}/rest/v1/yt_channels", params={"channel_id": f"eq.{it['id']}"}, timeout=60, json={
                "subscribers": int(st.get("subscriberCount", 0)), "total_views": views,
                "video_count": int(st.get("videoCount", 0)), "hourly": horas, "synced_at": agora.isoformat()})
            diarias_c.append({"channel_id": it["id"], "day": hoje, "subscribers": int(st.get("subscriberCount", 0)),
                              "total_views": views})

    desde = (agora - timedelta(days=30)).isoformat()
    videos = get("yt_videos", select="video_id,channel_id,user_id,title", published_at=f"gte.{desde}", limit="1000")
    meta = {v["video_id"]: v for v in videos}
    vids = list(meta)
    for i in range(0, len(vids), 50):
        lote, fotos = [], []
        for it in yt.videos().list(part="statistics", id=",".join(vids[i:i + 50]), maxResults=50).execute().get("items", []):
            st, m = it.get("statistics", {}), meta[it["id"]]
            n = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                 "comments": int(st.get("commentCount", 0))}
            lote.append({"video_id": it["id"], "channel_id": m["channel_id"], "user_id": m["user_id"],
                         "title": m["title"], "synced_at": agora.isoformat(), **n})
            fotos.append({"video_id": it["id"], "day": hoje, **n})
        if lote:
            s.post(f"{url}/rest/v1/yt_videos", params={"on_conflict": "video_id"}, json=lote, timeout=60,
                   headers={"Prefer": "resolution=merge-duplicates"})
            s.post(f"{url}/rest/v1/yt_video_daily", params={"on_conflict": "video_id,day"}, json=fotos, timeout=60,
                   headers={"Prefer": "resolution=merge-duplicates"})
    if diarias_c:
        s.post(f"{url}/rest/v1/yt_channel_daily", params={"on_conflict": "channel_id,day"}, json=diarias_c, timeout=60,
               headers={"Prefer": "resolution=merge-duplicates"})
    print(f"pulso ok: {len(diarias_c)} canais e {len(vids)} vídeos atualizados")


if __name__ == "__main__":
    main()
