// Free local cover preparation; API calls are authenticated by Telegram initData.
function openCoverEditor(url, vid) {
  if (!url) return alertMsg("Вставь ссылку на видео.");
  document.getElementById("coverEditor")?.remove();
  const projectId = pid(), originalTitle = window._top?.[vid]?.title || "";
  const base = `/api/projects/${projectId}/covers`;
  const dialog = document.createElement("dialog");
  dialog.id = "coverEditor";
  dialog.style.cssText = "width:min(600px,96vw);max-height:92dvh;overflow:auto;border:1px solid var(--sep);border-radius:18px;background:var(--bg);color:var(--text);padding:18px";
  dialog.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between"><h1>Подготовить ролик</h1><button class="ghost" data-close aria-label="Закрыть">✕</button></div>
    <label>Название ролика<input id="cvTitle" maxlength="100" placeholder="Оставь пустым — название из источника" style="width:100%;margin:8px 0 16px"></label>
    <label>Обложка<select id="cvMode" style="width:100%;margin:8px 0">
      <option value="project">По настройкам проекта</option><option value="selected">Выбрать свою</option><option value="off">Без обложки</option></select></label>
    <p class="sub">Локальная обработка · 0 AI-токенов. Для автообложки используется название ролика.</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><button id="cvGenerate">Создать 3 варианта</button><button id="cvUpload" class="ghost">Загрузить картинку</button></div>
    <input type="file" id="cvFile" accept="image/jpeg,image/png,image/webp" hidden>
    <div id="cvControls" hidden style="margin-top:14px">
      <label>Надпись на обложке<input id="cvText" maxlength="100" style="width:100%;margin:8px 0"></label>
      <div style="display:flex;gap:8px;flex-wrap:wrap"><select id="cvStyle" aria-label="Стиль">
        <option value="lemon">Лимон</option><option value="ocean">Океан</option><option value="coral">Коралл</option>
      </select><button id="cvRefresh" class="ghost">Обновить надпись</button></div>
    </div>
    <p id="cvStatus" class="sub" role="status" aria-live="polite"></p>
    <div id="cvImages" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px"></div>
    <p class="sub">Если YouTube отклонит обложку, бот пришлёт JPG для ручной установки. Видео повторно загружаться не будет.</p>
    <label>Когда<select id="cvWhen" style="width:100%;margin:8px 0"><option value="now">Сейчас</option><option value="next">В ближайший слот</option><option value="at">Указать время</option></select></label>
    <input id="cvAt" type="datetime-local" hidden style="width:100%;margin-bottom:12px">
    <button id="cvPublish" class="wide">Опубликовать</button>`;
  document.body.appendChild(dialog);
  const el = id => dialog.querySelector("#cv" + id);
  el("Title").value = originalTitle;
  el("Style").value = state.project.cover_style || "lemon";
  el("At").value = state.now;
  el("At").min = state.now;
  let draft = null, selected = null, busy = false, timer = null, revision = "";
  const alive = () => dialog.isConnected && dialog.open;
  const status = message => { el("Status").textContent = message; };
  const signature = () => JSON.stringify([el("Text").value, el("Style").value]);
  const controls = () => {
    el("Generate").disabled = busy;
    el("Upload").disabled = busy;
    el("Refresh").disabled = busy;
    el("Publish").disabled = busy || (el("Mode").value === "selected" &&
      (selected === null || (selected !== "custom" && revision !== signature())));
  };
  const close = () => { clearTimeout(timer); dialog.close(); dialog.remove(); };
  dialog.querySelector("[data-close]").onclick = close;
  dialog.addEventListener("close", () => { clearTimeout(timer); dialog.remove(); });
  el("Mode").onchange = controls;
  el("When").onchange = () => { el("At").hidden = el("When").value !== "at"; };
  for (const id of ["Text", "Style"]) el(id).addEventListener("input", () => {
    if (selected !== "custom") status("Нажми «Обновить надпись», чтобы увидеть изменения.");
    controls();
  });
  const showImages = (images, custom = false) => {
    const box = el("Images"); box.replaceChildren(); selected = null;
    images.forEach((src, i) => {
      const button = document.createElement("button");
      button.style.cssText = "padding:3px;background:var(--card);border:3px solid transparent;border-radius:12px";
      button.setAttribute("aria-label", custom ? "Выбрать свою обложку" : `Выбрать вариант ${i + 1}`);
      const img = document.createElement("img"); img.src = src; img.alt = custom ? "Своя обложка" : `Обложка ${i + 1}`;
      img.style.cssText = "width:100%;display:block;border-radius:6px;aspect-ratio:9/16;object-fit:contain";
      button.appendChild(img);
      button.onclick = () => {
        selected = custom ? "custom" : i; el("Mode").value = "selected";
        for (const b of box.children) { b.style.borderColor = "transparent"; b.setAttribute("aria-pressed", "false"); }
        button.style.borderColor = "var(--accent)"; button.setAttribute("aria-pressed", "true");
        status(custom ? "Своя картинка выбрана." : `Выбран вариант ${i + 1}.`); controls();
      };
      box.appendChild(button);
    });
    box.firstChild?.click();
  };
  async function refresh() {
    if (!draft || busy) return;
    busy = true; controls(); status("Оформляю обложки…");
    const requested = signature();
    try {
      const result = await api("POST", `${base}/${draft}/preview`, {text:el("Text").value, style:el("Style").value});
      if (!alive()) return;
      revision = requested; showImages(result.images);
    } catch (e) { if (alive()) status(e.message); }
    finally { busy = false; if (alive()) controls(); }
  }
  async function poll() {
    if (!alive()) return;
    try {
      const data = await api("GET", `${base}/${draft}`);
      if (!alive()) return;
      if (data.status === "failed") throw new Error(data.error);
      if (data.status === "ready") {
        if (!el("Title").value) el("Title").value = data.title;
        el("Text").value = data.text; el("Controls").hidden = false;
        busy = false; await refresh(); return;
      }
      status(data.status === "queued" ? "В очереди на подготовку…" : "Скачиваю ролик и выбираю кадры…");
      timer = setTimeout(poll, 1800);
    } catch (e) { busy = false; status(e.message); controls(); }
  }
  el("Generate").onclick = async () => {
    busy = true; selected = null; el("Mode").value = "selected"; controls(); status("Готовлю варианты…");
    try {
      const data = await api("POST", base, {video_url:url, title:el("Title").value});
      draft = data.id; if (alive()) await poll();
    } catch (e) { busy = false; status(e.message); controls(); }
  };
  el("Refresh").onclick = refresh;
  el("Upload").onclick = () => el("File").click();
  el("File").onchange = async () => {
    const file = el("File").files[0]; if (!file) return;
    if (file.size > 8 * 1024 * 1024) return status("Выбери картинку до 8 МБ.");
    busy = true; controls(); status("Загружаю картинку…");
    try {
      if (!draft) draft = (await api("POST", base, {video_url:url, title:el("Title").value, custom_only:true})).id;
      const response = await fetch(`${base}/${draft}/image`, {method:"POST", headers:{"X-Init-Data":tg?.initData || ""}, body:file});
      const data = await response.json(); if (!response.ok) throw new Error(data.error || "Не удалось загрузить картинку.");
      if (alive()) { showImages([data.image], true); el("Controls").hidden = true; }
    } catch(e) { if (alive()) status(e.message); }
    finally { busy = false; if (alive()) controls(); }
  };
  el("Publish").onclick = async () => {
    const when = el("When").value === "at" ? el("At").value : el("When").value;
    if (!when) return status("Укажи время публикации.");
    busy = true; controls();
    try {
      const result = await api("POST", `/api/projects/${projectId}/publish`, {
        video_url:url, title:originalTitle, publication_title:el("Title").value, when,
        cover_choice:el("Mode").value, cover_id:draft, cover_index:selected,
        text:el("Text").value, style:el("Style").value
      });
      close(); haptic("success"); toast(when === "now" ? "Ролик в очереди — результат придёт в чат" : "Запланировано на " + result.at);
      if (pid() === projectId) reload();
    } catch(e) { if (alive()) status(e.message); }
    finally { busy = false; if (alive()) controls(); }
  };
  dialog.showModal(); controls();
}
