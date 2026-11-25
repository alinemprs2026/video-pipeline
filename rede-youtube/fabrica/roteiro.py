"""Roteirista da fábrica: escreve o roteiro em inglês (~7 min) com o Claude (plano Max da dona,
via `claude -p` com CLAUDE_CODE_OAUTH_TOKEN). Cada canal pode ter uma VARIANTE de prompt
(teste A/B de estilo); o relatório compara qual converte mais.
"""
import json
import os
import random
import re
import subprocess

# Estruturas de roteiro dos virais de saúde 60+: muito problema no começo, promessa de
# solução, conteúdo útil de verdade, e o link como "o passo a passo completo".
VARIANTES = {
    "medo_e_alivio": "Open with a vivid, relatable scene of the problem (the viewer at home, the symptom, the worry). Spend the first 90 seconds deepening the problem and the hidden cause. Then relief: explain what actually helps.",
    "historia_vizinho": "Open with a short real-sounding story about a 60+ person (first name only) who had this exact problem, what they tried that failed, and the turning point. Use the story as the spine of the whole video.",
    "mito_vs_verdade": "Open by stating the common belief about this problem and why it is wrong. Reveal the real cause step by step, like solving a mystery, building curiosity before each reveal.",
    "lista_contagem": "Structure as a countdown list (e.g., 5 to 1), saving the most surprising item for last and teasing it from the first 20 seconds.",
}

REGRAS = """
HARD RULES (YouTube policy + audience trust):
- English, warm and simple, for people over 60. Short sentences. Talk to "you".
- AT LEAST {palavras} words of narration in total (7 to 9 minutes). Count them: shorter scripts are rejected.
- Never promise a cure, never say it "reverses" or "cures" a disease, never tell people to stop medication.
  Use "may help", "supports", "many people notice". Add one short line: "Talk to your doctor before changing anything."
- Do NOT claim to be a doctor. You are a health educator/narrator.
- Give REAL useful information (the video must be worth watching even without clicking).
- RETENTION FIRST: the first 60-90 seconds are pure hook + problem agitation: a vivid scene, a surprising fact,
  an open loop ("in a moment I'll show you the one thing..."). No intro, no "welcome back", no channel name.
- CALLS TO ACTION, VSL style (3 times: the first pitch right after the hook at ~1:15, again around the middle,
  and at the end). Speak like a caring friend who found the answer, e.g.:
  "I left the full recipe for how to get rid of this in the link — it's in the first comment right below this video."
  "If you'd rather not wait, scan the QR code on your screen now: it opens the complete step-by-step."
  "Don't close this video without checking the first comment — the {oferta} is there, and it's free to watch."
  Make it the missing piece: the video explains WHY, the link shows exactly WHAT to do. Never sound like an ad.
  Mark each of those segments with "cta": true.
- The first sentence must hook in under 8 seconds and match the title promise exactly.
- ENGAGEMENT (YouTube delivers videos with many comments): ask viewers to comment 3 times, at different moments:
  early (~1 min): "Tell me in the comments: how old are you and which city are you watching from?"
  middle: ask about THEIR problem ("Comment below: how long have you been dealing with this?")
  near the end: an either/or question that is easy to answer ("Type YES if you'll try this tonight").
- Ask for a LIKE once (around minute 3, after a useful tip: "if this helped, give it a like so more people see it")
  and to SUBSCRIBE once at a different moment (around minute 5: "subscribe so you don't miss the next one").
  Never ask for like and subscribe in the same sentence or in the same segment as the link call.
""".strip()

