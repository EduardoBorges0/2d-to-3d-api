"""Cliente para RunPod Pods (não Serverless) — liga um pod em Community Cloud,
usa-o para gerar N peças seguidas na mesma sessão, desliga-o no fim.

Porquê Pods e não Serverless: para o volume deste catálogo (poucas peças/dia,
processadas em lote), Community Cloud é ~4x mais barato por hora do que
Serverless, e pagar só o tempo de uma sessão de lote (minutos) em vez de por
pedido individual evita repetir o custo de arranque a cada peça.

Endpoints usados (RunPod REST API, https://docs.runpod.io/api-reference):
    POST   /v1/pods                 -> criar/alugar o pod
    GET    /v1/pods/{id}            -> estado (portMappings, publicIp)
    DELETE /v1/pods/{id}            -> terminar

O pod corre a imagem de runpod/Dockerfile, que expõe runpod/pod_server.py
(FastAPI) na porta RUNPOD_PORT com `/health` e `/generate`. Acede-se via o
proxy HTTP do RunPod: https://{pod_id}-{port}.proxy.runpod.net — este URL
funciona da mesma forma em Community e Secure Cloud.

Configuração via ambiente (ver .env.example):
    RUNPOD_API_KEY          — chave de API da conta RunPod
    RUNPOD_IMAGE             — imagem Docker publicada (runpod/README.md)
    RUNPOD_GPU_TYPE_IDS      — lista separada por vírgulas, por ordem de preferência
                               (default: GPU_TYPE_IDS_DEFAULT abaixo)
    RUNPOD_CLOUD_TYPE        — "COMMUNITY" (default, mais barato) ou "SECURE"
    RUNPOD_CONTAINER_DISK_GB — default 40
"""

import os
import time

import requests

REST_API_BASE = "https://rest.runpod.io/v1"
POD_PORT = 8000

# GPUs com pelo menos 16GB VRAM, da mais barata (Community Cloud) para a mais
# cara — o RunPod tenta por ordem e usa a primeira com capacidade disponível.
GPU_TYPE_IDS_DEFAULT = [
    "NVIDIA RTX A4000",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 4090",
]

# Sessão de lote: arranque do pod + carregamento do TRELLIS para a GPU pode
# demorar vários minutos, sobretudo em Community Cloud (agendamento da
# máquina) — timeouts generosos.
POD_READY_TIMEOUT_S = 10 * 60
GENERATE_TIMEOUT_S = 5 * 60


class RunPodConfigError(RuntimeError):
    """RUNPOD_API_KEY / RUNPOD_IMAGE em falta no ambiente."""


class PodError(RuntimeError):
    """Falha a criar, arrancar ou usar o pod (ex: sem capacidade Community Cloud)."""


def _config():
    api_key = os.environ.get("RUNPOD_API_KEY")
    image = os.environ.get("RUNPOD_IMAGE")
    if not api_key or not image:
        raise RunPodConfigError(
            "RUNPOD_API_KEY e/ou RUNPOD_IMAGE não definidos no ambiente "
            "(ver .env.example e runpod/README.md)."
        )
    return api_key, image


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _gpu_type_ids():
    raw = os.environ.get("RUNPOD_GPU_TYPE_IDS")
    if raw:
        return [g.strip() for g in raw.split(",") if g.strip()]
    return GPU_TYPE_IDS_DEFAULT


def create_pod() -> str:
    api_key, image = _config()
    payload = {
        "name": "2d-to-3d-trellis-batch",
        "imageName": image,
        "cloudType": os.environ.get("RUNPOD_CLOUD_TYPE", "COMMUNITY"),
        "computeType": "GPU",
        "gpuTypeIds": _gpu_type_ids(),
        "gpuCount": 1,
        "containerDiskInGb": int(os.environ.get("RUNPOD_CONTAINER_DISK_GB", 40)),
        "ports": [f"{POD_PORT}/http"],
    }
    resp = requests.post(f"{REST_API_BASE}/pods", headers=_headers(api_key), json=payload, timeout=30)
    if resp.status_code >= 400:
        raise PodError(f"Falha ao criar pod (HTTP {resp.status_code}): {resp.text}")
    return resp.json()["id"]


def get_pod(pod_id: str) -> dict:
    api_key, _ = _config()
    resp = requests.get(f"{REST_API_BASE}/pods/{pod_id}", headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def terminate_pod(pod_id: str):
    api_key, _ = _config()
    requests.delete(f"{REST_API_BASE}/pods/{pod_id}", headers=_headers(api_key), timeout=30)


def wait_until_ready(pod_id: str, timeout_s: int = POD_READY_TIMEOUT_S) -> str:
    """Espera o pod arrancar E o servidor TRELLIS lá dentro responder a /health.
    Devolve o URL proxy (https://{pod_id}-{port}.proxy.runpod.net)."""
    deadline = time.monotonic() + timeout_s
    proxy_url = f"https://{pod_id}-{POD_PORT}.proxy.runpod.net"

    # 1) esperar o RunPod mapear a porta (pod agendado + container arrancado)
    while True:
        pod = get_pod(pod_id)
        if pod.get("portMappings"):
            break
        if time.monotonic() > deadline:
            raise PodError(f"Pod {pod_id} não ficou pronto em {timeout_s}s (sem capacidade disponível?)")
        time.sleep(5)

    # 2) esperar o handler FastAPI + TRELLIS (carregamento do modelo) responder
    while True:
        try:
            r = requests.get(f"{proxy_url}/health", timeout=10)
            if r.status_code == 200:
                return proxy_url
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise PodError(f"Servidor TRELLIS no pod {pod_id} não respondeu em {timeout_s}s")
        time.sleep(5)


def generate(proxy_url: str, images_b64: list[str], quality: str, seed: int) -> bytes:
    resp = requests.post(
        f"{proxy_url}/generate",
        json={"images_b64": images_b64, "quality": quality, "seed": seed},
        timeout=GENERATE_TIMEOUT_S,
    )
    resp.raise_for_status()
    output = resp.json()
    if "error" in output:
        raise PodError(f"Worker RunPod devolveu erro: {output['error']}")
    import base64
    return base64.b64decode(output["glb_b64"])


class PodSession:
    """Context manager: liga um pod, dá acesso a .generate(...) para 1+ peças,
    termina o pod no fim (mesmo em caso de erro) — é o padrão "1 sessão de lote"."""

    def __init__(self):
        self.pod_id = None
        self.proxy_url = None

    def __enter__(self):
        self.pod_id = create_pod()
        try:
            self.proxy_url = wait_until_ready(self.pod_id)
        except Exception:
            terminate_pod(self.pod_id)
            raise
        return self

    def generate(self, images_b64: list[str], quality: str, seed: int) -> bytes:
        return generate(self.proxy_url, images_b64, quality, seed)

    def __exit__(self, exc_type, exc, tb):
        if self.pod_id:
            terminate_pod(self.pod_id)
        return False
