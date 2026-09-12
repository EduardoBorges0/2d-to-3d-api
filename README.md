# Catálogo 3D de Peças — Almovi

Pipeline: fotos de peças → TRELLIS no RunPod (image-to-3D) → GLB → catálogo web com
visualizador e edição por texto, seguindo o design system Almovi.

## Estrutura

```
2d-to-3d-api/
├── photos_input/            # fotos de cada peça (1 subpasta por peça, ex: photos_input/valvula-002/*.jpg)
├── pipeline/
│   ├── manual_pipeline.py   # extração de imagens+legendas de manuais técnicos (PDF)
│   ├── trellis_pipeline.py  # fotos -> RunPod (TRELLIS) -> GLB, atualiza catalog/data/parts.json
│   ├── runpod_pod_client.py # liga/desliga um pod RunPod (Community Cloud) por sessão de lote
│   ├── edit_pipeline.py     # edição por texto (furo/saliência paramétricos) sobre um GLB existente
│   ├── server.py            # backend FastAPI: catálogo + "Adicionar peça" + "Processar pendentes" + "Editar por texto"
│   └── requirements.txt
├── runpod/
│   ├── pod_server.py        # servidor (FastAPI) que corre DENTRO do pod, carrega o TRELLIS
│   ├── Dockerfile           # imagem do pod (build/push manual, ver runpod/README.md)
│   └── README.md            # passos manuais: publicar a imagem + credenciais
├── catalog/
│   ├── index.html           # visualizador web (design system Almovi)
│   ├── app.js
│   ├── styles.css
│   ├── data/parts.json      # catálogo de peças (fonte de dados do visualizador)
│   └── models/*.glb         # modelos 3D gerados (e versões editadas: <id>_v2.glb, <id>_v3.glb, ...)
├── .venv/                   # ambiente virtual Python deste projeto (não versionar)
├── .env                     # RUNPOD_API_KEY, RUNPOD_IMAGE, ANTHROPIC_API_KEY (não versionar, ver .env.example)
└── .claude/launch.json      # arranca pipeline/server.py em localhost:8791 para pré-visualização
```

## 0. Setup (uma vez) — ambiente virtual + credenciais

Há várias instalações de Python nesta máquina (`py`, `python`, etc. podem apontar para
sítios diferentes e causar `ModuleNotFoundError`). Para evitar esse problema, usa sempre
o `.venv` deste projeto:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r pipeline\requirements.txt
```

Este `.venv` já não precisa de torch/CUDA — a inferência do TRELLIS corre num pod
RunPod ligado por sessão de lote (ver `runpod/README.md`, passo único: publicar a
imagem Docker — não há endpoint nenhum para criar na consola).

Copiar `.env.example` para `.env` e preencher `RUNPOD_API_KEY`, `RUNPOD_IMAGE`
(a imagem publicada em `runpod/README.md`) e `ANTHROPIC_API_KEY` (usada por
`edit_pipeline.py` para interpretar as instruções de edição por texto).

## 1. Ligar o servidor (catálogo + API)

```powershell
.venv\Scripts\python.exe pipeline\server.py
```

Depois abrir `http://localhost:8791`. Contém 3 peças placeholder (`suporte`, `válvula`,
`vedante`) para validar grid, pesquisa, filtros, badges e modal 3D, mais dois botões:
**Adicionar peça** (upload de fotos → fica em fila, `estado: "a_processar"`) e
**Processar pendentes** (liga 1 pod RunPod, gera todas as peças em fila, desliga o pod).

Não abrir `catalog/index.html` diretamente no browser (`file://`) — o `fetch` do
`parts.json` e o upload de fotos precisam do servidor.

## 2. Pipeline de extração (manuais técnicos)

`pipeline/manual_pipeline.py` já existia e extrai imagens + legendas associadas de PDFs
de manuais técnicos (útil como fonte de fotos/diagramas de peças a alimentar o TRELLIS
quando não há fotografia própria da peça).

```powershell
.venv\Scripts\python.exe pipeline\manual_pipeline.py caminho_para_manual.pdf
```

Depende de `PyMuPDF` (fitz), `pytesseract` (+ Tesseract instalado no sistema) e `Pillow`.

## 3. Pipeline TRELLIS (fotos → GLB, via RunPod — por sessão de lote)

