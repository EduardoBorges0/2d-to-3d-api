"""
Pipeline: fotos de peças -> TRELLIS (image-to-3D, modo qualidade) -> GLB.

A inferência do TRELLIS corre num pod RunPod (Community Cloud) ligado por
sessão de lote (ver runpod/README.md) — este processo local não precisa de
GPU nem do repositório TRELLIS instalado, só de uma sessão (PodSession) para
enviar as imagens e esperar pelo(s) GLB(s). Uma sessão fica ligada o tempo do
lote todo (1+ peças) e desliga-se no fim — é isto que torna o custo baixo
para uso pouco frequente (ver runpod/README.md para o porquê).

Este ficheiro orquestra:
  1. Lê 1+ fotos de uma peça (uma pasta por peça em photos_input/<part_id>/)
  2. Submete-as à sessão RunPod ativa (single-image ou multi-image consoante
     o número de fotos disponíveis — multi-image tende a dar melhor geometria)
  3. Exporta o .glb devolvido para catalog/models/<part_id>.glb
  4. Atualiza catalog/data/parts.json com o novo estado ("gerado") e metadados

Uso:
    # 1 peça específica (abre e fecha uma sessão só para esta peça)
    python trellis_pipeline.py --input ../photos_input/suporte-002 --part-id suporte-002 \
        --nome "Suporte X" --categoria Estrutura --equipamento "Grua de aranha" \
        --referencia SUP-002 --quality quality

    # todas as subpastas de photos_input/ de uma vez (1 sessão para todas)
    python trellis_pipeline.py --batch ../photos_input --quality quality

    # tudo o que está "a_processar" no catálogo (fila do "Adicionar peça" do
    # site) — é este o modo pensado para correr 1x/dia, à mão ou agendado
    python trellis_pipeline.py --pending --quality quality
"""

import argparse
import base64
import datetime
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

import runpod_pod_client as rpc

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_JSON = REPO_ROOT / "catalog" / "data" / "parts.json"
MODELS_DIR = REPO_ROOT / "catalog" / "models"

load_dotenv(REPO_ROOT / ".env")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Presets de qualidade — "fast" serve para iterar rápido, "quality" é o modo
# final para peças que vão mesmo para o catálogo. Ajustar conforme resultados
# reais (nº de fotos, complexidade da peça).
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


def load_images(folder: Path):
    files = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS
    )
    if not files:
        raise FileNotFoundError(f"Nenhuma imagem encontrada em {folder}")
    return files


def run_trellis(session: rpc.PodSession, image_paths, quality: str, seed: int) -> bytes:
    """Envia 1+ imagens da mesma peça à sessão de pod RunPod ativa e devolve os
    bytes do .glb. O payload/output tem de ficar em sincronia com
    runpod/pod_server.py."""
    images_b64 = [base64.b64encode(p.read_bytes()).decode("ascii") for p in image_paths]
    return session.generate(images_b64, quality=quality, seed=seed)


def load_catalog():
    if CATALOG_JSON.exists():
        with open(CATALOG_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"updated_at": None, "parts": []}


def save_catalog(catalog):
    catalog["updated_at"] = datetime.date.today().isoformat()
    with open(CATALOG_JSON, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)


def upsert_part(catalog, part_id, glb_relpath, meta, origem_fotos, estado="gerado", erro=None, quality=None):
    parts = catalog.setdefault("parts", [])
    existing = next((p for p in parts if p["id"] == part_id), None)
    entry = {
        "id": part_id,
        "nome": meta.get("nome") or part_id,
        "categoria": meta.get("categoria") or "",
        "equipamento": meta.get("equipamento") or "",
        "referencia": meta.get("referencia") or part_id.upper(),
        "descricao": meta.get("descricao") or "",
        "modelo_glb": glb_relpath,
        "estado": estado,
        "origem_fotos": origem_fotos,
        "manual_origem": meta.get("manual_origem"),
        "erro": erro,
    }
    if quality:
        entry["quality"] = quality
    if existing:
        existing.update(entry)
    else:
        parts.append(entry)


def update_part_fields(part_id, **fields):
    """Atualiza campos de uma peça já existente no catálogo (usado pelo servidor
    para marcar transições de estado: a_processar -> gerado / erro)."""
    catalog = load_catalog()
    entry = next((p for p in catalog.get("parts", []) if p["id"] == part_id), None)
    if entry is None:
        return
    entry.update(fields)
    save_catalog(catalog)


