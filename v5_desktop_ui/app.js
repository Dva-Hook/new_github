(() => {
  const app = document.getElementById("app");
  const sideRail = document.getElementById("side-rail");
  const railScrim = document.getElementById("rail-scrim");
  const detailScrim = document.getElementById("detail-scrim");
  const taskDetail = document.getElementById("task-detail");
  const menuToggle = document.getElementById("menu-toggle");
  const themeToggle = document.getElementById("theme-toggle");
  const themeCheckbox = document.querySelector("[data-theme-checkbox]");
  const runDialog = document.getElementById("run-dialog");
  const toastRegion = document.getElementById("toast-region");

  const productMeta = {
    registration: {
      title: "注册中心",
      subtitle: "Registration",
      icon: "user-round-plus",
    },
    "ban-lookup": {
      title: "封禁查询",
      subtitle: "Ban Lookup",
      icon: "shield-search",
    },
    "phone-binding": {
      title: "绑定手机",
      subtitle: "Phone Binding",
      icon: "smartphone",
    },
  };

  const viewTitles = {
    overview: "任务总览",
    "new-run": "创建任务",
    runs: "运行记录",
    resources: "资源管理",
    results: "结果中心",
    logs: "实时日志",
    settings: "设置",
  };

  const solverLabels = {
    v11: "本地 V11",
    yescaptcha: "YesCaptcha",
    capmonster: "CapMonster",
  };

  const batches = {
    "30263221670": {
      status: "running",
      statusLabel: "运行中",
      statusIcon: "loader-circle",
      total: 256,
      completed: 174,
      success: 142,
      failed: 19,
      retry: 13,
      traffic: "101.3 MiB",
      started: "今天 20:31",
      solver: "CapMonster",
      browser: "RuyiPage",
      network: "代理 + 分流",
      country: "USA",
      email: "指定邮箱池",
      timeline: [
        ["20:48:45", "注册成功", "账号结果已写入批次输出", "success"],
        ["20:48:27", "求解完成", "Arkose token 已返回", ""],
        ["20:48:12", "任务启动", "Worker #12 获取任务", ""],
      ],
    },
    "30252060886": {
      status: "success",
      statusLabel: "已完成",
      statusIcon: "check",
      total: 256,
      completed: 256,
      success: 168,
      failed: 78,
      retry: 10,
      traffic: "309.9 MiB",
      started: "昨天 18:42",
      solver: "CapMonster",
      browser: "RuyiPage",
      network: "代理 + 分流",
      country: "USA",
      email: "指定邮箱池",
      timeline: [
        ["19:04:18", "批次完成", "256 个任务已全部结束", "success"],
        ["18:57:36", "邮箱验证完成", "验证结果已归档", ""],
        ["18:42:02", "任务启动", "20 个 Worker 已分配", ""],
      ],
    },
    "30241488219": {
      status: "success",
      statusLabel: "已完成",
      statusIcon: "check",
      total: 64,
      completed: 64,
      success: 50,
      failed: 11,
      retry: 3,
      traffic: "41.8 MiB",
      started: "7 月 26 日 14:08",
      solver: "本地 V11",
      browser: "RuyiPage",
      network: "直连",
      country: "GBR",
      email: "虚拟邮箱",
      timeline: [
        ["14:19:41", "批次完成", "本地模型推理服务已释放", "success"],
        ["14:10:24", "V11 模型加载", "CUDA Fast 模式可用", ""],
        ["14:08:03", "任务启动", "并发设置为 20", ""],
      ],
    },
    "30231176402": {
      status: "danger",
      statusLabel: "失败",
      statusIcon: "x",
      total: 20,
      completed: 7,
      success: 7,
      failed: 13,
      retry: 0,
      traffic: "12.6 MiB",
      started: "7 月 25 日 09:17",
      solver: "YesCaptcha",
      browser: "CloakBrowser",
      network: "代理",
      country: "USA",
      email: "指定邮箱池",
      timeline: [
        ["09:24:16", "批次终止", "连续失败超过阈值", "danger"],
        ["09:22:40", "代理连接异常", "Worker #03 重试失败", "danger"],
        ["09:17:08", "任务启动", "6 个 Worker 已分配", ""],
      ],
    },
  };

  const savedTheme = localStorage.getItem("v5-suite-theme");
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const state = {
    product: "registration",
    view: "overview",
    theme: savedTheme || (systemDark ? "dark" : "light"),
    railExpanded: false,
    detailOpen: false,
    selectedPreset: "balanced",
    selectedBatchId: "30263221670",
    solver: "capmonster",
    network: "proxy",
  };

  function renderIcons() {
    if (window.lucide) {
      window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
    }
  }

  function updateHash() {
    const hash = state.product === "registration"
      ? `${state.product}/${state.view}`
      : state.product;
    history.replaceState(null, "", `#${hash}`);
  }

  function setTheme(theme, persist = true) {
    state.theme = theme;
    document.documentElement.dataset.theme = theme;
    app.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    if (persist) localStorage.setItem("v5-suite-theme", theme);
    if (themeCheckbox) themeCheckbox.checked = theme === "dark";
    themeToggle.setAttribute(
      "aria-label",
      theme === "dark" ? "切换浅色主题" : "切换深色主题"
    );
    themeToggle.setAttribute(
      "title",
      theme === "dark" ? "切换浅色主题" : "切换深色主题"
    );
    themeToggle.innerHTML = `<i data-lucide="${theme === "dark" ? "sun" : "moon"}"></i>`;
    renderIcons();
  }

  function closeRail() {
    state.railExpanded = false;
    sideRail.classList.remove("is-expanded");
    railScrim.hidden = true;
    menuToggle.setAttribute("aria-expanded", "false");
    menuToggle.setAttribute("aria-label", "展开导航");
    menuToggle.setAttribute("title", "展开导航");
  }

  function openRail() {
    state.railExpanded = true;
    sideRail.classList.add("is-expanded");
    railScrim.hidden = false;
    menuToggle.setAttribute("aria-expanded", "true");
    menuToggle.setAttribute("aria-label", "收起导航");
    menuToggle.setAttribute("title", "收起导航");
  }

  function closeDetail() {
    state.detailOpen = false;
    taskDetail.classList.remove("is-open");
    detailScrim.hidden = true;
  }

  function openDetail() {
    if (window.innerWidth > 1180) return;
    state.detailOpen = true;
    taskDetail.classList.add("is-open");
    detailScrim.hidden = false;
  }

  function updateRailScope(product) {
    const meta = productMeta[product];
    document.getElementById("rail-title").textContent = meta.title;
    document.getElementById("rail-subtitle").textContent = meta.subtitle;
    const icon = document.querySelector(".rail-product-icon");
    icon.innerHTML = `<i data-lucide="${meta.icon}"></i>`;
    document.querySelector('[data-rail-scope="registration"]').hidden = product !== "registration";
    document.querySelector('[data-rail-scope="reserved"]').hidden = product === "registration";
    sideRail.setAttribute("aria-label", `${meta.title}导航`);
    renderIcons();
  }

  function setProduct(product, updateUrl = true) {
    if (!productMeta[product]) return;
    state.product = product;

    document.querySelectorAll("[data-product]").forEach((button) => {
      const active = button.dataset.product === product;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });

    document.querySelectorAll("[data-product-panel]").forEach((panel) => {
      const active = panel.dataset.productPanel === product;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });

    updateRailScope(product);
    document.title = `${productMeta[product].title} · V5 Suite`;
    closeRail();
    closeDetail();
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (updateUrl) updateHash();
  }

  function setView(view, updateUrl = true) {
    if (!viewTitles[view]) return;
    if (state.product !== "registration") setProduct("registration", false);
    state.view = view;

    document.querySelectorAll("[data-view-panel]").forEach((panel) => {
      const active = panel.dataset.viewPanel === view;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });

    document.querySelectorAll(".rail-item[data-view]").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });

    document.title = `${viewTitles[view]} · V5 Suite`;
    closeRail();
    closeDetail();
    document.getElementById("main-content").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (updateUrl) updateHash();
  }

  function showToast(title, detail = "") {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = `
      <span><i data-lucide="check"></i></span>
      <span><strong>${title}</strong><small>${detail}</small></span>
      <button class="icon-button compact" type="button" aria-label="关闭通知"><i data-lucide="x"></i></button>
    `;
    toast.querySelector("button").addEventListener("click", () => toast.remove());
    toastRegion.appendChild(toast);
    renderIcons();
    window.setTimeout(() => toast.remove(), 4200);
  }

  function renderBatchDetail(batchId) {
    const batch = batches[batchId];
    if (!batch) return;
    state.selectedBatchId = batchId;
    const percent = Math.round((batch.completed / batch.total) * 100);

    document.querySelectorAll("[data-batch-id]").forEach((row) => {
      row.classList.toggle("is-selected", row.dataset.batchId === batchId);
    });

    const status = document.getElementById("detail-status");
    status.className = `status-badge ${batch.status}`;
    status.innerHTML = `<i data-lucide="${batch.statusIcon}"></i>${batch.statusLabel}`;
    document.getElementById("detail-batch").textContent = `#${batchId}`;
    document.getElementById("detail-meta").textContent = `${batch.total} 个任务 · ${batch.started}`;
    document.getElementById("detail-progress-label").textContent = `${batch.completed} / ${batch.total}`;
    document.getElementById("detail-progress-bar").style.width = `${percent}%`;
    document.getElementById("detail-progress-percent").textContent = `${percent}%`;
    document.getElementById("metric-success").textContent = batch.success;
    document.getElementById("metric-failed").textContent = batch.failed;
    document.getElementById("metric-retry").textContent = batch.retry;
    document.getElementById("metric-traffic").textContent = batch.traffic;
    document.getElementById("config-solver").textContent = batch.solver;
    document.getElementById("config-browser").textContent = batch.browser;
    document.getElementById("config-network").textContent = batch.network;
    document.getElementById("config-country").textContent = batch.country;
    document.getElementById("config-email").textContent = batch.email;
    document.getElementById("detail-timeline").innerHTML = batch.timeline
      .map(([time, title, detail, tone]) => `
        <li><i class="${tone}"></i><span><time>${time}</time><strong>${title}</strong><small>${detail}</small></span></li>
      `)
      .join("");
    renderIcons();
  }

  function selectPreset(button) {
    if (!button.dataset.preset) return;
    state.selectedPreset = button.dataset.preset;
    document.querySelectorAll("[data-preset]").forEach((preset) => {
      preset.classList.toggle("is-selected", preset === button);
    });
    showToast("快捷配置已载入", button.querySelector("strong").textContent);
  }

  function selectChoice(group, button) {
    group.querySelectorAll("button[data-value]").forEach((item) => {
      item.classList.toggle("is-selected", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    });

    if (group.dataset.choiceGroup === "solver") {
      state.solver = button.dataset.value;
      document.getElementById("provider-fields").hidden = state.solver === "v11";
      document.getElementById("browser-field").hidden = state.solver === "capmonster";
      document.getElementById("summary-solver").textContent = solverLabels[state.solver];
    }

    if (group.dataset.choiceGroup === "network") {
      state.network = button.dataset.value;
      document.getElementById("proxy-row").hidden = state.network !== "proxy";
      document.getElementById("summary-network").textContent = state.network === "proxy" ? "代理" : "直连";
    }
  }

  function updateRunSummary() {
    const count = document.getElementById("count").value || "1";
    const parallel = document.getElementById("parallel").value || "1";
    const country = document.getElementById("country").value.toUpperCase() || "USA";
    document.getElementById("summary-count").textContent = count;
    document.getElementById("summary-parallel").textContent = parallel;
    document.getElementById("summary-country").textContent = country;
    document.getElementById("dialog-count").textContent = `${count} 个账号`;
    document.getElementById("dialog-solver").textContent = solverLabels[state.solver];
    document.getElementById("dialog-network").textContent = state.network === "proxy" ? "代理" : "直连";
  }

  function resetForm() {
    document.getElementById("run-form").reset();
    document.getElementById("count").value = "10";
    document.getElementById("parallel").value = "10";
    document.getElementById("country").value = "USA";
    const solver = document.querySelector('[data-choice-group="solver"] [data-value="capmonster"]');
    const network = document.querySelector('[data-choice-group="network"] [data-value="proxy"]');
    selectChoice(solver.closest("[data-choice-group]"), solver);
    selectChoice(network.closest("[data-choice-group]"), network);
    updateRunSummary();
    showToast("配置已重置", "已恢复默认任务参数");
  }

  document.querySelectorAll("[data-product]").forEach((button) => {
    button.addEventListener("click", () => setProduct(button.dataset.product));
  });

  document.querySelectorAll(".rail-item[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });

  menuToggle.addEventListener("click", () => {
    if (state.railExpanded) closeRail();
    else openRail();
  });

  document.querySelectorAll('[data-action="close-rail"]').forEach((button) => {
    button.addEventListener("click", closeRail);
  });
  railScrim.addEventListener("click", closeRail);

  themeToggle.addEventListener("click", () => {
    setTheme(state.theme === "dark" ? "light" : "dark");
  });
  if (themeCheckbox) {
    themeCheckbox.addEventListener("change", (event) => {
      setTheme(event.target.checked ? "dark" : "light");
    });
  }

  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => selectPreset(button));
  });

  document.querySelectorAll('[data-action="new-run"]').forEach((button) => {
    button.addEventListener("click", () => setView("new-run"));
  });

  document.querySelectorAll("[data-batch-id]").forEach((row) => {
    row.addEventListener("click", () => {
      renderBatchDetail(row.dataset.batchId);
      openDetail();
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        renderBatchDetail(row.dataset.batchId);
        openDetail();
      }
    });
  });

  document.querySelectorAll('[data-action="open-detail"]').forEach((button) => {
    button.addEventListener("click", openDetail);
  });
  document.querySelectorAll('[data-action="close-detail"]').forEach((button) => {
    button.addEventListener("click", closeDetail);
  });
  detailScrim.addEventListener("click", closeDetail);

  document.querySelectorAll("[data-choice-group]").forEach((group) => {
    group.querySelectorAll("button[data-value]").forEach((button) => {
      button.addEventListener("click", () => selectChoice(group, button));
    });
  });

  document.querySelectorAll("[data-step-target]").forEach((button) => {
    button.setAttribute(
      "aria-label",
      `${button.dataset.direction === "up" ? "增加" : "减少"}${button.dataset.stepTarget === "count" ? "注册数量" : "最大并发"}`
    );
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.stepTarget);
      const delta = button.dataset.direction === "up" ? 1 : -1;
      input.value = String(
        Math.min(Number(input.max), Math.max(Number(input.min), Number(input.value) + delta))
      );
      updateRunSummary();
    });
  });

  ["count", "parallel", "country"].forEach((id) => {
    document.getElementById(id).addEventListener("input", (event) => {
      if (id === "country") {
        event.target.value = event.target.value.toUpperCase().replace(/[^A-Z]/g, "");
      }
      updateRunSummary();
    });
  });

  document.querySelectorAll('[data-action="review-run"]').forEach((button) => {
    button.addEventListener("click", () => {
      updateRunSummary();
      runDialog.showModal();
    });
  });

  document.querySelector('[data-action="confirm-run"]').addEventListener("click", () => {
    window.setTimeout(() => {
      showToast("原型任务已确认", "未执行任何真实注册操作");
      setView("overview");
    }, 80);
  });

  document.querySelector('[data-action="reset-form"]').addEventListener("click", resetForm);

  document.querySelectorAll('[data-action="refresh"]').forEach((button) => {
    button.addEventListener("click", () => {
      showToast("资源状态已刷新", "当前为本地演示数据");
    });
  });

  document.querySelector('[data-action="clear-logs"]').addEventListener("click", () => {
    document.getElementById("log-console").replaceChildren();
    showToast("日志已清空", "当前视图没有日志记录");
  });

  document.querySelector('[data-action="save-settings"]').addEventListener("click", () => {
    showToast("设置已保存", "本地界面偏好已更新");
  });

  document.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const group = chip.parentElement;
      group.querySelectorAll(".filter-chip").forEach((item) => item.classList.remove("is-selected"));
      chip.classList.add("is-selected");
    });
  });

  document.querySelectorAll(".detail-tabs button").forEach((tab) => {
    tab.addEventListener("click", () => {
      tab.parentElement.querySelectorAll("button").forEach((item) => item.classList.remove("is-active"));
      tab.classList.add("is-active");
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeRail();
      closeDetail();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      const search = document.querySelector(".global-search input");
      if (search && getComputedStyle(search.closest(".global-search")).display !== "none") {
        event.preventDefault();
        search.focus();
      }
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 1180) closeDetail();
  });

  const hashParts = location.hash.slice(1).split("/").filter(Boolean);
  if (productMeta[hashParts[0]]) state.product = hashParts[0];
  if (state.product === "registration" && viewTitles[hashParts[1]]) state.view = hashParts[1];

  setTheme(state.theme, false);
  setProduct(state.product, false);
  if (state.product === "registration") setView(state.view, false);
  renderBatchDetail(state.selectedBatchId);
  updateRunSummary();
  renderIcons();
})();
