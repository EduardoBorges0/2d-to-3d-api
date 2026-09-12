"""Servidor HTTP (FastAPI) que corre DENTRO do pod RunPod — carrega o TRELLIS
uma vez e fica a ouvir pedidos de geração enquanto o pod estiver ligado.

Diferente de runpod/handler.py (removido): aquele era para RunPod Serverless
(1 handler por pedido, gerido pelo SDK `runpod`); isto é um pod "normal" que
processa vários pedidos seguidos na mesma sessão de lote — ver
pipeline/runpod_pod_client.py (PodSession) do lado de quem chama.

Contrato dos endpoints (tem de ficar em sincronia com runpod_pod_client.py):
    GET  /health              -> 200 assim que o TRELLIS estiver carregado
    POST /generate            -> {images_b64, quality, seed} -> {glb_b64}
"""

import base64
import io

from fastapi import FastAPI
from pydantic import BaseModel
from PIL import Image

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


class GenerateRequest(BaseModel):
    images_b64: list[str]
    quality: str = "quality"
    seed: int = 1


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/generate")
def generate(body: GenerateRequest):
    if body.quality not in QUALITY_PRESETS:
        return {"error": f"quality inválida: {body.quality!r} (esperado 'fast' ou 'quality')"}

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
    return {"glb_b64": base64.b64encode(buf.getvalue()).decode("ascii")}
