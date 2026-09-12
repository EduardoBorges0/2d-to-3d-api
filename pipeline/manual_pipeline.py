"""
Pipeline: liga fotos/diagramas a legendas e referências em manuais técnicos heterogéneos.

Etapas implementadas (correspondem aos passos 1-3 do pipeline discutido):
  1. Extração estrutural: texto + imagens + posições, por página (PyMuPDF)
  2. OCR de números/legendas dentro das próprias imagens (callouts em diagramas explodidos)
  3. Ligação imagem <-> texto próximo, por proximidade espacial + padrões de legenda (Fig., Item, etc.)

Uso:
    python manual_pipeline.py caminho_para_manual.pdf
"""

import sys
import os
import re
import json
import fitz  # PyMuPDF
import pytesseract
from PIL import Image

# Padrões comuns de legendas em manuais técnicos (PT/EN) — expandir conforme
# forem aparecendo formatos novos nos manuais reais.
CAPTION_PATTERNS = [
    r"\bfig(?:ura)?\.?\s*\d+",
    r"\bfigure\s*\d+",
    r"\bitem\s*\d+",
    r"\bpe[çc]a\s*n?[ºo°]?\s*\d+",
    r"\bref\.?\s*\d+",
    r"\bdiagrama\s*\d+",
    r"\btable\s*\d+",
    r"\btabela\s*\d+",
]
CAPTION_RE = re.compile("|".join(CAPTION_PATTERNS), re.IGNORECASE)


def extract_text_blocks(page, page_num):
    """Extrai blocos de texto com a sua posição (bbox) na página."""
    blocks = []
    for b in page.get_text("blocks"):
        x0, y0, x1, y1, text, block_no, block_type = b
        text = text.strip()
        if not text:
            continue
        blocks.append({
            "page": page_num,
            "bbox": (x0, y0, x1, y1),
            "text": text,
            "is_caption_like": bool(CAPTION_RE.search(text)),
        })
    return blocks


def extract_images(doc, page, page_num, out_dir):
    """Extrai imagens raster embutidas na página, com posição real (bbox)."""
    images = []
    for img_index, img in enumerate(page.get_images(full=True)):
        xref = img[0]
        # posição real da imagem na página (pode aparecer mais de uma vez)
        rects = page.get_image_rects(xref)
        if not rects:
            continue
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha > 3:  # CMYK ou outro espaço de cor
                pix = fitz.Pixmap(fitz.csRGB, pix)
        except Exception as e:
            continue

        # filtrar imagens muito pequenas (máscaras, decoração) — ajustar limiar
        # conforme os manuais reais; 40x40pt é um ponto de partida conservador
        for rect in rects:
            if rect.width < 40 or rect.height < 40:
                continue
            fname = f"page{page_num}_img{xref}.png"
            fpath = os.path.join(out_dir, fname)
            pix.save(fpath)
            images.append({
                "page": page_num,
                "bbox": (rect.x0, rect.y0, rect.x1, rect.y1),
                "path": fpath,
            })
    return images


def ocr_callouts(image_path):
    """
    OCR sobre a própria imagem para tentar capturar números de referência
    (callouts) desenhados dentro de diagramas explodidos.
    Nota: funciona bem para números grandes/nítidos; callouts pequenos dentro
    de círculos finos tendem a falhar — sinalizado no relatório final.
    """
    try:
        img = Image.open(image_path)
        # upscale ajuda o OCR a apanhar números pequenos
        img = img.resize((img.width * 2, img.height * 2))
        text = pytesseract.image_to_string(img, config="--psm 11")
        numbers = re.findall(r"\b\d{1,3}\b", text)
        return numbers
    except Exception:
        return []


def distance(bbox_a, bbox_b):
    """Distância entre centros de duas bounding boxes (proxy simples de proximidade)."""
    ax = (bbox_a[0] + bbox_a[2]) / 2
    ay = (bbox_a[1] + bbox_a[3]) / 2
    bx = (bbox_b[0] + bbox_b[2]) / 2
    by = (bbox_b[1] + bbox_b[3]) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def link_images_to_text(images, text_blocks):
    """
    Para cada imagem, escolhe o texto mais provável de ser a sua legenda:
    prioriza blocos com padrão de legenda (Fig./Item/...) na mesma página,
    depois usa proximidade espacial como critério de desempate/fallback.
    """
    results = []
    for img in images:
        same_page = [t for t in text_blocks if t["page"] == img["page"]]
        captions = [t for t in same_page if t["is_caption_like"]]

        candidates = captions if captions else same_page
        if not candidates:
            results.append({**img, "linked_text": None, "confidence": "sem_texto_na_pagina"})
            continue

        best = min(candidates, key=lambda t: distance(img["bbox"], t["bbox"]))
        confidence = "alta_padrao_legenda" if best["is_caption_like"] else "baixa_so_proximidade"
        results.append({**img, "linked_text": best["text"], "confidence": confidence})
    return results


def run(pdf_path, out_dir="/home/claude/extracted"):
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)

    all_text_blocks = []
    all_images = []

    for page_num, page in enumerate(doc, start=1):
        all_text_blocks.extend(extract_text_blocks(page, page_num))
        all_images.extend(extract_images(doc, page, page_num, out_dir))

    linked = link_images_to_text(all_images, all_text_blocks)

    for entry in linked:
        entry["ocr_numbers_in_image"] = ocr_callouts(entry["path"])

    report = {
        "pdf": pdf_path,
        "paginas": len(doc),
        "imagens_encontradas": len(all_images),
        "blocos_texto_encontrados": len(all_text_blocks),
        "ligacoes": [
            {
                "pagina": e["page"],
                "imagem": os.path.basename(e["path"]),
                "legenda_associada": e["linked_text"],
                "confianca": e["confidence"],
                "numeros_detetados_na_imagem": e["ocr_numbers_in_image"],
            }
            for e in linked
        ],
    }
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python manual_pipeline.py caminho_para_manual.pdf")
        sys.exit(1)

    report = run(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2))
