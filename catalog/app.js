// Catálogo 3D de Peças — Almovi
// Lê catalog/data/parts.json, renderiza grid de cards com <model-viewer>,
// pesquisa, filtros e modal de detalhe. Sem build step — HTML/CSS/JS puro.

const STATE = {
  parts: [],
  filtered: [],
  search: "",
  filters: { categoria: "", equipamento: "", estado: "" },
  page: 1,
  pageSize: 10,
};

const BADGE_BY_ESTADO = {
  placeholder: { label: "Placeholder (teste)", cls: "badge-rascunho" },
  gerado: { label: "Gerado", cls: "badge-submetido" },
  aprovado: { label: "Aprovado", cls: "badge-aprovado" },
  a_processar: { label: "Pendente (lote)", cls: "badge-pendente" },
  a_editar: { label: "A aplicar edição...", cls: "badge-pendente" },
  erro: { label: "Erro", cls: "badge-rejeitado" },
};

let currentModalId = null;

let pollTimer = null;

function badgeFor(estado) {
  return BADGE_BY_ESTADO[estado] || { label: estado || "—", cls: "badge-neutro" };
}

async function loadParts() {
  const res = await fetch("data/parts.json", { cache: "no-store" });
  if (!res.ok) throw new Error("Falha ao carregar parts.json: " + res.status);
  const data = await res.json();
  STATE.parts = data.parts || [];
  populateFilterOptions();
  applyFilters();
  managePolling();
}

