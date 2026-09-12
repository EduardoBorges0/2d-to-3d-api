# syntax=docker/dockerfile:1.7
# (necessário para RUN --mount=type=secret, usado mais abaixo para o HF_TOKEN)
#
# Imagem do pod RunPod (Community Cloud) que corre o TRELLIS (microsoft/TRELLIS).
#
# Este NÃO é o padrão RunPod Serverless — não usa o SDK `runpod`. É um pod
# "normal" que corre runpod/pod_server.py (FastAPI) e fica a postos para
# vários pedidos seguidos numa sessão de lote gerida por
# pipeline/runpod_pod_client.py (liga o pod, gera N peças, desliga o pod).
#
# Porque não corremos o setup.sh oficial do TRELLIS diretamente: esse script
# só instala as dependências CUDA (nvdiffrast, diffoctreerast, kaolin,
# spconv, mip-splatting) se detectar uma GPU física presente no momento da
# instalação (`torch.cuda.is_available()`). Isso funciona numa máquina com
# GPU, mas falha silenciosamente (salta a instalação sem erro) num
# `docker build` normal, que não tem acesso a GPU. Os passos abaixo replicam
# manualmente a mesma receita do README oficial do TRELLIS
# (--basic --xformers --flash-attn --diffoctreerast --spconv --mipgaussian
# --kaolin --nvdiffrast), mas fixando TORCH_CUDA_ARCH_LIST em vez de depender
# de detecção de GPU — nvcc consegue compilar para essas arquiteturas sem
# precisar de uma GPU física ligada durante o build.
#
# Build (fora desta máquina, requer Docker — funciona sem GPU local):
#   docker build -t <o-teu-user>/2d-to-3d-trellis:latest -f runpod/Dockerfile .
#   docker push <o-teu-user>/2d-to-3d-trellis:latest
#
# Depois: ver runpod/README.md para configurar RUNPOD_API_KEY/RUNPOD_IMAGE.

FROM nvidia/cuda:12.1.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9"
    # 8.0 = A100, 8.6 = A4000/A5000/A6000 (Ampere), 8.9 = RTX 4090 (Ada) —
    # cobre as GPUs em RUNPOD_GPU_TYPE_IDS (pipeline/runpod_pod_client.py)

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        python3.10 \
        python3.10-dev \
        python3-pip \
        ninja-build \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libsm6 \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*
    # libx11-6/libxext6/libxrender1/libsm6/libgl1/libglib2.0-0: o open3d importa
    # estas libs de sistema (mesmo sem GUI) — falha com "libX11.so.6: cannot
    # open shared object file" sem elas (confirmado no build real)

RUN ln -sf /usr/bin/python3.10 /usr/bin/python

WORKDIR /workspace

# 1) PyTorch (cu121, para combinar com a imagem base CUDA 12.1)
RUN pip install --no-cache-dir torch==2.4.0 torchvision==0.19.0 \
    --index-url https://download.pytorch.org/whl/cu121

# 2) Dependências "--basic" do TRELLIS (+ plyfile: o setup.sh oficial não a
# lista, mas trellis/representations/gaussian precisa dela — confirmado no
# build real: "ModuleNotFoundError: No module named 'plyfile'")
RUN pip install --no-cache-dir \
        pillow imageio imageio-ffmpeg tqdm easydict opencv-python-headless \
        scipy ninja rembg onnxruntime trimesh open3d xatlas pyvista \
        pymeshfix igraph transformers plyfile moderngl
# utils3d: instalar via pip (git+https) produz um wheel "UNKNOWN" praticamente
# vazio neste commit (bug de empacotamento do pyproject.toml deles, confirmado
# no build real) — clona-se e põe-se no PYTHONPATH diretamente em vez de
# depender do build de wheel do pip.
RUN git clone https://github.com/EasternJournalist/utils3d.git /opt/utils3d \
    && cd /opt/utils3d && git checkout 9a4eb15e4021b67b12c460c7057d642626897ec8

