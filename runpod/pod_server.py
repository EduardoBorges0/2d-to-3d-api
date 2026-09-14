"""Servidor HTTP (FastAPI) que corre DENTRO do pod RunPod — carrega o TRELLIS
uma vez e fica a ouvir pedidos de geração enquanto o pod estiver ligado.

Diferente de runpod/handler.py (removido): aquele era para RunPod Serverless
(1 handler por pedido, gerido pelo SDK `runpod`); isto é um pod "normal" que
processa vários pedidos seguidos na mesma sessão de lote — ver
pipeline/runpod_pod_client.py (PodSession) do lado de quem chama.

Contrato dos endpoints (tem de ficar em sincronia com runpod_pod_client.py):
    GET  /health              -> 200 assim que o TRELLIS estiver carregado
    POST /generate            -> {images_b64, quality, seed} -> {job_id}
    GET  /generate/{job_id}   -> {status: pending|done|error, glb_b64?, error?}

POST /generate devolve logo um job_id (não espera pela geração terminar) —
uma única geração pode demorar mais que o timeout do proxy HTTP do RunPod
(~100s, é o Cloudflare por trás do proxy.runpod.net; confirmado na prática
com um 524 numa geração real). Ao devolver logo e o cliente ir perguntando
pelo estado, nenhum pedido HTTP individual fica à espera tempo nenhum.
"""

import base64
import io
import os
import subprocess
import threading
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image

# Diagnóstico: já tivemos "CUDA unknown error" (torch.cuda / warp) em 4 pods
# diferentes seguidos (2 versões de imagem CUDA diferentes, hosts diferentes).
# Antes de sequer tentar carregar o TRELLIS, confirma se o GPU está visível ao
# container ao nível do SO — se nvidia-smi já falhar aqui, o problema é o
# RunPod não estar a expor o GPU a este container (fora do nosso controlo no
# Dockerfile/código), não a nossa imagem.
print("[diag] NVIDIA_VISIBLE_DEVICES=", os.environ.get("NVIDIA_VISIBLE_DEVICES"))
print("[diag] CUDA_VISIBLE_DEVICES=", os.environ.get("CUDA_VISIBLE_DEVICES"))
try:
    out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=30)
    print("[diag] nvidia-smi exit code:", out.returncode)
    print("[diag] nvidia-smi stdout:\n", out.stdout)
    print("[diag] nvidia-smi stderr:\n", out.stderr)
except Exception as e:
    print(f"[diag] nvidia-smi falhou ao correr: {type(e).__name__}: {e}")

from trellis.pipelines import TrellisImageTo3DPipeline
from trellis.utils import postprocessing_utils

QUALITY_PRESETS = {
    "fast": {
        "sparse_structure_sampler_params": {"steps": 12, "cfg_strength": 7.5},
        "slat_sampler_params": {"steps": 12, "cfg_strength": 3.0},
        "simplify": 0.95,
        "texture_size": 1024,
    },
    "quality": {
        "sparse_structure_sampler_params": {"steps": 25, "cfg_strength": 7.5},
        "slat_sampler_params": {"steps": 25, "cfg_strength": 3.5},
        "simplify": 0.9,
        "texture_size": 2048,
    },
}

app = FastAPI()

print("A carregar TRELLIS-image-large...")
PIPELINE = TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")
PIPELINE.cuda()
print("TRELLIS carregado, pod pronto para gerar.")

# job_id -> {"status": "pending"|"done"|"error", "glb_b64": str|None, "error": str|None}
# Sessão de lote é curta (o pod desliga-se no fim) — não vale a pena limpar
# jobs antigos, a memória usada é desprezável (só strings de estado).
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


class GenerateRequest(BaseModel):
    images_b64: list[str]
    quality: str = "quality"
    seed: int = 1


@app.get("/health")
def health():
    return {"ok": True}


def _run_generation(job_id: str, body: GenerateRequest):
    try:
        preset = QUALITY_PRESETS[body.quality]
        images = [Image.open(io.BytesIO(base64.b64decode(b))) for b in body.images_b64]

        common_kwargs = dict(
            seed=body.seed,
            sparse_structure_sampler_params=preset["sparse_structure_sampler_params"],
            slat_sampler_params=preset["slat_sampler_params"],
        )

        if len(images) == 1:
            outputs = PIPELINE.run(images[0], **common_kwargs)
        else:
            outputs = PIPELINE.run_multi_image(images, **common_kwargs)

        glb = postprocessing_utils.to_glb(
            outputs["gaussian"][0],
            outputs["mesh"][0],
            simplify=preset["simplify"],
            texture_size=preset["texture_size"],
        )

        buf = io.BytesIO()
        glb.export(buf, file_type="glb")
        glb_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "glb_b64": glb_b64, "error": None}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "glb_b64": None, "error": f"{type(e).__name__}: {e}"}


@app.post("/generate")
def generate(body: GenerateRequest):
    if body.quality not in QUALITY_PRESETS:
        return JSONResponse(
            {"error": f"quality inválida: {body.quality!r} (esperado 'fast' ou 'quality')"},
            status_code=400,
        )

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "pending", "glb_b64": None, "error": None}

    thread = threading.Thread(target=_run_generation, args=(job_id, body), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/generate/{job_id}")
def generate_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "job desconhecido"}, status_code=404)
    return job