function managePolling() {
  const hasPending = STATE.parts.some((p) => p.estado === "a_processar" || p.estado === "a_editar");
  if (hasPending && !pollTimer) {
    pollTimer = setInterval(async () => {
      try {
        await loadPartsQuiet();
      } catch (err) {
        console.error(err);
      }
    }, 4000);
  } else if (!hasPending && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function loadPartsQuiet() {
  const res = await fetch("data/parts.json", { cache: "no-store" });
  if (!res.ok) return;
  const data = await res.json();
  const before = new Map(STATE.parts.map((p) => [p.id, p.estado]));
  STATE.parts = data.parts || [];
  populateFilterOptions();
  applyFilters();
  managePolling();
  STATE.parts.forEach((p) => {
    const prev = before.get(p.id);
    if (prev === "a_processar" && p.estado === "gerado") {
      toast(`"${p.nome}" foi gerada com sucesso.`, "sucesso");
    } else if (prev === "a_processar" && p.estado === "erro") {
      toast(`Falha ao gerar "${p.nome}". Ver detalhe na peça.`, "erro");
    } else if (prev === "a_editar" && p.estado === "gerado") {
      toast(`Edição aplicada a "${p.nome}".`, "sucesso");
    } else if (prev === "a_editar" && p.estado === "erro") {
      toast(`Não foi possível aplicar a edição a "${p.nome}". Ver detalhe na peça.`, "erro");
    }
  });
  if (currentModalId && before.get(currentModalId) && before.get(currentModalId) !== STATE.parts.find((p) => p.id === currentModalId)?.estado) {
    openModal(currentModalId);
  }
}

function populateFilterOptions() {
  const categorias = [...new Set(STATE.parts.map((p) => p.categoria).filter(Boolean))].sort();
  const equipamentos = [...new Set(STATE.parts.map((p) => p.equipamento).filter(Boolean))].sort();

  fillSelect("filter-categoria", categorias);
  fillSelect("filter-equipamento", equipamentos);
  fillDatalist("categorias-list", categorias);
  fillDatalist("equipamentos-list", equipamentos);
}

function fillDatalist(id, values) {
  const el = document.getElementById(id);
  el.innerHTML = values.map((v) => `<option value="${escapeHtml(v)}"></option>`).join("");
}

function fillSelect(id, values) {
  const el = document.getElementById(id);
  const current = el.value;
  el.innerHTML = `<option value="">Todas</option>` + values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  el.value = current;
}

function applyFilters() {
  const q = STATE.search.trim().toLowerCase();
  STATE.filtered = STATE.parts.filter((p) => {
    if (STATE.filters.categoria && p.categoria !== STATE.filters.categoria) return false;
    if (STATE.filters.equipamento && p.equipamento !== STATE.filters.equipamento) return false;
    if (STATE.filters.estado && p.estado !== STATE.filters.estado) return false;
    if (q) {
      const haystack = `${p.nome} ${p.referencia} ${p.categoria} ${p.equipamento}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
  STATE.page = 1;
  updateFilterDot();
  render();
}

function updateFilterDot() {
  const hasFilters = !!(STATE.filters.categoria || STATE.filters.equipamento || STATE.filters.estado);
  document.getElementById("filter-toggle").classList.toggle("has-filters", hasFilters);
}

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function render() {
  const grid = document.getElementById("grid");
  const empty = document.getElementById("empty");

  const start = (STATE.page - 1) * STATE.pageSize;
  const pageItems = STATE.filtered.slice(start, start + STATE.pageSize);

  if (STATE.filtered.length === 0) {
    grid.innerHTML = "";
    empty.hidden = false;
  } else {
    empty.hidden = true;
    grid.innerHTML = pageItems.map(cardTemplate).join("");
    grid.querySelectorAll(".card").forEach((cardEl) => {
      cardEl.addEventListener("click", (e) => {
        // evita abrir o modal quando o utilizador está a interagir com o próprio model-viewer (arrastar)
        if (e.target.closest("model-viewer") && e.detail === 0) return;
        openModal(cardEl.dataset.id);
      });
    });
  }

  renderPagination();
}

function cardMediaTemplate(p) {
  if (p.modelo_glb) {
    return `<model-viewer src="${escapeHtml(p.modelo_glb)}" camera-controls disable-zoom shadow-intensity="0.8" auto-rotate rotation-per-second="20deg" alt="${escapeHtml(p.nome)}"></model-viewer>`;
  }
  if (p.estado === "erro") {
    return `<div class="model-placeholder"><span class="ico-erro">⚠</span><span>Falha ao gerar modelo</span></div>`;
  }
  return `<div class="model-placeholder"><span class="ico-pendente">⏳</span><span>Pendente — aguarda o próximo processamento em lote</span></div>`;
}

function cardTemplate(p) {
  const badge = badgeFor(p.estado);
  return `
    <div class="card" data-id="${escapeHtml(p.id)}">
      ${cardMediaTemplate(p)}
      <div class="card-body">
        <div class="card-title">${escapeHtml(p.nome)}</div>
        <div class="card-meta">${escapeHtml(p.referencia)} · ${escapeHtml(p.equipamento)}</div>
        <div class="card-badges">
          <span class="badge badge-neutro">${escapeHtml(p.categoria)}</span>
          <span class="badge ${badge.cls}">${badge.label}</span>
        </div>
      </div>
    </div>
  `;
}

function renderPagination() {
  const total = STATE.filtered.length;
  const start = total === 0 ? 0 : (STATE.page - 1) * STATE.pageSize + 1;
  const end = Math.min(STATE.page * STATE.pageSize, total);
  document.getElementById("pagination-info").textContent = `A mostrar ${start}-${end} de ${total}`;
  document.getElementById("prev-page").disabled = STATE.page <= 1;
  document.getElementById("next-page").disabled = end >= total;
}

function editHistoricoItemTemplate(item) {
  const estadoLabel = item.estado === "sucesso" ? "aplicada" : "falhou";
  const estadoCls = item.estado === "sucesso" ? "edit-ok" : "edit-erro";
  const detalhe = item.estado === "sucesso" ? "" : ` — ${escapeHtml(item.erro || "")}`;
  return `
    <div class="edit-historico-item">
      <span class="edit-historico-texto">"${escapeHtml(item.instrucao)}"</span>
      <span class="${estadoCls}">${estadoLabel}${detalhe}</span>
    </div>
  `;
}

function renderEditBloco(p) {
  const bloco = document.getElementById("modal-edit-bloco");
  const input = document.getElementById("modal-edit-input");
  const submitBtn = document.getElementById("modal-edit-submit");
  const historicoEl = document.getElementById("modal-edit-historico");

  const podeEditar = !!p.modelo_glb && p.estado !== "a_processar" && p.estado !== "a_editar";
  bloco.style.display = p.modelo_glb ? "" : "none";
  input.disabled = !podeEditar;
  submitBtn.disabled = !podeEditar;
  submitBtn.classList.toggle("is-loading", p.estado === "a_editar");
  input.value = "";

  const historico = p.historico_edicoes || [];
  historicoEl.innerHTML = historico.length
    ? historico.map(editHistoricoItemTemplate).join("")
    : "";
}

function openModal(id) {
  const p = STATE.parts.find((x) => x.id === id);
  if (!p) return;
  currentModalId = id;
  document.getElementById("modal-title").textContent = p.nome;
  document.getElementById("modal-ref").textContent = p.referencia || "—";
  const badge = badgeFor(p.estado);
  document.getElementById("modal-estado").innerHTML = `<span class="badge ${badge.cls}">${badge.label}</span>`;
  document.getElementById("modal-categoria").textContent = p.categoria || "—";
  document.getElementById("modal-equipamento").textContent = p.equipamento || "—";
  let desc = p.descricao || "Sem descrição.";
  if (p.estado === "erro" && p.erro) {
    desc += `\n\nErro do pipeline: ${p.erro}`;
  }
  document.getElementById("modal-desc").textContent = desc;

  const mount = document.getElementById("modal-viewer-mount");
  const download = document.getElementById("modal-download");
  if (p.modelo_glb) {
    mount.innerHTML = `<model-viewer id="modal-viewer" camera-controls auto-rotate shadow-intensity="1" exposure="1" alt="Modelo 3D da peça" src="${escapeHtml(p.modelo_glb)}"></model-viewer>`;
    download.style.display = "";
    download.href = p.modelo_glb;
    download.setAttribute("download", (p.referencia || p.id) + ".glb");
  } else {
    const placeholderHtml = p.estado === "erro"
      ? `<div class="model-placeholder"><span class="ico-erro">⚠</span><span>Falha ao gerar modelo</span></div>`
      : `<div class="model-placeholder"><span class="ico-pendente">⏳</span><span>Pendente — aguarda o próximo processamento em lote (botão "Processar pendentes")</span></div>`;
    mount.innerHTML = placeholderHtml;
    download.style.display = "none";
  }
  renderEditBloco(p);
  document.getElementById("modal-overlay").classList.add("open");
}

function closeModal() {
  document.getElementById("modal-overlay").classList.remove("open");
  document.getElementById("modal-viewer-mount").innerHTML = "";
  currentModalId = null;
}

async function submitEdit() {
  if (!currentModalId) return;
  const input = document.getElementById("modal-edit-input");
  const instrucao = input.value.trim();
  if (!instrucao) return;

  const submitBtn = document.getElementById("modal-edit-submit");
  submitBtn.disabled = true;
  submitBtn.classList.add("is-loading");

  try {
    const res = await fetch(`/api/parts/${encodeURIComponent(currentModalId)}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instrucao }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(body.error || "Não foi possível aplicar a edição.", "erro");
      submitBtn.disabled = false;
      submitBtn.classList.remove("is-loading");
      return;
    }
    toast("Edição em curso...", "info");
    await loadPartsQuiet();
  } catch (err) {
    console.error(err);
    toast("Não foi possível aplicar a edição. Confirma que o servidor está a correr.", "erro");
    submitBtn.disabled = false;
    submitBtn.classList.remove("is-loading");
  }
}

function toast(message, type = "info") {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  stack.appendChild(el);
  while (stack.children.length > 3) stack.removeChild(stack.firstChild);
  setTimeout(() => el.remove(), 4000);
}

function openAddModal() {
  document.getElementById("add-part-form").reset();
  ["field-nome", "field-fotos"].forEach((id) => document.getElementById(id).classList.remove("error"));
  document.getElementById("add-modal-overlay").classList.add("open");
  document.getElementById("add-nome").focus();
}

function closeAddModal() {
  document.getElementById("add-modal-overlay").classList.remove("open");
}

function validateField(fieldId, isValid) {
  document.getElementById(fieldId).classList.toggle("error", !isValid);
  return isValid;
}

function validateAddForm() {
  const nomeOk = validateField("field-nome", document.getElementById("add-nome").value.trim().length > 0);
  const fotosOk = validateField("field-fotos", document.getElementById("add-fotos").files.length > 0);
  return nomeOk && fotosOk;
}

async function submitAddForm() {
  if (!validateAddForm()) return;

  const form = document.getElementById("add-part-form");
  const formData = new FormData(form);
  const submitBtn = document.getElementById("add-submit");
  submitBtn.disabled = true;
  submitBtn.classList.add("is-loading");

  try {
    const res = await fetch("/api/parts", { method: "POST", body: formData });
    if (res.status === 400) {
      const body = await res.json();
      if (body.errors?.nome) validateField("field-nome", false);
      if (body.errors?.fotos) validateField("field-fotos", false);
      const msg = Object.values(body.errors || {}).join(" ") || "Dados inválidos.";
      toast(msg, "erro");
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);

    closeAddModal();
    toast("Peça adicionada à fila de processamento.", "info");
    await loadParts();
  } catch (err) {
    console.error(err);
    toast("Não foi possível criar a peça. Confirma que o servidor (server.py) está a correr.", "erro");
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove("is-loading");
  }
}

function bindEvents() {
  document.getElementById("search-input").addEventListener("input", (e) => {
    STATE.search = e.target.value;
    applyFilters();
  });

  document.getElementById("filter-toggle").addEventListener("click", () => {
    document.getElementById("filter-drawer").classList.add("open");
    document.getElementById("drawer-overlay").classList.add("open");
  });
  const closeDrawer = () => {
    document.getElementById("filter-drawer").classList.remove("open");
    document.getElementById("drawer-overlay").classList.remove("open");
  };
  document.getElementById("filter-close").addEventListener("click", closeDrawer);
  document.getElementById("drawer-overlay").addEventListener("click", closeDrawer);

  document.getElementById("filter-apply").addEventListener("click", () => {
    STATE.filters.categoria = document.getElementById("filter-categoria").value;
    STATE.filters.equipamento = document.getElementById("filter-equipamento").value;
    STATE.filters.estado = document.getElementById("filter-estado").value;
    applyFilters();
    closeDrawer();
  });

  document.getElementById("filter-clear").addEventListener("click", () => {
    document.getElementById("filter-categoria").value = "";
    document.getElementById("filter-equipamento").value = "";
    document.getElementById("filter-estado").value = "";
    STATE.filters = { categoria: "", equipamento: "", estado: "" };
    applyFilters();
  });

  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-fechar-btn").addEventListener("click", closeModal);
  document.getElementById("modal-edit-submit").addEventListener("click", submitEdit);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      closeAddModal();
    }
  });

  document.getElementById("prev-page").addEventListener("click", () => {
    if (STATE.page > 1) { STATE.page -= 1; render(); }
  });
  document.getElementById("next-page").addEventListener("click", () => {
    STATE.page += 1; render();
  });

  document.getElementById("btn-add-part").addEventListener("click", openAddModal);
  document.getElementById("add-modal-close").addEventListener("click", closeAddModal);
  document.getElementById("add-cancel").addEventListener("click", closeAddModal);
  document.getElementById("add-modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "add-modal-overlay") closeAddModal();
  });
  document.getElementById("add-nome").addEventListener("blur", () => {
    validateField("field-nome", document.getElementById("add-nome").value.trim().length > 0);
  });
  document.getElementById("add-fotos").addEventListener("blur", () => {
    validateField("field-fotos", document.getElementById("add-fotos").files.length > 0);
  });
  document.getElementById("add-part-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitAddForm();
  });

  document.getElementById("btn-refresh").addEventListener("click", async () => {
    try {
      await loadParts();
      toast("Catálogo atualizado.", "sucesso");
    } catch (err) {
      toast("Erro ao atualizar catálogo.", "erro");
      console.error(err);
    }
  });

  document.getElementById("btn-process-pending").addEventListener("click", async () => {
    const btn = document.getElementById("btn-process-pending");
    btn.disabled = true;
    btn.classList.add("is-loading");
    try {
      const res = await fetch("/api/parts/process-pending", { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(body.error || "Não foi possível iniciar o processamento.", "aviso");
        return;
      }
      toast(`A processar ${body.n_pendentes} peça(s) pendente(s) — 1 sessão RunPod para todas.`, "info");
      await loadPartsQuiet();
    } catch (err) {
      console.error(err);
      toast("Não foi possível contactar o servidor.", "erro");
    } finally {
      btn.disabled = false;
      btn.classList.remove("is-loading");
    }
  });
}

bindEvents();
loadParts().catch((err) => {
  console.error(err);
  toast("Não foi possível carregar o catálogo (parts.json).", "erro");
});
