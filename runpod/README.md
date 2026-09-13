# Setup do RunPod (pod persistente, Community Cloud)

Ao contrário de um endpoint RunPod Serverless, aqui **não crias nada antecipadamente
na consola** além de publicar a imagem Docker — o próprio código cria o pod na
1ª vez que corre uma sessão de lote
(`pipeline/trellis_pipeline.py --pending` ou o botão "Processar pendentes" no
site) e grava o id em `RUNPOD_POD_ID` no `.env`. Da 2ª vez em diante, cada
sessão só **retoma** (Start) esse mesmo pod e **pára-o** (Stop) no fim — nunca
o apaga sozinho, porque isso perderia a imagem já em cache no disco e o
arranque voltaria a ser lento. Ver
[pipeline/runpod_pod_client.py](../pipeline/runpod_pod_client.py) para a
implementação e a explicação completa do porquê deste modelo.

## 1. Build + push da imagem Docker

Numa máquina com Docker (não precisa de GPU para o build, só para correr):

```bash
docker build -t <o-teu-user-dockerhub>/2d-to-3d-trellis:latest -f runpod/Dockerfile .
docker push <o-teu-user-dockerhub>/2d-to-3d-trellis:latest
```

A imagem fica grande (CUDA + torch + pesos do TRELLIS pré-descarregados,
~12GB) — é normal, é o preço de cada sessão arrancar mais depressa (sem
re-descarregar o modelo). Ver `.github/workflows/publicar-imagem-trellis.yml`
para fazer isto no GitHub Actions em vez de localmente (rede de datacenter,
muito mais rápido) — precisa dos secrets `DOCKERHUB_USERNAME`,
`DOCKERHUB_TOKEN` e `HF_TOKEN` (ver abaixo) no repositório.

## 2. Credenciais

- **RUNPOD_API_KEY**: consola RunPod → Settings → API Keys → criar uma chave.
- **RUNPOD_IMAGE**: o nome:tag que publicaste no passo 1
  (`<o-teu-user-dockerhub>/2d-to-3d-trellis:latest`).
- **HF_TOKEN** (só para o build, não para correr): token do Hugging Face
  (huggingface.co/settings/tokens, tipo "Read") — autentica o download dos
  pesos do TRELLIS durante o build, evitando rate-limit anónimo.

Copiar para um `.env` na raiz do projeto (ver `.env.example`):

```
RUNPOD_API_KEY=...
RUNPOD_IMAGE=<o-teu-user-dockerhub>/2d-to-3d-trellis:latest
RUNPOD_POD_ID=
```

Deixar `RUNPOD_POD_ID` vazio — é preenchido sozinho na 1ª sessão.

## 3. Validar

Com o `.env` preenchido e o `.venv` do projeto com as dependências instaladas
(`pipeline/requirements.txt`):

```powershell
.venv\Scripts\python.exe pipeline\trellis_pipeline.py --input photos_input\<peça> --part-id teste-runpod
```

Na 1ª vez: cria o pod (Community Cloud, GPU mínima 16GB — ver
`RUNPOD_GPU_TYPE_IDS` em `.env.example` para a ordem de preferência), espera
o TRELLIS carregar (`/health`), gera a peça, e **pára o pod** (não o termina —
fica na consola RunPod com estado "Stopped", pronto a retomar mais rápido da
próxima vez). Nas vezes seguintes, o mesmo comando reutiliza esse pod.

Para testar o fluxo pensado para o dia-a-dia (1 sessão para várias peças em
fila), usa o site: "Adicionar peça" várias vezes (fica tudo em
`estado: "a_processar"`, sem gastar nada), depois "Processar pendentes".

## Notas

- **Disponibilidade variável (Community Cloud e às vezes até Secure Cloud)**:
  usa capacidade partilhada — pode genuinamente não haver máquina livre com a
  GPU pedida num dado momento (confirmámos isto na prática: a própria consola
  web do RunPod recusou o mesmo pedido). `RUNPOD_GPU_TYPE_IDS` aceita uma
  lista (o RunPod tenta pela ordem); se mesmo assim falhar,
  `pipeline/runpod_pod_client.py` lança `PodError` com a mensagem da API, sem
  inventar sucesso — a solução é simplesmente tentar mais tarde. Ver também
  [status.runpod.io](https://status.runpod.io) / [statusgator.com/services/runpod](https://statusgator.com/services/runpod)
  para incidentes ativos na plataforma.
- **Custo do pod parado**: o container disk (onde está a imagem) é grátis
  parado; só o volume (`volumeInGb`, ~20GB por omissão) continua a ser
  cobrado, ~$0.20/GB/mês (≈ $4/mês neste caso), mesmo sem gerar nada. Para
  deixar de pagar isto por completo, termina o pod à mão na consola RunPod
  (ou chama `runpod_pod_client.terminate_pod(pod_id)`) — a próxima sessão
  cria um novo automaticamente (voltando ao arranque lento uma vez).
- **IP pode mudar**: o cliente usa sempre o URL proxy
  (`https://{pod_id}-8000.proxy.runpod.net`), não o IP público, por isso isto
  não afeta o funcionamento.
- Se preferires mais previsibilidade de stock (ao custo de preço mais alto),
  define `RUNPOD_CLOUD_TYPE=SECURE` no `.env`.
