# assistant/views.py
import os
import json
import pathlib
import logging
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from dotenv import load_dotenv
from openai import OpenAI
import httpx

load_dotenv()
logger = logging.getLogger(__name__)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

BASE = pathlib.Path(__file__).resolve().parents[1]
TXT_DIR = BASE / "media" / "knowledge_txt"

# Oprește proxy-uri moștenite (evită erori cu httpx)
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"]:
    os.environ.pop(k, None)

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(timeout=60)  # fără proxy, timeout ok
)

# ——— Stil MyAIDrive prietenos ———
SYSTEM_PROMPT = """
Ești Dan — asistent tehnic & comercial pentru scule industriale (Unior Tepid).

Răspunde scurt, prietenos și practic, ca într-un chat cu un client. Folosește bullet-points când ajută.
Când poți, propune 2–6 opțiuni relevante, fiecare cu:
- denumire scurtă,
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
    Citește rapid .txt-urile din media/knowledge_txt și întoarce cele mai relevante
    fragmente (paragrafe) pentru întrebare, ca text brut concatenat.
    Simplu: scor pe baza overlap-ului de cuvinte.
    """
    import heapq
    import re

    q_tokens = set(re.findall(r"[a-z0-9ăâîșţșț\-]+", query.lower()))
    if not q_tokens:
        return ""

    candidates = []
    for path in txt_files:
        try:
            raw = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # împărțim în paragrafe; dacă textul e „liniaș”, facem ferestre de linii
        paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
        if len(paras) < 4:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            for i in range(0, len(lines), window):
                paras.append("\n".join(lines[i:i + window]))

        for p in paras:
            p_low = p.lower()
            p_tokens = set(re.findall(r"[a-z0-9ăâîșţșț\-]+", p_low))
            overlap = len(q_tokens & p_tokens)
            if overlap:
                # heap minim → păstrăm top_k cele mai mari scoruri
                heapq.heappush(candidates, (overlap, p))
                if len(candidates) > top_k:
                    heapq.heappop(candidates)

    # cele mai bune fragmente, ordonate descrescător
    top = [frag for _score, frag in sorted(candidates, key=lambda t: -t[0])]
    return "\n\n---\n\n".join(top)


@csrf_exempt
def ask(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = {}

    message = (data.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "empty message"}, status=400)

    # 1) verificăm cunoștințele locale
    txt_files = _list_text_files()
    if not txt_files:
        return JsonResponse(
            {"answer_html": "Nu am încă fișiere de cunoștințe. Rulează mai întâi <code>assistant/ingest.py</code>."},
            status=400,
        )

    # 2) pre-context local (fragmente relevante)
    pre_context = _prefilter_local_snippets(txt_files, message, top_k=40)

    # 3) apel Chat Completions (fără attachments/tools)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Întrebare: " + message + "\n\n"
                        "Context din cataloage (fragmente relevante):\n"
                        + (pre_context or "(nu am găsit fragmente locale)")
                    ),
                },
            ],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()

    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return JsonResponse(
            {"answer_html": f"Eroare server: <code>{type(e).__name__}: {e}</code>"},
            status=500,
        )

    if not text:
        text = "Nu am găsit ceva clar în cataloagele încărcate. Îmi dai un cod sau o denumire mai precisă?"

    return JsonResponse({"answer_html": text})
