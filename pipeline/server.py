"""
Servidor local do catálogo (FastAPI): serve o visualizador estático (catalog/)
e expõe a API para "Adicionar peça" — recebe fotos + metadados, guarda-as em
photos_input/<part-id>/ e cria uma entrada "a_processar" no parts.json.

Propositadamente NÃO liga um pod RunPod por upload: a peça fica em fila
("a_processar") e é processada mais tarde, em lote, por
POST /api/parts/process-pending (botão "Processar pendentes" no site) ou por
`python pipeline/trellis_pipeline.py --pending` (ex: numa tarefa agendada
1x/dia). É assim que se aproveita o custo baixo do RunPod Community Cloud —
1 sessão de pod para várias peças em vez de 1 por peça (ver runpod/README.md).

Uso:
    python pipeline/server.py
    (abre em http://localhost:8791)
"""

import os
import re
import sys
import threading
import traceback
import unicodedata
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edit_pipeline  # noqa: E402
import runpod_pod_client as rpc  # noqa: E402
import trellis_pipeline as tp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "catalog"
PHOTOS_DIR = REPO_ROOT / "photos_input"

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="Almovi - Catálogo 3D de Peças")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "peca"


def unique_part_id(base: str) -> str:
    catalog = tp.load_catalog()
    existing_ids = {p["id"] for p in catalog.get("parts", [])}
    if base not in existing_ids:
        return base
    i = 2
    while f"{base}-{i}" in existing_ids:
        i += 1
    return f"{base}-{i}"


# Impede duas sessões de pod em simultâneo (custo dobrado + condição de
# corrida a escrever no mesmo parts.json) — só um lote de cada vez.
_batch_lock = threading.Lock()
_batch_running = False


def run_batch_job(quality: str):
    """Liga UMA sessão de pod RunPod, processa todas as peças 'a_processar',
    desliga o pod. Corre numa thread de fundo para não bloquear o servidor
    (pode demorar minutos: arranque do pod + geração de cada peça)."""
    global _batch_running
    try:
        with rpc.PodSession() as session:
            sucesso, falhas = tp.process_pending(session, quality=quality, seed=1)
        print(f"Lote concluído: {len(sucesso)} com sucesso, {len(falhas)} com erro.")
    except Exception as e:
        # A sessão nem chegou a arrancar (ex: sem RUNPOD_API_KEY, sem
        # capacidade Community Cloud) — process_pending nunca correu, por
        # isso nenhuma peça ficou marcada em erro sozinha. Sem isto, as peças
        # ficavam presas em "a_processar" para sempre, com o erro real só
        # visível no terminal do servidor.
        traceback.print_exc()
        erro_msg = f"{type(e).__name__}: {e}"
        catalog = tp.load_catalog()
        pendentes = [p["id"] for p in catalog.get("parts", []) if p.get("estado") == "a_processar"]
        for part_id in pendentes:
            tp.update_part_fields(part_id, estado="erro", erro=erro_msg)
        print(f"Lote falhou antes de processar peças (pod não arrancou?): {e}", file=sys.stderr)
    finally:
        _batch_running = False


@app.get("/api/health")
def health():
    tem_runpod = bool(os.environ.get("RUNPOD_API_KEY") and os.environ.get("RUNPOD_IMAGE"))
    return {"ok": True, "runpod_configurado": tem_runpod}


