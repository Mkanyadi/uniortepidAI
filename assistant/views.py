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

Răspunde scurt, prietenos și practic, ca într-un chat cu un client. Folosește bullet-points când ajută.
Când poți, propune 2–6 opțiuni relevante, fiecare cu:
- denumire scurtă,
- preț (doar dacă apare explicit în fragmentele din context; nu inventa; menționează valuta),
- cod (dacă e clar din surse),
- 1–2 atribute cheie (dimensiune/standard/aplicație),
- observație practică (ex.: „bun pentru montaj pardoseli laminate”).

Dacă datele nu apar în fișierele mele, cere clarificări. Răspunde în română.
""".strip()


def index(request):
    return render(request, "assistant/index.html", {})


def _list_text_files():
    return [str(p) for p in TXT_DIR.glob("*.txt")]


def _prefilter_local_snippets(txt_files, query: str, top_k: int = 40, window: int = 6) -> str:
    """
    Citește rapid .txt-urile din media/knowledge_txt și întoarce fragmente relevante.
    Scor simplu după overlap de token-uri; folosește ferestre de linii dacă nu sunt paragrafe.
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
            p_tokens = set(re.findall(r"[a-z0-9ăâîșţșț\-]+", p.lower()))
            overlap = len(q_tokens & p_tokens)
            if overlap:
                heapq.heappush(candidates, (overlap, p))
                if len(candidates) > top_k:
                    heapq.heappop(candidates)

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
        # form fallback
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

    # --- OpenAI hívás ---
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
                        + (pre_context or "(nu am găsit fragmente locale)")
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
        answer_text = "Nu am găsit ceva clar în cataloagele încărcate. Îmi dai un cod sau o denumire mai precisă?"

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
