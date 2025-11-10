# assistant/views.py
import os
import re
import json
import heapq
import pathlib
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from dotenv import load_dotenv

import httpx
from openai import OpenAI

CATALOG_KEYWORDS = re.compile(r"\b(preț|pret|price|cod|code)\b", re.IGNORECASE | re.UNICODE)
MIN_OVERLAP = 2  # legalább 2 közös token a kérdéssel

load_dotenv()
logger = logging.getLogger(__name__)

# --- OpenAI modell és kliens -------------------------------------------------
OPENAI_MODEL = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip()
if OPENAI_MODEL.lower() == "gpt-40-mini":  # gyakori elütés (0 vs o)
    OPENAI_MODEL = "gpt-4o-mini"
logger.info(f"OPENAI_MODEL resolved to: {OPENAI_MODEL}")

# Távolítsd el az örökölt proxy-kat (httpx/OpenAI hibák elkerülése)
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"]:
    os.environ.pop(k, None)

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(timeout=60)
)

# --- L10N / tudásbázis -------------------------------------------------------
BASE = pathlib.Path(__file__).resolve().parents[1]
TXT_DIR = BASE / "media" / "knowledge_txt"

SYSTEM_PROMPT = """
Ești Dan — asistent tehnic & comercial pentru scule industriale (Unior Tepid).

Reguli FOARTE importante:
- Răspunzi DOAR pe baza fragmentelor din contextul furnizat mai jos.
- Dacă informația NU apare în context, spune explicit: „Nu am date în context pentru asta.” și cere clarificări/cuvinte-cheie.
- NU inventa produse, coduri, prețuri sau specificații.
- Când poți, propune 2–6 opțiuni RELEVANTE găsite în context, fiecare cu:
  - denumire scurtă,
  - preț (doar dacă apare EXPLICIT în context; menționează valuta; nu inventa),
  - cod (dacă apare),
  - 1–2 atribute cheie (dimensiune/standard/aplicație),
  - observație practică.
- Răspunde concis, în română, tip bullet-list; nu include alte surse decât contextul.
""".strip()


def index(request):
    return render(request, "assistant/index.html", {})


def _list_text_files():
    return [str(p) for p in TXT_DIR.glob("*.txt")]


CATALOG_KEYWORDS = re.compile(r"\b(preț|pret|price|cod|code)\b", re.IGNORECASE | re.UNICODE)
MIN_OVERLAP = 2  # legalább 2 közös token a kérdéssel


def _prefilter_local_snippets(txt_files, query: str, top_k: int = 40, window: int = 6) -> str:
    """
    Gyors, heurisztikus előszűrés:
    - token-átfedés alapján pontoz
    - CSAK olyan jelöltet enged át, ahol katalógus-jelek vannak (Preț/Price/Cod)
    - minimum átfedés küszöb (MIN_OVERLAP)
    """
    q_tokens = set(re.findall(r"[a-z0-9ăâîșţșț\-]+", (query or "").lower()))
    if not q_tokens:
        return ""

    candidates = []
    for path in txt_files:
        try:
            raw = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
        if len(paras) < 4:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            for i in range(0, len(lines), window):
                paras.append("\n".join(lines[i:i + window]))

        for p in paras:
            p_low = p.lower()
            p_tokens = set(re.findall(r"[a-z0-9ăâîșţșț\-]+", p_low))
            overlap = len(q_tokens & p_tokens)
            # csak ha van katalógus-jel ÉS elég az átfedés
            if overlap >= MIN_OVERLAP and CATALOG_KEYWORDS.search(p_low):
                # kis bonusz, ha 'preț/price' is van
                bonus = 1 if re.search(r"\b(preț|pret|price)\b", p_low) else 0
                score = overlap + bonus
                heapq.heappush(candidates, (score, p))
                if len(candidates) > top_k:
                    heapq.heappop(candidates)

    if not candidates:
        return ""

    top = [frag for _score, frag in sorted(candidates, key=lambda t: -t[0])]
    return "\n\n---\n\n".join(top)


@csrf_exempt
def ask(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    # --- Body parse (JSON + form) ---
    payload = {}
    try:
        if request.body:
            payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = {}

    user_text = (payload.get("message") or payload.get("text") or "").strip()
    if not user_text:
        user_text = (request.POST.get("message") or request.POST.get("text") or "").strip()

    if not user_text:
        return JsonResponse({"error": "empty message"}, status=400)

    # --- Tudásfájlok ellenőrzése ---
    txt_files = _list_text_files()
    if not txt_files:
        return JsonResponse(
            {"answer_html": "Nu am încă fișiere de cunoștințe. Rulează mai întâi <code>assistant/ingest.py</code>."},
            status=400,
        )

    pre_context = _prefilter_local_snippets(txt_files, user_text, top_k=40)

    # ha van pre_context, de nincs benne katalógus-jel, úgy tekintjük, hogy nem releváns
    if not (pre_context or "").strip() or not CATALOG_KEYWORDS.search(pre_context.lower()):
        return JsonResponse({
            "answer_html": (
                "Nu am găsit fragmente locale relevante pentru întrebarea ta.<br>"
                "Te rog verifică fișierele din <code>media/knowledge_txt</code> "
                "și folosește termeni apropiați de denumirile/codurile din catalog."
            )
        }, status=200)

    # --- OpenAI hívás csak user_text + context ---
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                            "Întrebare: " + user_text + "\n\n"
                                                        "Context din cataloage (fragmente relevante):\n"
                            + pre_context
                    ),
                },
            ],
        )
        answer_text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return JsonResponse(
            {"answer_html": f"Eroare server: <code>{type(e).__name__}: {e}</code>"},
            status=500,
        )

    if not answer_text:
        answer_text = (
            "Nu am găsit ceva clar în fragmentele din context. "
            "Îmi dai un cod sau o denumire mai precisă?"
        )

    return JsonResponse({"answer_html": answer_text})


# --- Gyors modell-teszt endpoint --------------------------------------------
@require_GET
def ping(request):
    try:
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
        return JsonResponse({
            "ok": True,
            "model": OPENAI_MODEL,
            "reply": r.choices[0].message.content
        })
    except Exception as e:
        logger.exception("Ping failed: %s", e)
        return JsonResponse({"ok": False, "model": OPENAI_MODEL, "error": str(e)}, status=500)


# --- Debug: láthatóak-e a TXT-k a szerveren? --------------------------------
@require_GET
def debug_knowledge(request):
    files = _list_text_files()
    out = []
    for p in files:
        try:
            txt = pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")
            head = txt[:400]
        except Exception as e:
            head = f"<< read error: {e} >>"
        out.append({"path": p, "head_len": len(head), "head": head})
    return JsonResponse({"count": len(files), "files": out})