`pipeline/trellis_pipeline.py` orquestra a geração: liga um pod RunPod (Community
Cloud, `pipeline/runpod_pod_client.py`), que corre o
[TRELLIS (Microsoft)](https://github.com/microsoft/TRELLIS) numa GPU alugada, gera
1+ peças na mesma sessão, e desliga o pod no fim. Este `.venv` local **não precisa de
GPU nem de instalar o TRELLIS**.

Optou-se por **Pod + Community Cloud** em vez de RunPod Serverless porque, para o
volume esperado (poucas peças/dia), sai ~4x mais barato por hora de GPU e evita pagar
o arranque do modelo a cada pedido individual — uma sessão arranca 1x e processa tudo
o que estiver pendente nesse momento. Ver `runpod/README.md` para os números e o
racional completo.

### Setup (uma vez) — publicar a imagem

Seguir `runpod/README.md`: build+push da imagem Docker (`runpod/Dockerfile` +
`runpod/pod_server.py`), copiar `RUNPOD_API_KEY`/`RUNPOD_IMAGE` para o `.env` da raiz
(ver `.env.example`). Não há endpoint nenhum para criar na consola — os pods são
criados/destruídos por sessão, via API.

Sem `.env` preenchido, `server.py` continua a funcionar — os pedidos ficam com estado
`erro` e a mensagem `RUNPOD_API_KEY e/ou RUNPOD_IMAGE não definidos...`, em vez de
falhar em silêncio.

### Uso

Organizar as fotos por peça:

```
photos_input/
├── suporte-002/
│   ├── foto1.jpg
│   └── foto2.jpg   # múltiplas vistas = melhor geometria (usa run_multi_image)
└── valvula-003/
    └── foto1.jpg
```

**Modo do dia-a-dia** — processar tudo o que está em fila (`a_processar`, vindo do
"Adicionar peça" do site) numa única sessão de pod: pelo botão **Processar pendentes**
no site, ou por CLI (ex: numa tarefa agendada 1x/dia):

```powershell
.venv\Scripts\python.exe pipeline\trellis_pipeline.py --pending --quality quality
```

Alternativas para uso pontual/CLI direto (também abrem e fecham a sua própria sessão):

```powershell
# 1 peça específica
.venv\Scripts\python.exe pipeline\trellis_pipeline.py `
  --input photos_input\suporte-002 --part-id suporte-002 `
  --nome "Suporte de fixação (variante 2)" --categoria Estrutura `
  --equipamento "Grua de aranha" --referencia SUP-002 `
  --quality quality

# todas as subpastas de photos_input/ de uma vez (ignora o estado no catálogo)
.venv\Scripts\python.exe pipeline\trellis_pipeline.py --batch photos_input --quality quality
```

O script, para cada peça processada:
1. Envia as fotos à sessão de pod ativa (`quality` = mais steps + texturas 2048px;
   `fast` = iteração rápida)
2. Exporta o `.glb` para `catalog/models/<part-id>.glb`
3. Atualiza (ou cria) a entrada correspondente em `catalog/data/parts.json` com
   `estado: "gerado"` — aparece automaticamente no visualizador ao dar refresh/Atualizar

Usar `--dry-run` para validar pastas de fotos e a escrita no catálogo sem ligar
nenhum pod (não gera GLB real, não tem custo).

### Estados de uma peça no catálogo

- `placeholder` — modelo de teste, não é a peça real (badge cinza)
- `a_processar` — fotos recebidas via "Adicionar peça", em fila para o próximo
  processamento em lote (badge amarelo "Pendente (lote)") — não arranca nada sozinho
- `a_editar` — uma edição por texto está a ser aplicada (badge amarelo, spinner)
- `gerado` — saiu do TRELLIS (ou de uma edição aplicada com sucesso), ainda por validar visualmente (badge azul)
- `aprovado` — revisto e validado para publicação (badge verde) — marcar manualmente
  no `parts.json` depois de conferir o modelo
- `erro` — o pipeline (geração ou edição) falhou; a mensagem de erro real fica guardada
  no campo `erro` e visível no modal de detalhe. As fotos ficam em
  `photos_input/<id>/`. "Processar pendentes" **não repete automaticamente** peças em
  `erro` (só apanha `a_processar`) — para reprocessar depois de corrigir a causa, muda
  o `estado` de volta para `"a_processar"` no `parts.json` (ou usa `--input` diretamente
  para essa peça).

## 4. Editar peça por texto (furo/saliência)

No modal de detalhe de uma peça já `gerado`, a caixa "Editar por texto" aceita
instruções como:

- "adicionar buraco de chaveiro de 50mm no topo"
- "criar uma saliência cilíndrica de 10mm no fundo, no canto superior esquerdo"

Isto **não é edição 3D generativa** — é uma operação CAD paramétrica real
(`pipeline/edit_pipeline.py`): o Claude interpreta o texto como furo (`hole`) ou
saliência (`boss`) cilíndricos com diâmetro/face definidos, e uma booleana
(`trimesh` + `manifold3d`) corta/funde um cilindro na malha existente. Se o texto não
mapear claramente para isto (dimensão em falta, pedido de edição livre/orgânica), a peça
fica em `erro` com o motivo — nunca inventa valores.

**Limitação conhecida (aceite para esta 1ª versão):** não há clique no visualizador para
apontar o sítio exato — a posição é resolvida por heurística (face pedida + bounding box
da peça, assumindo GLB Z-up, confirmado nos modelos deste catálogo: topo/fundo = eixo Z,
frente/trás = eixo Y, esquerda/direita = eixo X). Para peças com geometria não-convexa
(ex: um suporte em L) a posição pedida pode cair fora do material real — nesse caso a
edição falha com um erro explícito em vez de não fazer nada silenciosamente. Confirmar
sempre no visualizador antes de aprovar.

Cada edição bem-sucedida gera uma **nova versão** do `.glb`
(`catalog/models/<id>_v2.glb`, `_v3.glb`, ...) sem apagar a anterior, e fica registada em
`historico_edicoes` no `parts.json` (texto pedido + resultado), visível no modal.

## Próximos passos sugeridos

- Passo de revisão: alguém marca `gerado` → `aprovado` depois de olhar para o modelo
- Ação de "reprocessar" no visualizador para peças em `erro` (hoje só via edição manual do `parts.json` ou `--input`)
- Agendar `trellis_pipeline.py --pending` (Task Scheduler no Windows, ou cron) para
  correr sozinho 1x/dia, sem depender de alguém clicar "Processar pendentes"
- Editor: suportar mais operações (chanfro, rasgo) e, se a heurística de posição não for
  fiável o suficiente na prática, um clique no visualizador para apontar o ponto exato
