"""Edição paramétrica de peças por texto (ex: "adicionar buraco de chaveiro 50mm").

Isto NÃO é edição 3D generativa — para pedidos com dimensão exata, um modelo
de difusão não dá precisão dimensional. Em vez disso:

  1. Um LLM (Claude) interpreta o texto como UMA operação CAD estruturada e
     fechada: furo (hole) ou saliência (boss) cilíndricos, com diâmetro e
     face. Se o texto não mapear claramente para isto, devolve erro em vez de
     inventar valores — a peça não é tocada.
  2. A posição na peça é resolvida por heurística geométrica (bounding box +
     face pedida), não por o utilizador clicar num ponto — limitação aceite:
     ver README.md.
  3. Aplica-se uma operação booleana real (trimesh + engine "manifold") sobre
     a malha existente e exporta-se uma NOVA versão do .glb (nunca sobrescreve
     a anterior).

Convenção de eixos assumida: Z-up (confirmado nos modelos deste catálogo — o eixo
"vertical" das peças exportadas é Z, não Y como seria o Y-up por omissão do glTF):
    topo/fundo    -> eixo Z (+/-)
    frente/tras   -> eixo Y (+/-)
    esquerda/direita -> eixo X (+/-)
"""

from pathlib import Path

import numpy as np
import trimesh
from dotenv import load_dotenv

import trellis_pipeline as tp

REPO_ROOT = tp.REPO_ROOT
MODELS_DIR = tp.MODELS_DIR

load_dotenv(REPO_ROOT / ".env")

MODELO_LLM = "claude-haiku-4-5-20251001"

TOOL_SCHEMA = {
    "name": "extrair_operacao_cad",
    "description": (
        "Extrai uma operação CAD paramétrica (furo ou saliência cilíndrica) de uma "
        "instrução em linguagem natural sobre uma peça 3D existente."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "suportado": {
                "type": "boolean",
                "description": (
                    "false se a instrução não corresponder claramente a um furo ou "
                    "saliência cilíndrica com diâmetro conhecido (edição livre/orgânica, "
                    "ou falta a dimensão) — nesse caso não adivinhar valores."
                ),
            },
            "motivo_nao_suportado": {"type": "string"},
            "operacao": {"type": "string", "enum": ["hole", "boss"]},
            "diametro_mm": {"type": "number"},
            "profundidade_mm": {
                "type": "number",
                "description": "Profundidade (furo cego) ou altura (saliência) em mm. Omitir para furo passante.",
            },
            "face": {"type": "string", "enum": ["topo", "fundo", "frente", "tras", "esquerda", "direita"]},
            "posicao_na_face": {
                "type": "string",
                "enum": ["centro", "canto_sup_esq", "canto_sup_dir", "canto_inf_esq", "canto_inf_dir"],
            },
        },
        "required": ["suportado"],
    },
}

AXIS_BY_FACE = {
    "topo": (2, 1),
    "fundo": (2, -1),
    "frente": (1, 1),
    "tras": (1, -1),
    "direita": (0, 1),
    "esquerda": (0, -1),
}

CORNER_SIGNS = {
    "canto_sup_esq": (1, 1),
    "canto_sup_dir": (1, -1),
    "canto_inf_esq": (-1, 1),
    "canto_inf_dir": (-1, -1),
    "centro": (0, 0),
}

FACE_INSET = 0.2  # margem para não colocar o furo mesmo em cima da aresta


class EditError(ValueError):
    """Erro esperado (instrução ambígua, peça sem modelo, booleana falhada) —
    não é um bug, é reportado ao utilizador tal e qual."""


def parse_instruction(texto: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODELO_LLM,
        max_tokens=512,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "extrair_operacao_cad"},
        messages=[{
            "role": "user",
            "content": (
                f'Instrução do utilizador sobre uma peça 3D existente: "{texto}"\n\n'
                "Se não for um pedido claro de furo (hole) ou saliência cilíndrica (boss) "
                "com diâmetro definido, marca suportado=false e explica porquê em "
                "motivo_nao_suportado, em vez de adivinhar valores."
            ),
        }],
    )

    tool_use = next(b for b in msg.content if b.type == "tool_use")
    result = tool_use.input

    if not result.get("suportado"):
        raise EditError(result.get("motivo_nao_suportado") or "Instrução não reconhecida como furo/saliência.")

    em_falta = [k for k in ("operacao", "diametro_mm", "face", "posicao_na_face") if not result.get(k)]
    if em_falta:
        raise EditError(f"Faltam parâmetros na instrução: {', '.join(em_falta)}.")

    return result