FORMATO = """
Return ONLY valid JSON, no markdown:
{{
  "segments": [ {{"text": "2-4 sentences of narration", "visual": {{"type": "video" or "image",
      "query": "2-4 word English stock-footage search (for type video)",
      "prompt": "detailed photorealistic image description, no text (for type image)"}},
      "cta": "true ONLY on the 3 call-to-action segments",
      "on_screen": "OPTIONAL 1-3 word punch phrase that pops on screen (e.g. 3 AM SPIKE, NOT ALLERGIES, STOP THIS)" }} ],
  "description": "YouTube description: 2 short paragraphs + 3 timestamps-free bullet points + disclaimer line + 3 hashtags",
  "tags": ["10 to 15 tags"],
  "thumb_text": "3 to 5 SHORT lines separated by |, max 4 words per line, e.g. ONLY 1 GLASS|BEFORE BED|SUGAR DROPS|BY MORNING",
  "thumb_highlight": "one line from thumb_text to put in the red box",
  "thumb_prompt": "photorealistic close-up showing the problem/ingredient dramatically, for the RIGHT half of a thumbnail",
  "link_hook": "2-3 SHORT lines for the TOP of the description, right before the link. Speak straight to the SPECIFIC pain of THIS video (name the symptom, the fear, the frustration in the viewer's own words), then promise the missing piece is behind the link. Curiosity, urgency, no cure claims. Example for morning blood sugar: 'Still waking up with high sugar even when you ate nothing last night? 😟 That number is not your fault. I put the simple morning recipe that helps here 👇'",
  "comment": "pinned-style FIRST COMMENT, 3-5 short lines: agitate the exact pain of this video, show empathy, tell them the step-by-step recipe is in the link, and end with an easy question that invites replies (age, city, or how long they have had this problem). Emojis allowed (👇 ⚠️ ✅). No cure claims.",
  "fallback_query": "generic stock footage search for this niche"
}}
Use 34 to 45 segments. Alternate visuals: mostly "video", one "image" every 3-4 segments for things stock footage can't show.
Image prompts: close-ups of objects, food, organs/body details or ONE face (head and shoulders). Never hands doing things,
never full bodies or groups (AI draws them badly). Give "on_screen" to about 1 of every 3 segments, always in the first 2 segments.
thumb_text: words of IMPACT (WARNING, STOP, NEVER, 3 AM, DANGER, numbers). thumb_prompt: the problem itself, very close, no people.
""".strip()

NICHOS_PT = {"diabetes": "blood sugar / type 2 diabetes", "pulmao": "lungs, mucus and breathing",
             "pele": "skin aging, sagging skin and wrinkles", "queda_capilar": "hair loss and thinning hair",
             "disfuncao": "men's health, erections and testosterone"}


def montar_prompt(job: dict, variante: str, feedback: str | None, oferta: str) -> str:
    p = [f"You write scripts for a faceless YouTube health channel for people over 60 (niche: {NICHOS_PT.get(job['niche_key'], job['niche_key'])}).",
         f'Video title (keep the promise exactly): "{job["title"]}"',
         "Style for this channel: " + VARIANTES[variante],
         REGRAS.format(palavras=1500, oferta=oferta)]
    if feedback:
        p.append("The channel owner reviewed the previous version and asked for these changes (follow them carefully): " + feedback)
        if job.get("script"):
            p.append("Previous version (improve it, don't start from zero unless asked): " + json.dumps(job["script"])[:12000])
    p.append(FORMATO)
    return "\n\n".join(p)


def _json(texto: str) -> dict:
    texto = texto.strip()
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise RuntimeError("O roteirista não devolveu JSON")
    return json.loads(m.group(0))


def _gemini(prompt: str) -> dict:
    """Roteirista grátis (Gemini) — usado enquanto o Claude Max não está ligado na nuvem."""
    import time
    import requests
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("Sem roteirista: falta GEMINI_API_KEY ou CLAUDE_CODE_OAUTH_TOKEN")
    ultimo = ""
    for modelo in ("gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"):
        for espera in (0, 30):
            time.sleep(espera)
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                              params={"key": chave}, timeout=300,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}})
            if r.status_code in (429, 500, 503):
                ultimo = f"{modelo} {r.status_code}"
                continue
            if not r.ok:
                ultimo = f"{modelo} {r.status_code} {r.text[:200]}"
                break
            try:
                roteiro = _json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                palavras = sum(len(s["text"].split()) for s in roteiro["segments"])
                if palavras < 1200:  # 7 min é LEI: roteiro curto volta
                    ultimo = f"{modelo} curto ({palavras} palavras)"
                    continue
                return roteiro
            except Exception as e:
                ultimo = f"{modelo} json: {e}"
    raise RuntimeError("Gemini não escreveu o roteiro: " + ultimo)


def escrever(job: dict, banco=None) -> dict:
    variante = (job.get("script") or {}).get("variant") or random.choice(list(VARIANTES))
    oferta = "the full natural routine and the exact recipe"
    prompt = montar_prompt(job, variante, job.get("feedback"), oferta)
    if banco is not None:  # ângulo da VSL do produto do nicho (estudado da página de vendas)
        try:
            n = banco.get("yt_niches", key=f"eq.{job['niche_key']}", select="rules")
            if n and n[0].get("rules"):
                prompt += ("\n\nTHE PRODUCT BEHIND THE LINK (align the story and the calls to action with it, "
                           "but never name the product or promise results):\n" + n[0]["rules"])
        except Exception:
            pass
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        roteiro = _gemini(prompt)
        roteiro["variant"] = variante
        roteiro["writer"] = "gemini"
        return roteiro
    r = subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("Claude falhou: " + r.stderr[-400:])
    saida = json.loads(r.stdout)
    roteiro = _json(saida.get("result", ""))
    roteiro["variant"] = variante
    return roteiro
