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
# Voltámos a 12.1.0 (tínhamos passado brevemente para 12.4.1). O "CUDA
# unknown error" que nos fez suspeitar da imagem 12.1.0 afinal tinha DUAS
# causas reais, nada a ver com a versão da imagem: (1) o RunPod anexava por
# omissão um volume de 20GB em /workspace que tapava o conteúdo da imagem
# (ver pipeline/runpod_pod_client.py, volumeInGb=0), e (2) qualidade
# inconsistente de hosts no Community Cloud (resolvido testando em Secure
# Cloud). Com essas duas causas corrigidas, a imagem 12.1.0 voltou a
# funcionar perfeitamente (nvidia-smi OK, TRELLIS carregado, geração real a
# correr). Entretanto, com a 12.4.1, apareceu um crash nativo (processo
# inteiro morre, sem traceback Python) sempre no mesmo ponto — logo ao
# início da fase de slat sampling — reproduzido em 2 GPUs diferentes (A40 e
# RTX 4090). Suspeita: as nossas extensões CUDA (nvdiffrast, diffoctreerast,
# diff_gaussian_rasterization) são compiladas no build com o nvcc da imagem
# base, mas o torch/xformers continuam fixados em wheels cu121 — com base
# 12.4.1 isso passa a ser um desalinhamento real de toolkit (12.4) vs ABI
# esperada pelo torch (12.1), que a 12.1.0 nunca teve. Voltar a 12.1.0
# elimina essa fonte de crash sem reintroduzir o problema original (que já
# sabemos não ser da imagem).

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9" \
    ATTN_BACKEND=xformers
    # 8.0 = A100, 8.6 = A4000/A5000/A6000 (Ampere), 8.9 = RTX 4090 (Ada) —
    # cobre as GPUs em RUNPOD_GPU_TYPE_IDS (pipeline/runpod_pod_client.py)
    #
    # ATTN_BACKEND=xformers: o TRELLIS (trellis/modules/attention/__init__.py)
    # lê esta variável para escolher o backend de atenção; por omissão usa
    # 'flash_attn', que decidimos não instalar (compilação lenta e frágil).
    # Confirmado num pod real: sem isto, rebenta com "ModuleNotFoundError:
    # No module named 'flash_attn'" ao construir o modelo de fluxo — e esse
    # erro fica mascarado por um except genérico no código do TRELLIS, que
    # mostra uma mensagem de "Repository Not Found" completamente enganadora
    # em vez do erro real.

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

# O pip que vem do apt (Ubuntu 22.04) é muito antigo (22.0.2) e tem um bug
# conhecido: para pacotes com pyproject.toml E setup.py no mesmo repo (caso
# do nvdiffrast e do utils3d), não lê o nome em [project.name] e instala tudo
# como um pacote genérico "UNKNOWN" — confirmado no build real (2 pacotes
# diferentes com o mesmo sintoma). Atualizar aqui, antes de instalar mais
# nada, resolve na origem.
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

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

# 4) "--flash-attn" propositadamente OMITIDO: compilação lenta/frágil, e o
# TRELLIS funciona bem com xformers como backend de atenção (ATTN_BACKEND
# acima) — sem necessidade de flash-attn.

# 5) "--kaolin" — wheel pré-compilada (não precisa de compilar nada)
RUN pip install --no-cache-dir kaolin \
    -f https://nvidia-kaolin.s3.us-east-2.amazonaws.com/torch-2.4.0_cu121.html

# 6) "--nvdiffrast" — o setup.py precisa de ver o torch já instalado (senão
# falha com "Cannot compile nvdiffrast CUDA extension"), por isso
# --no-build-isolation (não deixar o pip criar um venv de build isolado).
# Verificação a seguir: confirmámos num pod real que este passo "passava"
# sem erro mas produzia um pacote partido (import falhava em runtime) —
# por isso testamos mesmo o import aqui, para o BUILD falhar já, não só
# ao ligar um pod (mais lento e com custo).
RUN git clone https://github.com/NVlabs/nvdiffrast.git /tmp/extensions/nvdiffrast \
    && pip install --no-cache-dir --no-build-isolation -v /tmp/extensions/nvdiffrast
RUN python -c "import nvdiffrast.torch; print('[verify] nvdiffrast OK')"

# 7) "--diffoctreerast" — compila kernels CUDA já no build (TORCH_CUDA_ARCH_LIST
# acima); mesmo motivo do --no-build-isolation que o nvdiffrast.
RUN git clone --recurse-submodules https://github.com/JeffreyXiang/diffoctreerast.git /tmp/extensions/diffoctreerast \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/diffoctreerast
RUN python -c "import diffoctreerast; print('[verify] diffoctreerast OK')"

# 8) "--mipgaussian" (mip-splatting / diff-gaussian-rasterization)
RUN git clone https://github.com/autonomousvision/mip-splatting.git /tmp/extensions/mip-splatting \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/mip-splatting/submodules/diff-gaussian-rasterization/
RUN python -c "import diff_gaussian_rasterization; print('[verify] diff_gaussian_rasterization OK')"

# 9) "--spconv" — compilado a PARTIR DO CÓDIGO (não a wheel pré-compilada
# spconv-cu120 do PyPI, que é de abril de 2023, contra um toolchain CUDA já
# antigo). Confirmado em pods reais (2 GPUs diferentes — A40 e RTX 4090 — e 2
# versões de imagem CUDA base — 12.1 e 12.4) um crash nativo (processo morre,
# sem traceback Python) 100% reprodutível logo no início da fase de slat
# sampling do TRELLIS — exatamente o primeiro ponto onde o spconv é mesmo
# invocado. A própria documentação do spconv (COMMON_PROBLEMS.md, "PTX
# compilado com toolchain não suportado") descreve este sintoma e recomenda
# compilar contra uma versão de CUDA mais próxima da do sistema — não existe
# wheel PyPI mais recente que cu120, por isso compilamos nós.
# cumm é uma dependência obrigatória do spconv (documentado no README deles).
# CUMM_CUDA_ARCH_LIST usa o mesmo formato/lista que TORCH_CUDA_ARCH_LIST
# acima, para cobrir as mesmas GPUs.
ENV CUMM_CUDA_ARCH_LIST="8.0;8.6;8.9"
# Dependências de build do cumm (setup.py deles importa isto diretamente,
# --no-build-isolation não instala nada disto sozinho — confirmado no build
# real: "ModuleNotFoundError: No module named 'pccm'").
RUN pip install --no-cache-dir pccm pybind11 fire numpy sympy ccimport
RUN git clone https://github.com/FindDefinition/cumm.git /tmp/extensions/cumm \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/cumm
RUN git clone https://github.com/traveller59/spconv.git /tmp/extensions/spconv \
    && pip install --no-cache-dir --no-build-isolation /tmp/extensions/spconv
RUN python -c "import spconv.pytorch; print('[verify] spconv OK')"

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
