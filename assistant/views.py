# assistant/views.py
import os
import re
import json
import heapq
import pathlib
import logging
import html

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
if OPENAI_MODEL.lower() == "gpt-40-mini":
    OPENAI_MODEL = "gpt-4o-mini"
logger.info(f"OPENAI_MODEL resolved to: {OPENAI_MODEL}")

# proxy env-k kiürítése
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"]:
    os.environ.pop(k, None)

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(timeout=60)
)

# --- Tudásbázis --------------------------------------------------------------
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

CATALOG_KEYWORDS = re.compile(r"\b(preț|pret|price|cod|code)\b", re.IGNORECASE | re.UNICODE)
MIN_OVERLAP = 2  # legalább 2 közös token

def index(request):
    return render(request, "assistant/index.html", {})

def _list_text_files():
    return [str(p) for p in TXT_DIR.glob("*.txt")]

def _prefilter_local_snippets(txt_files, query: str, top_k: int = 40, window: int = 6) -> str:
    """
    Heurisztikus előszűrés:
    - token-átfedés alapján pontoz
    - CSAK olyan jelöltet enged át, ahol katalógus-jel (Preț/Price/Cod) van
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

            if overlap >= MIN_OVERLAP and CATALOG_KEYWORDS.search(p_low):
                bonus = 1 if re.search(r"\b(preț|pret|price)\b", p_low) else 0
                score = overlap + bonus
                heapq.heappush(candidates, (score, p))
                if len(candidates) > top_k:
                    heapq.heappop(candidates)

    if not candidates:
        return ""

    top = [frag for _score, frag in sorted(candidates, key=lambda t: -t[0])]
    return "\n\n---\n\n".join(top)

# --- Automatikus kinyerés kontextusból ---------------------------------------
def _extract_catalog_entries(pre_context: str) -> str:
    """
    Kinyer Denumire/Preț/Cod mezőket a kontextusból.
    Ha a Preț mellett nincs valuta, automatikusan RON-t teszünk.
    Visszatér: formázott HTML (vagy üres string, ha nincs találat).
    """
    if not pre_context:
        return ""

    entries = []
    blocks = re.split(r"\n\s*\n+", pre_context)

    for block in blocks:
        if not re.search(r"(preț|pret|price|cod|code)", block, re.I):
            continue

        # név – sok katalógusban nincs explicit "Denumire:", ezért az első sorból próbálunk
        first_line = block.strip().splitlines()[0].strip() if block.strip() else ""
        name_match = re.search(r"(?:Denumire|Produs|Articol|Lamp[ăa]|Lantern[ăa])[:\-]?\s*(.+)", block, re.I)
        name = (name_match.group(1).strip() if name_match else first_line) or "(fără denumire)"

        price_match = re.search(r"(?:Preț|Pret|Price)[:\-]?\s*([0-9][0-9\.\, ]*)", block, re.I)
        code_match  = re.search(r"(?:Cod|Code)[:\-]?\s*([A-Za-z0-9\-/]+)", block, re.I)

        price = price_match.group(1).strip() if price_match else None
        code  = code_match.group(1).strip() if code_match else None

        if not (name or price or code):
            continue

        # valuta pótlás
        if price and not re.search(r"\b(RON|EUR|USD)\b", price, re.I):
            price = price + " RON"

        entries.append({"name": name, "price": price, "code": code})

    if not entries:
        return ""

    parts = []
    for i, e in enumerate(entries, 1):
        row = []
        row.append(f"<b>{i}. {html.escape(e['name'])}</b>")
        if e["price"]:
            row.append(f"Preț: {html.escape(e['price'])}")
        if e["code"]:
            row.append(f"Cod: {html.escape(e['code'])}")
        parts.append("<br>".join(row))
    return "<div class='catalog-results'>" + "<hr>".join(parts) + "</div>"

# --- API ---------------------------------------------------------------------
@csrf_exempt
def ask(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    # body parse (JSON + form)
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

    # tudásfájlok
    txt_files = _list_text_files()
    if not txt_files:
        return JsonResponse(
            {"answer_html": "Nu am încă fișiere de cunoștințe. Rulează mai întâi <code>assistant/ingest.py</code>."},
            status=400,
        )

    pre_context = _prefilter_local_snippets(txt_files, user_text, top_k=40)

    # hard gate – kontextus nélkül nem kérdezünk
    if not (pre_context or "").strip() or not CATALOG_KEYWORDS.search(pre_context.lower()):
        return JsonResponse({
            "answer_html": (
                "Nu am găsit fragmente locale relevante pentru întrebarea ta.<br>"
                "Te rog verifică fișierele din <code>media/knowledge_txt</code> "
                "și folosește termeni apropiați de denumirile/codurile din catalog."
            )
        }, status=200)

    # 1) próbáljuk saját kinyeréssel (stabil, RON hozzáadása)
    auto_html = _extract_catalog_entries(pre_context)
    if auto_html:
        return JsonResponse({"answer_html": auto_html})

    # 2) ha nincs strukturált találat, mehet a modell (csak a kontextussal)
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

# --- Gyors modell-teszt ------------------------------------------------------
@require_GET
def ping(request):
    try:
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
        return JsonResponse({"ok": True, "model": OPENAI_MODEL, "reply": r.choices[0].message.content})
    except Exception as e:
        logger.exception("Ping failed: %s", e)
        return JsonResponse({"ok": False, "model": OPENAI_MODEL, "error": str(e)}, status=500)

# --- Debug: látja-e a szerver a TXT-ket? ------------------------------------
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
