# Setup do RunPod (pod persistente, Secure Cloud)

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

Na 1ª vez: cria o pod (Secure Cloud, GPU mínima 16GB — ver
`RUNPOD_GPU_TYPE_IDS` em `.env.example` para a ordem de preferência), espera
o TRELLIS carregar (`/health`), gera a peça, e **pára o pod** (não o termina —
fica na consola RunPod com estado "Stopped", pronto a retomar mais rápido da
próxima vez). Nas vezes seguintes, o mesmo comando reutiliza esse pod.

Para testar o fluxo pensado para o dia-a-dia (1 sessão para várias peças em
fila), usa o site: "Adicionar peça" várias vezes (fica tudo em
`estado: "a_processar"`, sem gastar nada), depois "Processar pendentes".

## Notas

- **Porque Secure Cloud por omissão**: testámos primeiro Community Cloud
  (mais barato) e deu sempre "CUDA unknown error" ao inicializar o driver
  dentro do container — confirmado em vários hosts/GPUs diferentes (A40, RTX
  4090), não era bug nosso. O mesmo setup em Secure Cloud (datacenters
  próprios do RunPod) funcionou de forma fiável. Se quiseres tentar poupar
  (Community Cloud é ~4x mais barato por hora), define
  `RUNPOD_CLOUD_TYPE=COMMUNITY` no `.env` — mas é possível voltar a apanhar
  hosts inconsistentes.
- **Disponibilidade variável**: mesmo em Secure Cloud, cada pod fica preso a
  uma máquina física específica — se essa GPU estiver ocupada quando tentas
  retomar (Start) um pod parado, a API devolve "not enough free GPUs on the
  host machine" e não há como forçar; `pipeline/runpod_pod_client.py` trata
  isto automaticamente criando um pod novo nesse caso (não inventa sucesso,
  só não fica preso a um host indisponível). Ver também
  [status.runpod.io](https://status.runpod.io) / [statusgator.com/services/runpod](https://statusgator.com/services/runpod)
  para incidentes ativos na plataforma.
- **Custo do pod parado: $0/h.** O container disk (onde está a imagem) é
  grátis parado, e não pedimos nenhum volume persistente (`volumeInGb=0` no
  `create_pod()`) — chegámos a ter um volume de 20GB por omissão do RunPod
  que, além de custar, tapava o conteúdo da imagem em `/workspace` (bug real,
  já corrigido).
- **IP pode mudar**: o cliente usa sempre o URL proxy
  (`https://{pod_id}-8000.proxy.runpod.net`), não o IP público, por isso isto
  não afeta o funcionamento.