def resolve_placement(mesh: trimesh.Trimesh, face: str, posicao_na_face: str):
    """Devolve (ponto_na_superficie, normal_unitaria) para a face/posição pedidas,
    a partir da bounding box da peça."""
    axis, sign = AXIS_BY_FACE[face]
    lo, hi = mesh.bounds[0].copy(), mesh.bounds[1].copy()

    point = (lo + hi) / 2.0
    point[axis] = hi[axis] if sign > 0 else lo[axis]

    normal = np.zeros(3)
    normal[axis] = float(sign)

    other_axes = [a for a in range(3) if a != axis]
    s0, s1 = CORNER_SIGNS.get(posicao_na_face, (0, 0))
    for a, s in zip(other_axes, (s0, s1)):
        half = (hi[a] - lo[a]) / 2.0
        point[a] = (lo[a] + hi[a]) / 2.0 + s * half * (1 - FACE_INSET)

    return point, normal


def _check_effective_change(mesh: trimesh.Trimesh, result: trimesh.Trimesh, cilindro: trimesh.Trimesh, op_label: str):
    """A posição da operação é uma heurística (bounding box) e pode cair fora do
    material real em peças não-convexas (ex: um suporte em L). Nesse caso a
    booleana "sucede" tecnicamente mas devolve a peça inalterada — isto tem de
    ser um erro explícito, não um sucesso silencioso sem efeito visível."""
    delta = abs(result.volume - mesh.volume)
    if delta < cilindro.volume * 0.05:
        raise EditError(
            f"A posição estimada para '{op_label}' não intersectou material suficiente da "
            "peça (a heurística de posição/face pode não corresponder à geometria real, "
            "que pode não ser um bloco simples). Tenta descrever melhor a face/posição."
        )


MM_PER_UNIT = 1000.0  # assume mesh em metros (glTF/GLB) — diametro_mm/profundidade_mm vêm em mm


def apply_operation(mesh: trimesh.Trimesh, op: dict, point: np.ndarray, normal: np.ndarray) -> trimesh.Trimesh:
    radius = (op["diametro_mm"] / MM_PER_UNIT) / 2.0
    profundidade_pedida = op.get("profundidade_mm")
    profundidade_m = (profundidade_pedida / MM_PER_UNIT) if profundidade_pedida else None
    thickness_along_normal = float(np.dot(mesh.extents, np.abs(normal))) or 1.0

    rotation = trimesh.geometry.align_vectors([0.0, 0.0, 1.0], normal)

    if op["operacao"] == "hole":
        # altura generosa (1.5x a espessura da peça nesse eixo) para garantir
        # que o furo atravessa por completo, a não ser que tenha sido pedida
        # uma profundidade explícita (furo cego)
        altura = profundidade_m or thickness_along_normal * 1.5
        cilindro = trimesh.creation.cylinder(radius=radius, height=altura, sections=48)
        cilindro.apply_transform(rotation)
        cilindro.apply_translation(point)
        try:
            result = trimesh.boolean.difference([mesh, cilindro], engine="manifold")
        except Exception as e:
            raise EditError(f"Falha na operação booleana (furo): {e}") from e
        _check_effective_change(mesh, result, cilindro, "furo")
        return result

    # boss: saliência que se funde na superfície — metade embutida, metade saliente
    altura = profundidade_m or radius * 2
    embutimento = min(altura, thickness_along_normal) * 0.15
    cilindro = trimesh.creation.cylinder(radius=radius, height=altura, sections=48)
    cilindro.apply_transform(rotation)
    cilindro.apply_translation(point + normal * (altura / 2.0 - embutimento))
    try:
        result = trimesh.boolean.union([mesh, cilindro], engine="manifold")
    except Exception as e:
        raise EditError(f"Falha na operação booleana (saliência): {e}") from e
    _check_effective_change(mesh, result, cilindro, "saliência")
    return result


def edit_part(part_id: str, instrucao: str) -> str:
    """Aplica uma instrução de texto a uma peça já gerada. Devolve o caminho
    relativo (a partir da raiz do repo) do novo .glb. Lança EditError em casos
    esperados (instrução ambígua, peça sem modelo, booleana falhada) — quem
    chama (server.py) apanha e regista via trellis_pipeline.record_edit."""
    catalog = tp.load_catalog()
    entry = next((p for p in catalog.get("parts", []) if p["id"] == part_id), None)
    if entry is None:
        raise EditError(f"Peça '{part_id}' não existe no catálogo.")
    if not entry.get("modelo_glb"):
        raise EditError("Peça ainda não tem modelo 3D gerado.")

    op = parse_instruction(instrucao)

    glb_path = REPO_ROOT / entry["modelo_glb"]
    mesh = trimesh.load(str(glb_path), force="mesh")

    point, normal = resolve_placement(mesh, op["face"], op["posicao_na_face"])
    novo_mesh = apply_operation(mesh, op, point, normal)

    versao = len(entry.get("historico_edicoes", [])) + 2
    novo_glb_path = MODELS_DIR / f"{part_id}_v{versao}.glb"
    novo_mesh.export(str(novo_glb_path))

    return f"models/{novo_glb_path.name}"
