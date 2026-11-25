"""Bio do canal (descrição + palavras-chave do nicho + link rastreado). Feito pela API, pelo proxy do canal."""
import json

BIO = {
    "diabetes": ("Simple, natural tips to support healthy blood sugar after 50.",
                 "blood sugar, type 2 diabetes, A1C, insulin resistance, morning blood sugar, pancreas health, diabetic diet",
                 "the simple morning drink recipe that supports healthy blood sugar"),
    "pulmao": ("Breathe easier after 60: lung health, COPD and shortness-of-breath tips explained simply.",
               "lung health, COPD, shortness of breath, breathing exercises, inhaler, lung cleanse, weak lungs",
               "the simple breathing ritual many seniors use to breathe deeper"),
    "pele": ("Firmer, younger-looking skin after 50: collagen, wrinkles and sagging skin made simple.",
             "collagen, sagging skin, wrinkles, anti aging, skin care over 50, crepey skin, jowls",
             "the collagen routine that supports firmer skin from the inside"),
    "queda_capilar": ("Thinning hair and shedding after 40? Natural hair care tips that really make sense.",
                      "hair loss, thinning hair, hair growth, rosemary for hair, hair shedding, menopause hair loss, scalp care",
                      "the simple rosemary ritual for thinning hair"),
    "disfuncao": ("Men's health after 50: prostate, energy and vitality explained man to man.",
                  "prostate health, men over 50, male vitality, testosterone, enlarged prostate, night urination, men's health",
                  "the natural morning trick for men's vitality and prostate health"),
    "emagrecimento": ("Lose stubborn belly fat after 50 with simple, natural habits.",
                      "belly fat, weight loss after 50, metabolism, natural ozempic, hunger cravings, menopause weight, fat burning",
                      "the morning shot recipe that helps crush hunger"),
    "neuropatia": ("Tingling, numb or burning feet? Simple nerve health tips for seniors.",
                   "neuropathy, tingling feet, numb feet, burning feet, nerve pain, diabetic neuropathy, nerve health",
                   "the pink salt foot ritual for tingling and numbness"),
    "memoria": ("Keep your mind sharp after 60: memory, brain fog and brain health tips.",
                "memory loss, brain fog, brain health, dementia prevention, alzheimer's, focus, healthy aging brain",
                "the cinnamon and honey morning ritual for memory"),
    "pressao": ("High blood pressure after 50? Heart and artery health explained simply.",
                "high blood pressure, hypertension, heart health, clogged arteries, cholesterol, stroke prevention, circulation",
                "the arterial flush ritual seniors use for healthy blood pressure"),
}


def texto_bio(nicho: str, link: str) -> tuple[str, str]:
    frase, chaves, oferta = BIO[nicho]
    descricao = (
        f"{frase}\n\n"
        f"👉 FREE: {oferta} — watch it here:\n{link}\n\n"
        "New videos every day with practical, easy-to-follow advice for people over 50.\n"
        "Subscribe and turn on notifications so you never miss one.\n\n"
        "⚠️ This channel is for education only and is not medical advice. Always talk to your doctor "
        "before changing your diet, supplements or medication.\n\n"
        f"Topics: {chaves}."
    )
    palavras = " ".join(f'"{k.strip()}"' if " " in k.strip() else k.strip() for k in chaves.split(","))
    return descricao[:1000], palavras[:500]


def garantir_perfil(banco, F, channel_id: str, forcar: bool = False) -> bool:
    """Atualiza a bio só quando o canal ainda não tem a bio do nicho atual (marca em app_secrets)."""
    c = banco.get("yt_channels", channel_id=f"eq.{channel_id}", select="user_id,niche_id,name")
    if not c or not c[0].get("niche_id"):
        return False
    n = banco.get("yt_niches", id=f"eq.{c[0]['niche_id']}", select="key,affiliate_link")
    if not n or not n[0].get("affiliate_link") or n[0]["key"] not in BIO:
        return False
    nicho, chave = n[0]["key"], f"perfil_ok:{channel_id}"
    feito = banco.get("app_secrets", key=f"eq.{chave}", select="value")
    if feito and feito[0]["value"] == nicho and not forcar:
        return False
    slug = F.canal_slug(banco, channel_id)
    link_base = n[0]["affiliate_link"]
    sep = "&" if "?" in link_base else "?"
    link = f"{link_base}{sep}pg={('bio-' + slug)[:40]}&utm_source=youtube&utm_campaign={slug}"
    descricao, palavras = texto_bio(nicho, link)
    yt = F.youtube_do_canal(banco, channel_id)
    atual = yt.channels().list(part="brandingSettings", id=channel_id).execute()["items"][0]["brandingSettings"]
    canal = atual.get("channel", {})
    canal.update({"description": descricao, "keywords": palavras, "defaultLanguage": "en", "country": "US"})
    yt.channels().update(part="brandingSettings", body={"id": channel_id, "brandingSettings": {"channel": canal}}).execute()
    banco.s.post(f"{banco.url}/rest/v1/app_secrets", params={"on_conflict": "key"},
                 headers={"Prefer": "resolution=merge-duplicates"}, json={"key": chave, "value": nicho}, timeout=60)
    F.log(f"  bio do canal atualizada ({nicho})")
    return True
