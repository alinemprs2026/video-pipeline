"""Proxy de cada canal: lê do AdsPower, guarda no token e monta as conexões
da API do YouTube saindo pelo IP do proxy (nunca pelo IP do servidor/casa)."""
import os
import json
import urllib.request

import httplib2
import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ADSPOWER = "http://127.0.0.1:50325"


def proxies_adspower() -> dict[str, dict]:
    """IP do proxy -> {tipo, host, porta, usuario, senha, perfis:[nomes]} (só os perfis com proxy)."""
    out: dict[str, dict] = {}
    pagina = 1
    while True:
        with urllib.request.urlopen(f"{ADSPOWER}/api/v1/user/list?page={pagina}&page_size=100", timeout=10) as r:
            d = json.load(r)
        lista = (d.get("data") or {}).get("list") or []
        for u in lista:
            p = u.get("user_proxy_config") or {}
            if not p.get("proxy_host"):
                continue
            item = out.setdefault(p["proxy_host"], {
                "tipo": p.get("proxy_type"), "host": p["proxy_host"], "porta": int(p["proxy_port"]),
                "usuario": p.get("proxy_user") or "", "senha": p.get("proxy_password") or "", "perfis": []})
            item["perfis"].append(u.get("name") or u.get("user_id"))
        if len(lista) < 100:
            return out
        pagina += 1


def _url(px: dict) -> str:
    esquema = "socks5h" if px["tipo"] == "socks5" else "http"
    auth = f"{px['usuario']}:{px['senha']}@" if px.get("usuario") else ""
    return f"{esquema}://{auth}{px['host']}:{px['porta']}"


def sessao(px: dict | None) -> requests.Session:
    s = requests.Session()
    if px:
        s.proxies = {"http": _url(px), "https": _url(px)}
    return s


def credenciais(token: dict) -> Credentials:
    """Renova o token PASSANDO PELO PROXY do canal."""
    creds = Credentials.from_authorized_user_info(token, token.get("scopes"))
    creds.refresh(Request(session=sessao(token.get("proxy"))))
    return creds


class _HttpPeloProxy:
    """Transporte da API do Google feito com requests (o httplib2 não autentica nesses proxies)."""
    timeout = 300
    redirect_codes = frozenset()  # 308 do upload em partes é tratado pela própria API

    def __init__(self, creds: Credentials, px: dict):
        self.s = AuthorizedSession(creds, auth_request=Request(session=sessao(px)))
        self.s.proxies = sessao(px).proxies

    def request(self, uri, method="GET", body=None, headers=None, **_):
        r = self.s.request(method, uri, data=body, headers=headers, timeout=self.timeout, allow_redirects=False)
        info = {k.lower(): v for k, v in r.headers.items()}
        info["status"] = str(r.status_code)
        return httplib2.Response(info), r.content


def youtube(token: dict, api="youtube", versao="v3"):
    """Cliente da API com TODO o tráfego (token, upload, thumb, comentário) saindo pelo proxy do canal."""
    px = token.get("proxy")
    if not px:
        # Só NA NUVEM (servidor, IP compartilhado e trocado a cada rodada) é permitido sem proxy — nunca pelo IP de casa.
        if os.environ.get("SEM_PROXY_PELO_SERVIDOR") == "1":
            from google.oauth2.credentials import Credentials
            return build(api, versao, credentials=Credentials.from_authorized_user_info(token), cache_discovery=False,
                         static_discovery=True)
        raise RuntimeError(f"Canal {token.get('canal_nome')} sem proxy vinculado. Abra /vincular no perfil dele.")
    return build(api, versao, http=_HttpPeloProxy(credenciais(token), px), cache_discovery=False,
                 static_discovery=True)


def ip_de_saida(px: dict) -> str:
    return sessao(px).get("https://api64.ipify.org", timeout=20).text.strip()
