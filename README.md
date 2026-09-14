# TRELLIS no RunPod — imagem→3D

Container Docker que corre o [TRELLIS](https://github.com/microsoft/TRELLIS) (Microsoft,
`microsoft/TRELLIS-image-large`, 1.2B parâmetros, MIT) num pod RunPod alugado: recebe 1+
fotos de um objeto, devolve um `.glb` (modelo 3D) gerado por IA generativa.

## Estrutura

```
2d-to-3d-api/
├── runpod/
│   ├── Dockerfile           # imagem do pod: CUDA + TRELLIS + dependências, build/push via CI
│   ├── pod_server.py        # servidor (FastAPI) que corre DENTRO do pod — carrega o TRELLIS
│   │                          uma vez, expõe /health + /generate
│   └── README.md            # setup completo: build+push da imagem, credenciais, custos
├── pipeline/
│   ├── runpod_pod_client.py # cliente: cria/liga/desliga o pod, chama /generate
│   ├── trellis_pipeline.py  # exemplo de CLI sobre o cliente (fotos -> .glb)
│   └── requirements.txt
├── .github/workflows/publicar-imagem-trellis.yml  # build+push automático da imagem (GitHub Actions)
└── .env                     # RUNPOD_API_KEY, RUNPOD_IMAGE (não versionar, ver .env.example)
```

## O modelo

[TRELLIS](https://github.com/microsoft/TRELLIS) (CVPR'25 Spotlight, Microsoft) gera uma
malha 3D texturada a partir de 1+ imagens de um objeto isolado (fundo removido
automaticamente). Não é fotogrametria/digitalização métrica — é geração generativa: boa
para visualização/catálogo, não garante medidas exatas da peça real.

A imagem publicada (`runpod/Dockerfile`) compila algumas das dependências CUDA do TRELLIS
a partir do código-fonte (`spconv`, `cumm`, `nvdiffrast`, `diffoctreerast`,
`diff_gaussian_rasterization`) em vez de usar wheels pré-compiladas — algumas wheels
públicas são antigas o suficiente para dar crashes nativos (sem traceback) em hosts com
drivers NVIDIA recentes; ver comentários no próprio Dockerfile para o histórico completo.

## Setup (uma vez) — publicar a imagem

Ver `runpod/README.md` para o passo a passo completo (build+push, credenciais,
`RUNPOD_CLOUD_TYPE`, custos reais medidos). Resumo:

1. Build+push da imagem — automático via GitHub Actions
   (`.github/workflows/publicar-imagem-trellis.yml`) a cada alteração em `runpod/`, ou
   manual: `docker build -t <utilizador>/2d-to-3d-trellis:latest -f runpod/Dockerfile .`
2. Copiar `.env.example` para `.env`, preencher `RUNPOD_API_KEY` (consola RunPod →
   Settings → API Keys) e `RUNPOD_IMAGE` (a imagem publicada no passo 1). Deixar
   `RUNPOD_POD_ID` vazio — o próprio código cria o pod na 1ª sessão e grava o id aí.

Não há endpoint nenhum para criar manualmente na consola RunPod — o pod (persistente,
Secure Cloud por omissão) é criado e gerido pelo próprio código
(`pipeline/runpod_pod_client.py`).

## Uso

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r pipeline\requirements.txt
```

Direto pelo cliente (`pipeline/runpod_pod_client.py`) — liga o pod, gera, desliga:

```python
import base64
from pipeline import runpod_pod_client as rpc

with rpc.PodSession() as session:
    images_b64 = [base64.b64encode(open(p, "rb").read()).decode("ascii") for p in ["foto1.jpg", "foto2.jpg"]]
    glb_bytes = session.generate(images_b64, quality="quality", seed=1)  # "fast" para iterar mais depressa
    open("peca.glb", "wb").write(glb_bytes)
```

Múltiplas imagens da mesma peça (ângulos diferentes) tendem a dar melhor geometria — o
`pod_server.py` usa automaticamente o modo multi-imagem do TRELLIS quando recebe mais que
uma foto.

`pipeline/trellis_pipeline.py` é um exemplo de CLI construído sobre este cliente
(`--input <pasta_de_fotos> --part-id X --quality quality`) — grava o `.glb` e alguns
metadados num `catalog/` local que não faz parte deste repositório; serve de referência,
não é obrigatório usá-lo.

## Contrato do servidor dentro do pod (`runpod/pod_server.py`)

- `GET /health` — 200 assim que o TRELLIS estiver carregado na GPU
- `POST /generate` — `{images_b64, quality, seed}` → `{job_id}` (não bloqueia — uma
  geração pode demorar mais que o timeout do proxy HTTP do RunPod)
- `GET /generate/{job_id}` — `{status: pending|done|error, glb_b64?, error?}`

## Porquê pod persistente (não Serverless, não recriado a cada sessão)

Para o volume esperado (poucas gerações/dia), um pod Community/Secure Cloud com
Start/Stop sai mais barato por hora de GPU que RunPod Serverless, e manter o mesmo pod
(só parar/retomar, nunca apagar) evita repetir o "cold start" de puxar a imagem inteira
(~12GB) a cada sessão — o container disk fica em cache enquanto o pod está parado, sem
custo. Custos reais medidos e o racional completo (incluindo porque Secure Cloud e não
Community Cloud) estão em `runpod/README.md`.
