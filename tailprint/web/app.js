"use strict";
const el = (id) => document.getElementById(id);
const create = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
};
const params = new URLSearchParams(location.search);
let selected = params.get("printer") || "";
let data = null,
  busy = false,
  page = "print",
  requestKey = null;
const statusNames = {
  queued: "В очереди",
  sending: "Передача",
  submitted: "В системной очереди",
  failed: "Ошибка",
  uncertain: "Проверьте очередь ОС",
  demo: "Демонстрация завершена",
};

function msg(id, text, bad = false) {
  const box = el(id);
  box.hidden = !text;
  box.textContent = text;
  box.className = bad ? "bad" : "good";
}
async function api(url, options) {
  const response = await fetch(url, options);
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("Агент не ответил. Проверьте соединение с Tailscale.");
  }
  if (!response.ok) throw new Error(result.error || "Ошибка агента.");
  return result;
}
function stack(title, caption) {
  const group = create("div", undefined, "stack");
  group.append(create("b", title), create("small", caption));
  return group;
}
function navigate(name) {
  page = name;
  for (const btn of document.querySelectorAll("[data-page]")) {
    const active = btn.dataset.page === name;
    el(btn.dataset.page).hidden = !active;
    if (active) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  if (name === "jobs") historyList();
  if (name === "admin") localList();
}
function renderPrinters() {
  if (!data) return;
  const list = document.createDocumentFragment();
  const hosts = [
    { host: data.host, printers: data.printers, local: true, online: true },
    ...data.peers,
  ];
  for (const h of hosts) {
    for (const name of h.printers) {
      const current = h.local && selected === name;
      const row = create(
        "button",
        undefined,
        "printer" + (current ? " selected" : ""),
      );
      row.type = "button";
      row.disabled = !h.online || busy;
      row.setAttribute("aria-pressed", String(!!current));
      row.append(stack(name, h.host + (!h.online ? " · узел недоступен" : "")));
      row.onclick = () => {
        if (!h.local) {
          if (
            el("file").files.length &&
            !confirm(
              "Открыть другой узел? На нём потребуется выбрать файл снова.",
            )
          )
            return;
          const dest = new URL(h.url);
          dest.searchParams.set("printer", name);
          location.assign(dest);
          return;
        }
        if (selected !== name) requestKey = null;
        selected = name;
        msg("result", "");
        renderPrinters();
      };
      list.append(row);
    }
  }
  if (!list.childElementCount) {
    list.append(
      create(
        "p",
        data.admin
          ? "Пока нет опубликованных принтеров. Откройте «Принтеры узла», чтобы добавить первый."
          : "Пока нет опубликованных принтеров. На компьютере с принтером откройте локальное управление агентом.",
        "empty",
      ),
    );
  }
  el("printers").replaceChildren(list);
  const available = data.printers.includes(selected);
  el("chosen").textContent = available
    ? selected
    : "Выберите принтер из списка";
  el("fields").disabled = !available || busy;
}
async function network() {
  try {
    const fresh = await api("/api/network");
    const changed = JSON.stringify(fresh) !== JSON.stringify(data);
    data = fresh;
    el("node").textContent = data.host;
    el("demo").hidden = !data.demo;
    el("admin-tab").hidden = !data.admin;
    el("network-error").hidden = !data.error;
    el("network-error").textContent = data.error;
    el("history-host").textContent = "Задания на " + data.host;
    if (data.admin && data.url) {
      el("client-link").hidden = false;
      el("address").href = data.url;
      el("address").textContent = data.url;
    }
    if (changed) renderPrinters();
  } catch (e) {
    el("network-error").hidden = false;
    el("network-error").textContent =
      "Не удалось обновить каталог. " + e.message;
  }
}
async function historyList() {
  try {
    const jobs = await api("/api/jobs");
    const rows = jobs.map((job) => {
      const row = create("div", undefined, "row");
      const date = new Date(job.created * 1000).toLocaleString("ru", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
      row.append(
        stack(job.name, job.printer + " · " + job.copies + " экз. · " + date),
        create(
          "span",
          [statusNames[job.state], job.detail].filter(Boolean).join(" · "),
          "status",
        ),
      );
      return row;
    });
    el("history").replaceChildren(
      ...(rows.length
        ? rows
        : [
            create(
              "p",
              "С этого устройства пока не отправляли документов на этот узел.",
              "empty",
            ),
          ]),
    );
  } catch (e) {
    el("history").textContent = e.message;
  }
}
async function localList() {
  if (!data?.admin) return;
  try {
    const { queues, published } = await api("/api/local");
    for (const name of published) {
      if (!queues.some((p) => p.name === name))
        queues.push({ name, kind: "Очередь исчезла из системы" });
    }
    const rows = queues.map((p) => {
      const enabled = published.includes(p.name);
      const row = create("div", undefined, "row");
      const btn = create(
        "button",
        enabled ? "Снять публикацию" : "Опубликовать",
        "secondary",
      );
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api("/api/publish", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Tailprint": "1" },
            body: JSON.stringify({ name: p.name, enabled: !enabled }),
          });
          msg(
            "admin-result",
            enabled ? "Публикация снята." : "Принтер опубликован в сети.",
          );
          await Promise.all([network(), localList()]);
        } catch (e) {
          msg("admin-result", e.message, true);
        } finally {
          btn.disabled = false;
        }
      };
      row.append(
        stack(p.name, p.kind + (enabled ? " · опубликован" : "")),
        btn,
      );
      return row;
    });
    el("local").replaceChildren(
      ...(rows.length
        ? rows
        : [
            create(
              "p",
              "Очередей нет. Добавьте принтер в настройках ОС и повторите поиск.",
              "empty",
            ),
          ]),
    );
  } catch (e) {
    el("local").textContent = e.message;
  }
}
function fileChanged() {
  requestKey = null;
  const file = el("file").files[0];
  el("filename").textContent = file?.name || "Выбрать документ";
  msg(
    "result",
    file && file.size > 25 * 1024 ** 2 ? "Файл превышает 25 МиБ." : "",
    true,
  );
}
el("file").onchange = fileChanged;
el("send").addEventListener("input", () => {
  requestKey = null;
});
let beforeBooklet = null;
el("booklet").onchange = () => {
  const on = el("booklet").checked;
  const locked = { orientation: "landscape", nup: "2", duplex: "short" };
  if (on)
    beforeBooklet = Object.fromEntries(
      Object.keys(locked).map((key) => [key, el(key).value]),
    );
  for (const key of Object.keys(locked)) {
    el(key).value = on ? locked[key] : beforeBooklet[key];
    el(key).disabled = on;
  }
  el("booklet-note").hidden = !on;
};
for (const type of ["dragover", "dragleave", "drop"]) {
  el("drop").addEventListener(type, (e) => {
    e.preventDefault();
    el("drop").classList.toggle("active", type === "dragover");
    if (
      type === "drop" &&
      !el("fields").disabled &&
      e.dataTransfer.files.length === 1
    ) {
      el("file").files = e.dataTransfer.files;
      fileChanged();
    }
  });
}
function uuid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join(
    "",
  );
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}
el("send").onsubmit = async (e) => {
  e.preventDefault();
  const file = el("file").files[0];
  if (busy || !file) return;
  if (file.size > 25 * 1024 ** 2)
    return msg("result", "Файл превышает 25 МиБ.", true);
  requestKey ||= uuid();
  const query = new URLSearchParams({
    id: requestKey,
    printer: selected,
    copies: el("copies").value,
    name: file.name,
  });
  busy = true;
  renderPrinters();
  msg("result", "Передаём документ…");
  try {
    const opts = {
      paper: el("paper").value,
      orientation: el("orientation").value,
      scale: el("scale").value,
      nup: Number(el("nup").value),
      duplex: el("duplex").value,
      booklet: el("booklet").checked,
      pages: el("pages").value.trim(),
    };
    if (!/^[0-9, -]*$/.test(opts.pages))
      throw new Error("Укажите страницы в виде 1-3,5.");
    await api("/api/jobs?" + query, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Tailprint": "1",
        "X-Print-Options": JSON.stringify(opts),
      },
      body: file,
    });
    el("file").value = "";
    fileChanged();
    msg("result", "Задание принято. Статус доступен в разделе «Мои задания».");
  } catch (e) {
    msg(
      "result",
      e.message + " Результат можно проверить в разделе «Мои задания».",
      true,
    );
  } finally {
    busy = false;
    renderPrinters();
  }
};
document
  .querySelectorAll("[data-page]")
  .forEach((btn) => (btn.onclick = () => navigate(btn.dataset.page)));
el("refresh").onclick = network;
el("scan").onclick = localList;
el("history-refresh").onclick = historyList;
network();
setInterval(() => {
  if (!document.hidden) {
    network();
    if (page === "jobs") historyList();
  }
}, 5000);
