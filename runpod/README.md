# Setup do RunPod (Pod em Community Cloud, por sessão de lote)

Ao contrário de um endpoint RunPod Serverless, aqui **não crias nada antecipadamente
na consola** além de publicar a imagem Docker — cada sessão de lote
(`pipeline/trellis_pipeline.py --pending` ou o botão "Processar pendentes" no
site) liga um pod novo via API, usa-o para todas as peças pendentes, e
termina-o no fim. Ver [pipeline/runpod_pod_client.py](../pipeline/runpod_pod_client.py)
para a implementação e a explicação do porquê deste modelo (custo ~4x menor
que Serverless para o volume deste catálogo).

## 1. Build + push da imagem Docker

Numa máquina com Docker (não precisa de GPU para o build, só para correr):

```bash
docker build -t <o-teu-user-dockerhub>/2d-to-3d-trellis:latest -f runpod/Dockerfile .
docker push <o-teu-user-dockerhub>/2d-to-3d-trellis:latest
```

A imagem fica grande (CUDA + torch + pesos do TRELLIS pré-descarregados,
tipicamente vários GB) — é normal, é o preço de cada sessão de lote arrancar
mais depressa (sem re-descarregar o modelo).

## 2. Credenciais

- **RUNPOD_API_KEY**: consola RunPod → Settings → API Keys → criar uma chave.
- **RUNPOD_IMAGE**: o nome:tag que publicaste no passo 1
  (`<o-teu-user-dockerhub>/2d-to-3d-trellis:latest`).

Copiar para um `.env` na raiz do projeto (ver `.env.example`):

```
RUNPOD_API_KEY=...
RUNPOD_IMAGE=<o-teu-user-dockerhub>/2d-to-3d-trellis:latest
```

## 3. Validar

Com o `.env` preenchido e o `.venv` do projeto com as dependências instaladas
(`pipeline/requirements.txt`):

```powershell
.venv\Scripts\python.exe pipeline\trellis_pipeline.py --input photos_input\<peça> --part-id teste-runpod
```

Isto liga um pod (Community Cloud, GPU mínima 16GB — ver
`RUNPOD_GPU_TYPE_IDS` em `.env.example` para a ordem de preferência), espera
o TRELLIS carregar (`/health`), gera a peça, e **termina o pod logo a seguir**
— confirma na consola RunPod que não fica nenhum pod esquecido ligado.

Para testar o fluxo pensado para o dia-a-dia (1 sessão para várias peças em
fila), usa o site: "Adicionar peça" várias vezes (fica tudo em
`estado: "a_processar"`, sem gastar nada), depois "Processar pendentes" —
deve aparecer 1 pod na consola RunPod, processar todas, e desaparecer.

## Notas sobre Community Cloud

- **Disponibilidade variável**: Community Cloud é mais barato porque usa
  capacidade de terceiros — pode não haver máquina livre com a GPU pedida num
  dado momento. `RUNPOD_GPU_TYPE_IDS` aceita uma lista (o RunPod tenta pela
  ordem); se mesmo assim falhar, `pipeline/runpod_pod_client.py` lança
  `PodError` com a mensagem da API, sem inventar sucesso.
- **IP pode mudar**: o cliente usa sempre o URL proxy
  (`https://{pod_id}-8000.proxy.runpod.net`), não o IP público, por isso isto
  não afeta o funcionamento — só o acesso direto por SSH, que não usamos aqui.
- Se preferires mais previsibilidade (ao custo de preço mais alto), define
  `RUNPOD_CLOUD_TYPE=SECURE` no `.env`.
