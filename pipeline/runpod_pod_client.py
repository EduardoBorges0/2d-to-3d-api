"""Cliente para RunPod Pods (não Serverless) — um pod PERSISTENTE (criado uma
vez, depois só Start/Stop) usado para gerar N peças por sessão.

Porquê pod persistente em vez de criar/destruir a cada sessão: o disco do
container (onde já está a nossa imagem de 12GB) não é cobrado enquanto o pod
está parado — só a GPU pára de custar. Manter o mesmo pod e só pará-lo/
retomá-lo evita repetir o "cold start" de puxar a imagem inteira a cada
sessão (o que acontecia sempre que criávamos/destruíamos um pod novo, porque
cada sessão calhava numa máquina física diferente sem a imagem em cache).

Custo do disco parado: ~$0.20/GB/mês de volume (não confundir com o
container disk, esse é grátis parado) — no nosso caso ~20GB ≈ $4/mês, mesmo
sem gerar nada.

Endpoints usados (RunPod REST API, https://docs.runpod.io/api-reference):
    POST   /v1/pods                 -> criar o pod (1ª vez apenas)
    POST   /v1/pods/{id}/start      -> retomar (resume) um pod parado
    POST   /v1/pods/{id}/stop       -> parar (sem apagar disco/imagem)
    GET    /v1/pods/{id}            -> estado (portMappings, publicIp)
    DELETE /v1/pods/{id}            -> apagar definitivamente (não usado por
                                        omissão — só se o utilizador quiser
                                        mesmo deixar de ter o pod)

O pod corre a imagem de runpod/Dockerfile, que expõe runpod/pod_server.py
(FastAPI) na porta RUNPOD_PORT com `/health` e `/generate`. Acede-se via o
proxy HTTP do RunPod: https://{pod_id}-{port}.proxy.runpod.net — este URL
funciona da mesma forma em Community e Secure Cloud.

Configuração via ambiente (ver .env.example):
    RUNPOD_API_KEY          — chave de API da conta RunPod
    RUNPOD_IMAGE             — imagem Docker publicada (runpod/README.md)
    RUNPOD_POD_ID            — id do pod persistente; vazio na 1ª vez (o
                               PodSession cria-o e grava-o automaticamente no
                               .env), preenchido nas vezes seguintes
    RUNPOD_GPU_TYPE_IDS      — lista separada por vírgulas, por ordem de preferência
                               (default: GPU_TYPE_IDS_DEFAULT abaixo)
    RUNPOD_CLOUD_TYPE        — "COMMUNITY" (default, mais barato) ou "SECURE"
    RUNPOD_CONTAINER_DISK_GB — default 40
"""

import os
import re
import time
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

REST_API_BASE = "https://rest.runpod.io/v1"
POD_PORT = 8000

# GPUs com pelo menos 16GB VRAM, da mais barata (Community Cloud) para a mais
# cara — o RunPod tenta por ordem e usa a primeira com capacidade disponível.
GPU_TYPE_IDS_DEFAULT = [
    "NVIDIA RTX A4000",
    "NVIDIA RTX A4500",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA GeForce RTX 4080 SUPER",
    "NVIDIA GeForce RTX 4080",
    "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA L4",
    "NVIDIA RTX A6000",
    "NVIDIA A40",
    "NVIDIA RTX 5000 Ada Generation",
    "NVIDIA L40",
    "NVIDIA L40S",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA A100-SXM4-40GB",
    "NVIDIA A100 80GB PCIe",
    "NVIDIA A100-SXM4-80GB",
]
# Todas Ampere/Ada (compute 8.0/8.6/8.9) — compatíveis com o
# TORCH_CUDA_ARCH_LIST fixado no Dockerfile sem precisar de mudar nada. Não
# inclui Hopper/Blackwell (H100/H200/B200/B300): precisariam de mais
# arquiteturas no TORCH_CUDA_ARCH_LIST e, no caso do Blackwell, de uma
# imagem base CUDA mais recente que a 12.1 (não suporta compute_100).

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
        "containerDiskInGb": int(os.environ.get("RUNPOD_CONTAINER_DISK_GB", 35)),
        # Sem isto, o RunPod anexa por omissão um volume persistente de 20GB
        # montado em /workspace — exatamente onde a imagem tem o TRELLIS e o
        # pod_server.py copiados (runpod/Dockerfile) — e esse volume TAPA o
        # conteúdo da imagem nesse caminho (fica vazio na 1ª vez). Confirmado
        # num pod real: "Could not import module pod_server" mesmo a imagem
        # estando correta — o volumeInGb/volumeMountPath do pod (via GET
        # /pods/{id}) mostrava 20GB em /workspace sem nunca o termos pedido.
        # Não precisamos de nenhum volume persistente (nada a guardar entre
        # sessões dentro do pod) — 0 desativa-o.
        "volumeInGb": 0,
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
    """Apaga o pod definitivamente (perde a imagem em cache no disco). Não é
    chamado automaticamente pelo PodSession — só para quem quiser mesmo
    deixar de ter o pod persistente (ex: CLI --forget-pod, ou à mão)."""
    api_key, _ = _config()
    requests.delete(f"{REST_API_BASE}/pods/{pod_id}", headers=_headers(api_key), timeout=30)


def start_pod(pod_id: str):
    api_key, _ = _config()
    resp = requests.post(f"{REST_API_BASE}/pods/{pod_id}/start", headers=_headers(api_key), timeout=30)
    if resp.status_code >= 400:
        raise PodError(f"Falha ao retomar pod {pod_id} (HTTP {resp.status_code}): {resp.text}")