def generate_glb(session: rpc.PodSession, image_paths, quality: str, seed: int, output_path: Path):
    """Corre o TRELLIS (via sessão RunPod ativa) e exporta o .glb para
    output_path. Não toca no catálogo — usado tanto pelo CLI (process_part)
    como pelo servidor (job de lote em background)."""
    glb_bytes = run_trellis(session, image_paths, quality=quality, seed=seed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(glb_bytes)
    return output_path


def record_edit(part_id: str, instrucao: str, novo_glb_relpath: str | None, estado: str, erro: str | None = None):
    """Acrescenta uma entrada ao historico_edicoes da peça e, em sucesso, avança
    modelo_glb para a nova versão. Usado pelo servidor depois de edit_pipeline.edit_part."""
    catalog = load_catalog()
    entry = next((p for p in catalog.get("parts", []) if p["id"] == part_id), None)
    if entry is None:
        return

    historico = entry.setdefault("historico_edicoes", [])
    historico.insert(0, {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "instrucao": instrucao,
        "versao": novo_glb_relpath,
        "estado": estado,
        "erro": erro,
    })

    if estado == "sucesso" and novo_glb_relpath:
        entry["modelo_glb"] = novo_glb_relpath
        entry["estado"] = "gerado"
        entry["erro"] = None
    else:
        entry["estado"] = "erro"
        entry["erro"] = erro

    save_catalog(catalog)


def process_part(session: "rpc.PodSession | None", part_id, input_dir: Path, quality: str, seed: int, meta, dry_run: bool):
    images = load_images(input_dir)
    print(f"[{part_id}] {len(images)} imagem(ns) em {input_dir}")

    glb_path = MODELS_DIR / f"{part_id}.glb"

    if dry_run:
        print(f"[{part_id}] --dry-run: a saltar inferência TRELLIS. Destino seria {glb_path}")
    else:
        generate_glb(session, images, quality=quality, seed=seed, output_path=glb_path)
        print(f"[{part_id}] GLB exportado para {glb_path}")

    catalog = load_catalog()
    upsert_part(
        catalog,
        part_id,
        glb_relpath=f"models/{glb_path.name}",
        meta=meta,
        origem_fotos=[str(p.relative_to(REPO_ROOT)) if REPO_ROOT in p.parents else str(p) for p in images],
        estado="gerado",
    )
    save_catalog(catalog)
    print(f"[{part_id}] catalog/data/parts.json atualizado (estado=gerado)")


def process_pending(session: "rpc.PodSession | None", quality: str, seed: int, dry_run: bool = False):
    """Processa, na sessão de pod já ligada, todas as peças com estado
    'a_processar' no catálogo (fila alimentada pelo formulário "Adicionar
    peça" do site). É o modo pensado para correr 1x/dia (à mão ou agendado).

    Devolve (sucesso: list[str], falhas: dict[str, str]) com os ids das peças.
    """
    catalog = load_catalog()
    pendentes = [p for p in catalog.get("parts", []) if p.get("estado") == "a_processar"]

    sucesso, falhas = [], {}
    for entry in pendentes:
        part_id = entry["id"]
        input_dir = REPO_ROOT / "photos_input" / part_id
        meta = {
            "nome": entry.get("nome"),
            "categoria": entry.get("categoria"),
            "equipamento": entry.get("equipamento"),
            "referencia": entry.get("referencia"),
            "descricao": entry.get("descricao"),
        }
        part_quality = entry.get("quality") or quality
        try:
            process_part(session, part_id, input_dir, part_quality, seed, meta, dry_run)
            sucesso.append(part_id)
        except Exception as e:
            update_part_fields(part_id, estado="erro", erro=f"{type(e).__name__}: {e}")
            falhas[part_id] = str(e)
            print(f"[{part_id}] falhou: {e}", file=sys.stderr)

    return sucesso, falhas


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, help="Pasta com as fotos de UMA peça")
    ap.add_argument("--batch", type=Path, help="Pasta com uma subpasta por peça (nome da subpasta = part-id)")
    ap.add_argument("--pending", action="store_true", help="Processa tudo o que está 'a_processar' no catálogo (fila do site) — 1 sessão de pod para todas")
    ap.add_argument("--part-id", help="Identificador da peça (obrigatório com --input)")
    ap.add_argument("--nome")
    ap.add_argument("--categoria")
    ap.add_argument("--equipamento")
    ap.add_argument("--referencia")
    ap.add_argument("--descricao")
    ap.add_argument("--quality", choices=["fast", "quality"], default="quality")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="Valida ficheiros e atualiza o catálogo sem ligar nenhum pod RunPod (para testar sem custo)")
    args = ap.parse_args()

    if not args.input and not args.batch and not args.pending:
        ap.error("Indica --input <pasta_da_peca>, --batch <pasta_com_subpastas> ou --pending")

    def com_sessao(fn):
        """Só liga um pod (sessão de lote) se não for --dry-run — dry-run não
        deve custar nada nem precisar de RUNPOD_API_KEY."""
        if args.dry_run:
            fn(None)
        else:
            with rpc.PodSession() as session:
                fn(session)

    if args.input:
        if not args.part_id:
            ap.error("--part-id é obrigatório quando se usa --input")
        meta = {
            "nome": args.nome,
            "categoria": args.categoria,
            "equipamento": args.equipamento,
            "referencia": args.referencia,
            "descricao": args.descricao,
        }
        com_sessao(lambda session: process_part(session, args.part_id, args.input, args.quality, args.seed, meta, args.dry_run))
        return

    if args.pending:
        def _run_pending(session):
            sucesso, falhas = process_pending(session, args.quality, args.seed, args.dry_run)
            print(f"Pendentes processadas: {len(sucesso)} com sucesso, {len(falhas)} com erro.")
            for part_id, erro in falhas.items():
                print(f"  [{part_id}] {erro}", file=sys.stderr)

        com_sessao(_run_pending)
        return

    # modo --batch: cada subpasta é uma peça; metadados mínimos (editar depois no parts.json)
    subfolders = sorted(p for p in args.batch.iterdir() if p.is_dir())
    if not subfolders:
        print(f"Sem subpastas em {args.batch}", file=sys.stderr)
        sys.exit(1)

    def _run_batch(session):
        for folder in subfolders:
            part_id = folder.name
            meta = {"nome": part_id.replace("-", " ").title()}
            try:
                process_part(session, part_id, folder, args.quality, args.seed, meta, args.dry_run)
            except FileNotFoundError as e:
                print(f"[{part_id}] a saltar: {e}", file=sys.stderr)

    com_sessao(_run_batch)


if __name__ == "__main__":
    main()