# 3) "--xformers" (versão fixada pelo setup.sh oficial para torch 2.4.0 + cu121)
RUN pip install --no-cache-dir xformers==0.0.27.post2 \
    --index-url https://download.pytorch.org/whl/cu121

# 4) "--flash-attn" — otimização de atenção, não estritamente necessária para
# correr o pipeline; se a compilação falhar (é lenta e pesada), não trava o
# build, só corre um pouco mais devagar.
RUN pip install --no-cache-dir flash-attn \
    || echo "[build] flash-attn falhou a instalar — a continuar sem ele"

# 5) "--kaolin" — wheel pré-compilada (não precisa de compilar nada)
RUN pip install --no-cache-dir kaolin \
    -f https://nvidia-kaolin.s3.us-east-2.amazonaws.com/torch-2.4.0_cu121.html

# 6) "--nvdiffrast" — o setup.py precisa de ver o torch já instalado (senão
# falha com "Cannot compile nvdiffrast CUDA extension"), por isso
# --no-build-isolation (não deixar o pip criar um venv de build isolado).
RUN git clone https://github.com/NVlabs/nvdiffrast.git /tmp/extensions/nvdiffrast \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/nvdiffrast

# 7) "--diffoctreerast" — compila kernels CUDA já no build (TORCH_CUDA_ARCH_LIST
# acima); mesmo motivo do --no-build-isolation que o nvdiffrast.
RUN git clone --recurse-submodules https://github.com/JeffreyXiang/diffoctreerast.git /tmp/extensions/diffoctreerast \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/diffoctreerast

# 8) "--mipgaussian" (mip-splatting / diff-gaussian-rasterization)
RUN git clone https://github.com/autonomousvision/mip-splatting.git /tmp/extensions/mip-splatting \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/mip-splatting/submodules/diff-gaussian-rasterization/

# 9) "--spconv" — wheel pré-compilada
RUN pip install --no-cache-dir spconv-cu120

# 10) Código do TRELLIS em si (não corremos o setup.sh dele — os passos acima
# já cobrem tudo o que ele instalaria para "--basic --xformers --flash-attn
# --diffoctreerast --spconv --mipgaussian --kaolin --nvdiffrast")
RUN git clone --recurse-submodules https://github.com/microsoft/TRELLIS.git /workspace/TRELLIS
ENV PYTHONPATH="/workspace/TRELLIS:/opt/utils3d:${PYTHONPATH}"

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

# Pré-descarrega os FICHEIROS do TRELLIS-image-large (pesos + configs) para
# dentro da imagem, para não pagar esse download a cada sessão de lote.
#
# Importante: NÃO instanciamos o pipeline aqui (TrellisImageTo3DPipeline.
# from_pretrained(...)) — o decoder de malha (slat_decoder_mesh) precisa de
# GPU física só para CONSTRUIR o modelo (não só para inferência), e falha
# com "RuntimeError: Found no NVIDIA driver" no build (confirmado no build
# real — esse erro ficava mascarado por um except genérico no código do
# TRELLIS, que tentava um caminho alternativo e falhava por outro motivo,
# escondendo a causa raiz). Por isso usamos snapshot_download, que só
# copia os ficheiros para a cache local — a construção real do modelo (que
# precisa de GPU) só acontece dentro do pod, em runpod/pod_server.py.
#
# HF_TOKEN autentica os pedidos (rate limit mais alto), passado como build
# secret (--mount=type=secret) para nunca ficar gravado em nenhuma camada.
RUN --mount=type=secret,id=HF_TOKEN \
    export HF_TOKEN="$(cat /run/secrets/HF_TOKEN 2>/dev/null || true)" && \
    python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('microsoft/TRELLIS-image-large')"

COPY runpod/pod_server.py /workspace/pod_server.py
WORKDIR /workspace

CMD ["uvicorn", "pod_server:app", "--host", "0.0.0.0", "--port", "8000"]