def stop_pod(pod_id: str):
    api_key, _ = _config()
    requests.post(f"{REST_API_BASE}/pods/{pod_id}/stop", headers=_headers(api_key), timeout=30)


def _save_pod_id_to_env(pod_id: str):
    """Grava RUNPOD_POD_ID no .env automaticamente, para a próxima sessão
    reutilizar o mesmo pod sem precisar de o criar de novo. Só acontece na
    1ª vez (quando ainda não há RUNPOD_POD_ID no ambiente)."""
    if not ENV_PATH.exists():
        return
    text = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"^RUNPOD_POD_ID=.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^RUNPOD_POD_ID=.*$", f"RUNPOD_POD_ID={pod_id}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\nRUNPOD_POD_ID={pod_id}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    os.environ["RUNPOD_POD_ID"] = pod_id


def wait_until_ready(pod_id: str, timeout_s: int = POD_READY_TIMEOUT_S) -> str:
    """Espera o pod arrancar E o servidor TRELLIS lá dentro responder a /health.
    Devolve o URL proxy (https://{pod_id}-{port}.proxy.runpod.net)."""
    deadline = time.monotonic() + timeout_s
    proxy_url = f"https://{pod_id}-{POD_PORT}.proxy.runpod.net"

    # Só há um sinal fiável de "pronto": o /health responder 200 — é isso que
    # realmente nos interessa (pod agendado + container arrancado + TRELLIS
    # carregado). Antes havia um passo prévio que esperava por
    # pod.get("portMappings") no GET /pods/{id}; confirmado num pod real
    # (Secure Cloud) que esse campo nunca ficava preenchido mesmo com o
    # servidor já a responder normalmente (logs do próprio pod mostravam
    # "TRELLIS carregado, pod pronto para gerar" e pedidos HTTP a serem
    # servidos) — o script desistia e apagava um pod perfeitamente saudável.
    #
    # Tolera falhas de rede (locais ou o proxy ainda não estar disponível
    # porque o pod ainda não arrancou) — só desiste ao fim do timeout.
    while True:
        try:
            r = requests.get(f"{proxy_url}/health", timeout=10)
            if r.status_code == 200:
                return proxy_url
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise PodError(f"Pod {pod_id} não respondeu em /health dentro de {timeout_s}s (sem capacidade disponível, ou falha no arranque do servidor)")
        time.sleep(5)


def generate(proxy_url: str, images_b64: list[str], quality: str, seed: int) -> bytes:
    """Submete a geração e vai perguntando pelo resultado (padrão
    submeter+perguntar) — uma geração pode demorar mais que o timeout do
    proxy HTTP do RunPod (~100s, confirmado na prática com um 524), por isso
    nenhum pedido individual pode ficar à espera do resultado completo."""
    resp = requests.post(
        f"{proxy_url}/generate",
        json={"images_b64": images_b64, "quality": quality, "seed": seed},
        timeout=30,
    )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + GENERATE_TIMEOUT_S
    while True:
        r = requests.get(f"{proxy_url}/generate/{job_id}", timeout=30)
        r.raise_for_status()
        job = r.json()

        if job["status"] == "done":
            import base64
            return base64.b64decode(job["glb_b64"])
        if job["status"] == "error":
            raise PodError(f"Worker RunPod devolveu erro: {job['error']}")

        if time.monotonic() > deadline:
            raise PodError(f"Geração (job {job_id}) não terminou em {GENERATE_TIMEOUT_S}s")
        time.sleep(5)


class PodSession:
    """Context manager para o pod PERSISTENTE: na 1ª vez cria-o (e grava o id
    em RUNPOD_POD_ID no .env); nas vezes seguintes só o retoma (Start). Dá
    acesso a .generate(...) para 1+ peças, e PÁRA o pod no fim (Stop, não
    Terminate — mantém o disco/imagem para a próxima sessão arrancar mais
    depressa)."""

    def __init__(self):
        self.pod_id = os.environ.get("RUNPOD_POD_ID") or None
        self.proxy_url = None
        self._criado_agora = False

    def __enter__(self):
        if self.pod_id is None:
            self.pod_id = create_pod()
            self._criado_agora = True
            _save_pod_id_to_env(self.pod_id)
            print(f"[runpod] pod persistente criado: {self.pod_id} (gravado em .env como RUNPOD_POD_ID)")
        else:
            try:
                start_pod(self.pod_id)
            except PodError:
                # RUNPOD_POD_ID no .env aponta para um pod que já não existe
                # (ex: apagado à mão na consola) — cria um novo em vez de
                # falhar para sempre com o mesmo id inválido.
                print(f"[runpod] pod guardado ({self.pod_id}) já não existe — a criar um novo")
                self.pod_id = create_pod()
                self._criado_agora = True
                _save_pod_id_to_env(self.pod_id)

        try:
            self.proxy_url = wait_until_ready(self.pod_id)
        except Exception:
            # Nunca deixa o pod ligado a gastar depois de uma falha: pára-o
            # sempre; se foi criado agora mesmo (nunca chegou a ficar
            # utilizável), termina-o também — não vale a pena manter o disco
            # de um pod que nunca funcionou.
            stop_pod(self.pod_id)
            if self._criado_agora:
                terminate_pod(self.pod_id)
            raise
        return self

    def generate(self, images_b64: list[str], quality: str, seed: int) -> bytes:
        return generate(self.proxy_url, images_b64, quality, seed)

    def __exit__(self, exc_type, exc, tb):
        if self.pod_id:
            stop_pod(self.pod_id)
        return False