@app.post("/api/parts")
async def create_part(
    nome: str = Form(...),
    referencia: str = Form(""),
    categoria: str = Form(""),
    equipamento: str = Form(""),
    descricao: str = Form(""),
    quality: str = Form("quality"),
    fotos: list[UploadFile] = File(...),
):
    nome = nome.strip()
    if not nome:
        return JSONResponse({"errors": {"nome": "Nome é obrigatório."}}, status_code=400)
    if quality not in ("fast", "quality"):
        quality = "quality"

    fotos = [f for f in fotos if f and f.filename]
    if not fotos:
        return JSONResponse({"errors": {"fotos": "Pelo menos uma foto é obrigatória."}}, status_code=400)
    for f in fotos:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return JSONResponse(
                {"errors": {"fotos": f"Formato não suportado: {f.filename}"}}, status_code=400
            )

    base_slug = slugify(referencia or nome)
    part_id = unique_part_id(base_slug)

    input_dir = PHOTOS_DIR / part_id
    input_dir.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(fotos, start=1):
        ext = Path(f.filename).suffix.lower()
        dest = input_dir / f"foto{i}{ext}"
        dest.write_bytes(await f.read())

    catalog = tp.load_catalog()
    tp.upsert_part(
        catalog,
        part_id,
        glb_relpath=None,
        meta={
            "nome": nome,
            "categoria": categoria,
            "equipamento": equipamento,
            "referencia": referencia or part_id.upper(),
            "descricao": descricao,
        },
        origem_fotos=[str(p.relative_to(REPO_ROOT)) for p in sorted(input_dir.iterdir())],
        estado="a_processar",
        quality=quality,
    )
    tp.save_catalog(catalog)

    # Não processa já — fica em fila até ao próximo lote (ver
    # /api/parts/process-pending e o cabeçalho deste ficheiro).
    return JSONResponse({"id": part_id, "status": "a_processar"}, status_code=202)


@app.post("/api/parts/process-pending")
def process_pending():
    global _batch_running
    with _batch_lock:
        if _batch_running:
            return JSONResponse({"error": "Já há um lote a processar."}, status_code=409)
        catalog = tp.load_catalog()
        n_pendentes = sum(1 for p in catalog.get("parts", []) if p.get("estado") == "a_processar")
        if n_pendentes == 0:
            return JSONResponse({"error": "Não há peças pendentes."}, status_code=400)
        _batch_running = True

    thread = threading.Thread(target=run_batch_job, args=("quality",), daemon=True)
    thread.start()

    return JSONResponse({"status": "a_processar", "n_pendentes": n_pendentes}, status_code=202)


class EditRequest(BaseModel):
    instrucao: str


def run_edit_job(part_id: str, instrucao: str):
    """Corre numa thread separada, mesmo padrão de run_batch_job — a edição
    (booleana + chamada ao LLM) não precisa de GPU mas ainda demora alguns
    segundos e não deve bloquear o event loop."""
    try:
        novo_glb_relpath = edit_pipeline.edit_part(part_id, instrucao)
        tp.record_edit(part_id, instrucao, novo_glb_relpath, estado="sucesso")
        print(f"[{part_id}] edição aplicada -> {novo_glb_relpath}")
    except edit_pipeline.EditError as e:
        tp.record_edit(part_id, instrucao, None, estado="erro", erro=str(e))
        print(f"[{part_id}] edição rejeitada: {e}", file=sys.stderr)
    except Exception as e:
        traceback.print_exc()
        tp.record_edit(part_id, instrucao, None, estado="erro", erro=f"{type(e).__name__}: {e}")
        print(f"[{part_id}] edição falhou: {e}", file=sys.stderr)


@app.post("/api/parts/{part_id}/edit")
def edit_part(part_id: str, body: EditRequest):
    catalog = tp.load_catalog()
    entry = next((p for p in catalog.get("parts", []) if p["id"] == part_id), None)
    if entry is None:
        return JSONResponse({"error": f"Peça '{part_id}' não encontrada."}, status_code=404)
    if not entry.get("modelo_glb"):
        return JSONResponse({"error": "Peça ainda não tem modelo 3D gerado."}, status_code=400)
    if not body.instrucao.strip():
        return JSONResponse({"error": "Instrução vazia."}, status_code=400)

    tp.update_part_fields(part_id, estado="a_editar")

    thread = threading.Thread(target=run_edit_job, args=(part_id, body.instrucao.strip()), daemon=True)
    thread.start()

    return JSONResponse({"id": part_id, "status": "a_editar"}, status_code=202)


# Montado por último: serve catalog/ (index.html, app.js, data/parts.json,
# models/*.glb) em "/" sem tapar as rotas /api/* definidas acima.
app.mount("/", StaticFiles(directory=str(CATALOG_DIR), html=True), name="catalog")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8791)
