<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="traffic-lights" aria-hidden="true">
        <span class="red"></span>
        <span class="yellow"></span>
        <span class="green"></span>
      </div>

      <div class="brand-card">
        <div class="brand-mark">MP</div>
        <div>
          <p class="eyebrow">本地控制台</p>
          <h1>公众号抓取</h1>
        </div>
      </div>

      <nav class="nav-list" aria-label="主导航">
        <button
          v-for="item in navItems"
          :key="item.key"
          class="nav-item"
          :class="{ active: activeTab === item.key }"
          type="button"
          @click="activeTab = item.key"
        >
          <span class="nav-icon" v-html="item.icon"></span>
          <span>{{ item.label }}</span>
        </button>
      </nav>
    </aside>

    <main class="main-panel">
      <header class="topbar">
        <div>
          <p class="breadcrumb">WeRSS / 本地网页端 / 第一版</p>
          <h2>{{ currentTitle }}</h2>
        </div>
        <div class="topbar-actions">
          <div class="topbar-status">
            <span>强校验</span>
            <strong :class="authClass">{{ authLabel }}</strong>
          </div>
          <button class="ghost-button" type="button" :disabled="loading.overview" @click="loadOverview">
            {{ loading.overview ? "刷新中..." : "刷新状态" }}
          </button>
          <button class="primary-button" type="button" :disabled="loading.qr" @click="openQrModal">
            {{ loading.qr ? "获取中..." : "扫码登录" }}
          </button>
        </div>
      </header>

      <section v-if="toast" class="toast" role="status" aria-live="polite">
        {{ toast }}
      </section>

      <section v-if="activeTab === 'accounts'" class="workspace-page">
        <article class="workspace-toolbar paper-card">
          <div>
            <p class="eyebrow">公众号工作台</p>
            <h3>先选公众号，再执行抓取</h3>
            <p class="muted-text">
              默认已经帮你选好目标公众号。第一次使用可以直接进入“执行任务”，点击开始执行。
            </p>
          </div>
          <div class="toolbar-actions">
            <span class="count-text">执行清单 {{ selectedRunCount }} / {{ accounts.length || 0 }}</span>
            <button class="ghost-button" type="button" :disabled="loading.accounts" @click="loadAccounts">
              {{ loading.accounts ? "读取中..." : "重新读取公众号" }}
            </button>
            <button class="primary-button" type="button" @click="activeTab = 'tasks'">
              去执行任务
            </button>
          </div>
        </article>

        <article class="paper-card selection-panel">
          <div class="selection-copy">
            <p class="eyebrow">执行清单</p>
            <h3>已选 {{ selectedRunCount }} 个公众号</h3>
            <p class="muted-text">
              勾选的公众号会在“执行任务”里被处理。取消勾选只影响本地执行清单，不会修改 WeRSS 订阅。
            </p>
          </div>
          <div class="selection-actions">
            <button class="ghost-button" type="button" :disabled="accounts.length === 0" @click="setAccountsSelected(accounts, true)">
              全部加入
            </button>
            <button class="ghost-button" type="button" :disabled="selectedRunCount === 0" @click="clearSelectedAccounts">
              清空选择
            </button>
            <select v-model="bulkCategoryName" :disabled="selectedRunCount === 0">
              <option v-for="item in categoryOptions" :key="item.name" :value="item.name">
                {{ item.name }}
              </option>
            </select>
            <button class="ghost-button" type="button" :disabled="selectedRunCount === 0" @click="moveSelectedAccountsToCategory">
              移动到分类
            </button>
          </div>
        </article>

        <article class="paper-card category-manager">
          <div class="category-manager-head">
            <div>
              <p class="eyebrow">分类</p>
              <h3>分类只在这里管理</h3>
            </div>
            <form class="category-form" @submit.prevent="addCategory">
              <input v-model.trim="newCategoryName" type="text" placeholder="新增分类名称" maxlength="24" />
              <button class="ghost-button" type="submit">增加分类</button>
            </form>
          </div>
          <div class="category-chip-list">
            <span v-for="category in categorySections" :key="category.name" class="category-chip">
              {{ category.name }} · {{ category.accounts.length }}
              <button
                v-if="!category.protected && category.accounts.length === 0"
                type="button"
                aria-label="删除分类"
                @click="deleteCategory(category.name)"
              >
                ×
              </button>
            </span>
          </div>
        </article>

        <section class="category-sections" aria-label="公众号分类列表">
          <section v-for="category in categorySections" :key="category.name" class="category-section">
            <div class="category-section-head">
              <h3>{{ category.name }}</h3>
              <span>{{ category.accounts.length }} 个公众号</span>
            </div>
            <div class="mp-card-grid" aria-label="公众号卡片列表">
              <article v-for="account in category.accounts" :key="account.mp_id" class="mp-card" :class="{ selected: account.run_enabled }">
                <label class="mp-select-box">
                  <input
                    v-model="account.run_enabled"
                    type="checkbox"
                    :aria-label="`${account.run_enabled ? '移出' : '加入'}执行清单：${account.mp_name}`"
                    @change="saveAccountFlag(account, 'run_enabled')"
                  />
                  <span class="select-mark"></span>
                </label>

                <div class="mp-card-head">
                  <div class="mp-avatar" aria-hidden="true">
                    <img
                      v-if="account.mp_cover && !account.avatar_failed"
                      :src="account.mp_cover"
                      alt=""
                      loading="lazy"
                      @error="account.avatar_failed = true"
                    />
                    <span v-else>{{ avatarText(account.mp_name) }}</span>
                  </div>
                  <div class="mp-title-block">
                    <h3>{{ account.mp_name }}</h3>
                    <p>{{ account.mp_id }}</p>
                  </div>
                  <span class="mini-pill" :class="{ on: account.server_status === 1 }">
                    {{ account.server_status === 1 ? "WeRSS 启用" : "WeRSS 停用" }}
                  </span>
                </div>

                <div class="mp-card-meta">
                  <span class="category-tag">{{ account.category_name || "其他" }}</span>
                  <span>{{ account.run_enabled ? "已加入执行清单" : "本次不处理" }}</span>
                </div>

                <div class="mp-card-actions">
                  <button
                    class="ghost-button"
                    type="button"
                    :disabled="loading.job || isJobRunning"
                    @click="startJob([account.mp_id], account.mp_id)"
                  >
                    {{ refreshingMpId === account.mp_id ? "执行中..." : "处理该号" }}
                  </button>
                </div>
              </article>
            </div>
          </section>
        </section>

      </section>

      <section v-if="activeTab === 'tasks'" class="task-page">
        <article class="workspace-toolbar paper-card">
          <div>
            <p class="eyebrow">执行任务</p>
            <h3>批量执行区</h3>
            <p class="muted-text">
              这里是命令区。它只负责发起批量流程、展示即将执行的步骤和任务日志；公众号状态在第一个页面管理。
            </p>
          </div>
          <div class="toolbar-actions">
            <span class="count-text">将处理 {{ selectedRunCount }} 个公众号</span>
            <div class="action-with-tip">
              <button
                class="primary-button"
                type="button"
                :disabled="loading.job || isJobRunning || selectedRunCount === 0"
                aria-describedby="workflow-run-tip"
                @click="startJob()"
              >
                {{ loading.job ? "启动中..." : "开始执行" }}
              </button>
              <div id="workflow-run-tip" class="workflow-tooltip" role="tooltip">
                <strong>点击后会按这 6 步执行</strong>
                <ol>
                  <li>读取当前设置和“执行清单”里的公众号。</li>
                  <li>如果选择了刷新，会先做微信登录强校验。</li>
                  <li>未通过强校验时直接停止，不会继续刷新。</li>
                  <li>调用 WeRSS 刷新所选公众号文章。</li>
                  <li>读取最近 N 天文章，跳过已处理和已拒绝内容。</li>
                  <li>按当前 AI 过滤开关决定是否分类，再保存 Markdown。</li>
                </ol>
              </div>
            </div>
          </div>
        </article>

        <article class="paper-card command-panel">
          <div class="section-head">
            <div>
              <p class="eyebrow">命令参数</p>
              <h3>本次执行会做什么</h3>
            </div>
          </div>
          <div class="form-grid compact">
            <label class="check-row">
              <input v-model="jobForm.refresh_before_run" type="checkbox" />
              <span>运行前先刷新 WeRSS 公众号文章</span>
            </label>
            <label class="check-row">
              <input v-model="jobForm.use_ai_filter" type="checkbox" />
              <span>AI 过滤文章</span>
            </label>
            <label>
              抓取最近天数
              <input v-model.number="jobForm.days_to_fetch" type="number" min="1" max="365" />
            </label>
            <label>
              起始页
              <input v-model.number="jobForm.start_page" type="number" min="0" />
            </label>
            <label>
              结束页
              <input v-model.number="jobForm.end_page" type="number" min="0" />
            </label>
          </div>
          <div class="execution-summary">
            <span>1. 强校验登录</span>
            <span>2. 刷新所选公众号</span>
            <span>3. 拉取最近文章</span>
            <span>4. 去重和跳过已拒绝</span>
            <span>5. {{ jobForm.use_ai_filter ? "AI 分类" : "直接保存" }}</span>
            <span>6. 保存 Markdown</span>
          </div>
        </article>

        <article class="paper-card model-panel">
          <div class="section-head">
            <div>
              <p class="eyebrow">模型配置</p>
              <h3>分类模型</h3>
            </div>
            <button
              class="ghost-button"
              type="button"
              :disabled="loading.modelProbe || !modelConfig.classifier.model"
              @click="probeClassifierModel"
            >
              {{ loading.modelProbe ? "测试中..." : "刷新测试" }}
            </button>
          </div>
          <div class="model-config-row">
            <label>
              平台
              <select v-model="modelConfig.classifier.platform" @change="saveClassifierModel">
                <option v-for="platform in modelConfig.platforms" :key="platform.key" :value="platform.key">
                  {{ platform.name }}
                </option>
              </select>
            </label>
            <label>
              模型
              <select v-model="modelConfig.classifier.model" @change="saveClassifierModel">
                <option v-for="model in availableClassifierModels" :key="model" :value="model">
                  {{ model }}
                </option>
              </select>
            </label>
            <div class="model-status" :class="modelProbeStatusClass">
              <span>{{ modelProbeLabel }}</span>
              <small>{{ modelProbeHelp }}</small>
            </div>
          </div>
          <div class="workflow-model-list" aria-label="本次执行模型清单">
            <div v-for="item in modelConfig.workflowModels" :key="item.key" class="workflow-model-item">
              <div>
                <strong>{{ item.name }}</strong>
                <p>{{ item.description }}</p>
              </div>
              <span class="model-pill" :class="{ off: !item.active }">
                {{ formatWorkflowModel(item) }}
              </span>
            </div>
          </div>
        </article>

        <article class="paper-card future-panel">
          <div>
            <p class="eyebrow">预留能力</p>
            <h3>未来可以加到这里</h3>
          </div>
          <div class="future-grid">
            <span>定时任务</span>
            <span>仅分类不下载</span>
            <span>只刷新不转 Markdown</span>
            <span>失败重跑</span>
          </div>
        </article>

        <article class="paper-card job-panel">
          <div class="section-head">
            <div>
              <p class="eyebrow">任务日志</p>
              <h3>{{ activeJob ? `任务 ${activeJob.id}` : "暂无任务" }}</h3>
            </div>
            <button
              v-if="activeJob && ['queued', 'running', 'cancelling'].includes(activeJob.status)"
              class="danger-button"
              type="button"
              @click="cancelJob(activeJob.id)"
            >
              停止任务
            </button>
          </div>
          <div class="progress-track">
            <span :style="{ width: progressPercent + '%' }"></span>
          </div>
          <pre class="log-box">{{ jobLogs }}</pre>
        </article>
      </section>

      <section v-if="activeTab === 'settings'" class="paper-card settings-card">
        <div class="section-head">
          <div>
            <p class="eyebrow">本地配置</p>
            <h3>运行参数</h3>
          </div>
          <button class="primary-button" type="button" :disabled="loading.settings" @click="saveSettings">
            {{ loading.settings ? "保存中..." : "保存设置" }}
          </button>
        </div>
        <div class="form-grid">
          <label>
            WeRSS 地址
            <input v-model="settings.werss_base_url" type="text" />
          </label>
          <label>
            用户名
            <input v-model="settings.username" type="text" autocomplete="username" />
          </label>
          <label>
            密码
            <input v-model="settings.password" type="password" autocomplete="current-password" />
          </label>
          <label>
            强校验关键词
            <input v-model="settings.probe_keyword" type="text" />
          </label>
          <label class="span-2">
            Markdown 输出目录
            <input v-model="settings.output_dir" type="text" />
          </label>
          <label class="span-2">
            拒绝记录 CSV
            <input v-model="settings.rejected_csv_file" type="text" />
          </label>
          <label>
            默认抓取天数
            <input v-model.number="settings.days_to_fetch" type="number" min="1" />
          </label>
          <label>
            刷新等待秒数
            <input v-model.number="settings.refresh_wait_seconds" type="number" min="0" />
          </label>
        </div>
      </section>
    </main>

    <div v-if="qrModal.visible" class="modal-backdrop" role="dialog" aria-modal="true" aria-label="微信扫码登录">
      <div class="qr-modal">
        <button class="modal-close" type="button" aria-label="关闭二维码弹窗" @click="closeQrModal">×</button>
        <p class="eyebrow">微信授权</p>
        <h3>扫码后会再次强校验</h3>
        <div class="qr-frame">
          <div v-if="loading.qr" class="spinner"></div>
          <img v-else-if="qrModal.imageUrl" :src="qrModal.imageUrl" alt="微信授权二维码" />
          <p v-else>二维码未生成</p>
        </div>
        <p class="modal-help">
          这里不会只相信 `/qr/status`，扫码后会用公众号搜索接口再次确认登录是否真实可用。
        </p>
        <button class="ghost-button full" type="button" :disabled="loading.auth" @click="checkAuth">
          {{ loading.auth ? "校验中..." : "我已扫码，立即校验" }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";

type TabKey = "accounts" | "tasks" | "settings";

interface Settings {
  werss_base_url: string;
  username: string;
  password: string;
  probe_keyword: string;
  output_dir: string;
  rejected_csv_file: string;
  days_to_fetch: number;
  refresh_wait_seconds: number;
  start_page: number;
  end_page: number;
  classifier_platform: string;
  classifier_model: string;
}

interface Account {
  mp_id: string;
  mp_name: string;
  mp_cover?: string;
  server_status?: number;
  monitor_enabled: boolean;
  run_enabled: boolean;
  category_name: string;
  avatar_failed?: boolean;
}

interface Category {
  name: string;
  account_count: number;
  created_at?: string;
  protected?: boolean;
}

interface AuthStatus {
  logged_in: boolean;
  can_confirm: boolean;
  display_status: string;
  message: string;
  inconsistent?: boolean;
}

interface Job {
  id: string;
  status: string;
  progress: {
    stage: string;
    current: number;
    total: number;
    message: string;
    mp_name?: string;
  };
  logs: string[];
  result?: unknown;
  error?: string | null;
}

interface AiPlatform {
  key: string;
  name: string;
  models: string[];
}

interface ModelProbeResult {
  status: "idle" | "checking" | "ok" | "bad";
  elapsed_ms?: number;
  response?: string;
  message?: string;
}

interface WorkflowModel {
  key: string;
  name: string;
  active: boolean;
  configurable: boolean;
  platform: string;
  model: string;
  description: string;
  fallbacks?: Array<{ platform: string; model: string }>;
}

const navItems: Array<{ key: TabKey; label: string; icon: string }> = [
  {
    key: "accounts",
    label: "公众号",
    icon: '<svg viewBox="0 0 24 24"><path d="M4 5h16v3H4V5Zm0 5h16v3H4v-3Zm0 5h10v3H4v-3Z"/></svg>'
  },
  {
    key: "tasks",
    label: "执行任务",
    icon: '<svg viewBox="0 0 24 24"><path d="M4 4h10v2H4V4Zm0 5h16v2H4V9Zm0 5h16v2H4v-2Zm0 5h10v2H4v-2Zm13-15 4 3-4 3V4Z"/></svg>'
  },
  {
    key: "settings",
    label: "设置",
    icon: '<svg viewBox="0 0 24 24"><path d="M19.4 13.5c.1-.5.1-1 .1-1.5s0-1-.1-1.5l2-1.5-2-3.4-2.4 1a7 7 0 0 0-2.6-1.5L14 2.5h-4l-.4 2.6A7 7 0 0 0 7 6.6l-2.4-1-2 3.4 2 1.5A8 8 0 0 0 4.5 12c0 .5 0 1 .1 1.5l-2 1.5 2 3.4 2.4-1a7 7 0 0 0 2.6 1.5l.4 2.6h4l.4-2.6a7 7 0 0 0 2.6-1.5l2.4 1 2-3.4-2-1.5ZM12 15.5A3.5 3.5 0 1 1 12 8a3.5 3.5 0 0 1 0 7.5Z"/></svg>'
  }
];

const activeTab = ref<TabKey>("accounts");
const settings = reactive<Settings>({
  werss_base_url: "",
  username: "",
  password: "",
  probe_keyword: "",
  output_dir: "",
  rejected_csv_file: "",
  days_to_fetch: 15,
  refresh_wait_seconds: 10,
  start_page: 0,
  end_page: 20,
  classifier_platform: "",
  classifier_model: ""
});
const jobForm = reactive({
  refresh_before_run: true,
  use_ai_filter: true,
  days_to_fetch: 15,
  start_page: 0,
  end_page: 20
});
const loading = reactive({
  overview: false,
  settings: false,
  accounts: false,
  auth: false,
  qr: false,
  job: false,
  modelProbe: false
});
const overview = ref<any>(null);
const accounts = ref<Account[]>([]);
const categories = ref<Category[]>([]);
const authStatus = ref<AuthStatus | null>(null);
const jobs = ref<Job[]>([]);
const activeJob = ref<Job | null>(null);
const toast = ref("");
const qrModal = reactive({ visible: false, imageUrl: "" });
const refreshingMpId = ref("");
const newCategoryName = ref("");
const bulkCategoryName = ref("其他");
const modelConfig = reactive({
  platforms: [] as AiPlatform[],
  workflowModels: [] as WorkflowModel[],
  classifier: {
    platform: "",
    model: ""
  }
});
const modelProbe = reactive<ModelProbeResult>({ status: "idle" });

let jobTimer = 0;
let qrTimer = 0;

const currentTitle = computed(() => {
  if (activeTab.value === "accounts") return "公众号";
  if (activeTab.value === "tasks") return "执行任务";
  if (activeTab.value === "settings") return "设置";
  return "公众号";
});
const authLabel = computed(() => authStatus.value?.display_status || "未校验");
const authClass = computed(() => {
  if (!authStatus.value?.can_confirm) return "unknown";
  return authStatus.value.logged_in ? "ok" : "bad";
});
const selectedAccounts = computed(() => accounts.value.filter((item) => item.run_enabled));
const selectedRunCount = computed(() => selectedAccounts.value.length);
const isJobRunning = computed(() => {
  return Boolean(activeJob.value && ["queued", "running", "cancelling"].includes(activeJob.value.status));
});
const availableClassifierModels = computed(() => {
  const platform = modelConfig.platforms.find((item) => item.key === modelConfig.classifier.platform);
  return platform?.models || [];
});
const modelProbeLabel = computed(() => {
  if (modelProbe.status === "checking") return "正在测试";
  if (modelProbe.status === "ok") return `可用 · ${modelProbe.elapsed_ms} ms`;
  if (modelProbe.status === "bad") return "无响应";
  return "未测试";
});
const modelProbeHelp = computed(() => {
  if (modelProbe.status === "checking") return "最多等待 60 秒，超过则视为断线。";
  if (modelProbe.status === "ok") return `模型回复：${modelProbe.response || "已收到"}`;
  if (modelProbe.status === "bad") return modelProbe.message || "60 秒内没有拿到有效回复。";
  return "点击刷新测试，会向模型发送：请回复已收到。";
});
const modelProbeStatusClass = computed(() => ({
  checking: modelProbe.status === "checking",
  ok: modelProbe.status === "ok",
  bad: modelProbe.status === "bad"
}));
const categoryOptions = computed(() => {
  const names = new Set(categories.value.map((item) => item.name));
  for (const account of accounts.value) {
    names.add(account.category_name || "其他");
  }
  return Array.from(names)
    .sort((a, b) => a.localeCompare(b, "zh-Hans-CN"))
    .map((name) => ({ name }));
});
const categorySections = computed(() => {
  const categoryMeta = new Map(categories.value.map((item) => [item.name, item]));
  const grouped = new Map<string, Account[]>();

  for (const account of accounts.value) {
    const categoryName = account.category_name || "其他";
    if (!grouped.has(categoryName)) {
      grouped.set(categoryName, []);
    }
    grouped.get(categoryName)?.push(account);
  }

  for (const category of categories.value) {
    if (!grouped.has(category.name)) {
      grouped.set(category.name, []);
    }
  }

  return Array.from(grouped.entries())
    .map(([name, sectionAccounts]) => ({
      name,
      protected: Boolean(categoryMeta.get(name)?.protected),
      accounts: [...sectionAccounts].sort((a, b) => a.mp_name.localeCompare(b.mp_name, "zh-Hans-CN"))
    }))
    .sort((a, b) => {
      if (b.accounts.length !== a.accounts.length) return b.accounts.length - a.accounts.length;
      if (a.name === "其他") return 1;
      if (b.name === "其他") return -1;
      return a.name.localeCompare(b.name, "zh-Hans-CN");
    });
});
const progressPercent = computed(() => {
  const progress = activeJob.value?.progress;
  if (!progress || progress.total <= 0) return activeJob.value ? 12 : 0;
  return Math.min(100, Math.max(8, Math.round((progress.current / progress.total) * 100)));
});
const jobLogs = computed(() => {
  if (!activeJob.value) return "任务尚未启动。";
  return activeJob.value.logs.join("\n");
});

async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // keep raw status text
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

function notify(message: string) {
  toast.value = message;
  window.setTimeout(() => {
    if (toast.value === message) toast.value = "";
  }, 4200);
}

function avatarText(name: string): string {
  const cleanName = String(name || "").trim();
  return cleanName ? cleanName.slice(0, 1) : "?";
}

function formatWorkflowModel(item: WorkflowModel): string {
  if (!item.active) return "本次不调用";
  if (!item.platform) return item.model;
  return `${item.platform} / ${item.model}`;
}

async function loadSettings() {
  const data = await apiFetch<Settings>("/api/settings");
  Object.assign(settings, data);
  jobForm.days_to_fetch = data.days_to_fetch;
  jobForm.start_page = data.start_page;
  jobForm.end_page = data.end_page;
}

async function loadAiModels() {
  const data = await apiFetch<{
    platforms: AiPlatform[];
    workflow_models: WorkflowModel[];
    classifier: { platform: string; model: string };
  }>("/api/ai/models");
  modelConfig.platforms = data.platforms;
  modelConfig.workflowModels = data.workflow_models;
  modelConfig.classifier.platform = data.classifier.platform;
  modelConfig.classifier.model = data.classifier.model;
}

async function saveClassifierModel() {
  const models = availableClassifierModels.value;
  if (models.length > 0 && !models.includes(modelConfig.classifier.model)) {
    modelConfig.classifier.model = models[0];
  }
  try {
    const data = await apiFetch<{ platform: string; model: string }>("/api/ai/models/classifier", {
      method: "PUT",
      body: JSON.stringify(modelConfig.classifier)
    });
    modelConfig.classifier.platform = data.platform;
    modelConfig.classifier.model = data.model;
    await loadAiModels();
    modelProbe.status = "idle";
    modelProbe.message = "";
  } catch (error) {
    notify(`模型配置保存失败：${(error as Error).message}`);
    await loadAiModels();
  }
}

async function probeClassifierModel() {
  loading.modelProbe = true;
  modelProbe.status = "checking";
  modelProbe.elapsed_ms = undefined;
  modelProbe.response = "";
  modelProbe.message = "";
  try {
    await saveClassifierModel();
    const data = await apiFetch<{
      ok: boolean;
      elapsed_ms: number;
      response: string;
    }>("/api/ai/models/classifier/probe", {
      method: "POST",
      body: JSON.stringify(modelConfig.classifier)
    });
    modelProbe.status = data.ok ? "ok" : "bad";
    modelProbe.elapsed_ms = data.elapsed_ms;
    modelProbe.response = data.response;
  } catch (error) {
    modelProbe.status = "bad";
    modelProbe.message = (error as Error).message;
  } finally {
    loading.modelProbe = false;
  }
}

async function saveSettings() {
  loading.settings = true;
  try {
    const data = await apiFetch<Settings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings)
    });
    Object.assign(settings, data);
    notify("设置已保存。");
  } catch (error) {
    notify(`保存失败：${(error as Error).message}`);
  } finally {
    loading.settings = false;
  }
}

async function checkAuth() {
  loading.auth = true;
  try {
    authStatus.value = await apiFetch<AuthStatus>("/api/auth/wechat/check", { method: "POST" });
    if (authStatus.value.logged_in) {
      closeQrModal();
      notify("微信登录已通过强校验。");
    } else {
      notify(authStatus.value.message);
    }
  } catch (error) {
    notify(`状态校验失败：${(error as Error).message}`);
  } finally {
    loading.auth = false;
  }
}

async function openQrModal() {
  loading.qr = true;
  qrModal.visible = true;
  qrModal.imageUrl = "";
  try {
    const data = await apiFetch<{ already_logged_in: boolean; image_url: string | null; auth_status: AuthStatus }>(
      "/api/auth/wechat/qrcode",
      { method: "POST" }
    );
    authStatus.value = data.auth_status;
    if (data.already_logged_in) {
      closeQrModal();
      notify("当前已登录，不需要重新扫码。");
      return;
    }
    qrModal.imageUrl = `${data.image_url}?t=${Date.now()}`;
    window.clearInterval(qrTimer);
    qrTimer = window.setInterval(checkAuth, 3000);
  } catch (error) {
    notify(`二维码获取失败：${(error as Error).message}`);
    qrModal.visible = false;
  } finally {
    loading.qr = false;
  }
}

function closeQrModal() {
  qrModal.visible = false;
  qrModal.imageUrl = "";
  window.clearInterval(qrTimer);
}

async function loadAccounts() {
  loading.accounts = true;
  try {
    const data = await apiFetch<{ total: number; accounts: Account[] }>("/api/accounts");
    accounts.value = data.accounts.map((account) => ({
      ...account,
      category_name: account.category_name || "其他"
    }));
    await loadCategories();
  } catch (error) {
    notify(`公众号读取失败：${(error as Error).message}`);
  } finally {
    loading.accounts = false;
  }
}

async function loadCategories() {
  const data = await apiFetch<{ total: number; categories: Category[] }>("/api/categories");
  categories.value = data.categories;
  const validNames = new Set(categories.value.map((item) => item.name));
  if (!validNames.has(bulkCategoryName.value)) {
    bulkCategoryName.value = categories.value[0]?.name || "其他";
  }
}

async function saveAccountFlag(account: Account, field: "monitor_enabled" | "run_enabled") {
  try {
    await apiFetch(`/api/accounts/${encodeURIComponent(account.mp_id)}/flags`, {
      method: "PATCH",
      body: JSON.stringify({ [field]: account[field] })
    });
  } catch (error) {
    account[field] = !account[field];
    notify(`保存账号开关失败：${(error as Error).message}`);
  }
}

async function setAccountsSelected(targetAccounts: Account[], selected: boolean) {
  const targets = targetAccounts.filter((account) => account.run_enabled !== selected);
  if (targets.length === 0) return;

  const previous = new Map(targets.map((account) => [account.mp_id, account.run_enabled]));
  targets.forEach((account) => {
    account.run_enabled = selected;
  });

  try {
    await Promise.all(
      targets.map((account) =>
        apiFetch(`/api/accounts/${encodeURIComponent(account.mp_id)}/flags`, {
          method: "PATCH",
          body: JSON.stringify({ run_enabled: selected })
        })
      )
    );
    notify(selected ? `已加入 ${targets.length} 个公众号。` : `已移出 ${targets.length} 个公众号。`);
  } catch (error) {
    targets.forEach((account) => {
      account.run_enabled = previous.get(account.mp_id) ?? account.run_enabled;
    });
    notify(`保存执行清单失败：${(error as Error).message}`);
  }
}

async function clearSelectedAccounts() {
  await setAccountsSelected(selectedAccounts.value, false);
}

async function moveSelectedAccountsToCategory() {
  const targets = [...selectedAccounts.value];
  const targetCategory = bulkCategoryName.value;
  if (targets.length === 0) {
    notify("请先勾选要移动的公众号。");
    return;
  }
  if (!targetCategory) {
    notify("请选择目标分类。");
    return;
  }
  if (!window.confirm(`确定把已选 ${targets.length} 个公众号移动到「${targetCategory}」吗？`)) {
    return;
  }

  const previous = new Map(targets.map((account) => [account.mp_id, account.category_name]));
  targets.forEach((account) => {
    account.category_name = targetCategory;
  });

  try {
    await Promise.all(
      targets.map((account) =>
        apiFetch(`/api/accounts/${encodeURIComponent(account.mp_id)}/flags`, {
          method: "PATCH",
          body: JSON.stringify({ category_name: targetCategory })
        })
      )
    );
    await loadCategories();
    notify(`已把 ${targets.length} 个公众号移动到「${targetCategory}」。`);
  } catch (error) {
    targets.forEach((account) => {
      account.category_name = previous.get(account.mp_id) || "其他";
    });
    notify(`移动分类失败：${(error as Error).message}`);
  }
}

async function addCategory() {
  if (!newCategoryName.value) {
    notify("分类名称不能为空。");
    return;
  }
  try {
    await apiFetch("/api/categories", {
      method: "POST",
      body: JSON.stringify({ name: newCategoryName.value })
    });
    newCategoryName.value = "";
    await loadCategories();
  } catch (error) {
    notify(`新增分类失败：${(error as Error).message}`);
  }
}

async function deleteCategory(name: string) {
  if (!window.confirm(`确定删除空分类「${name}」吗？`)) {
    return;
  }
  try {
    await apiFetch(`/api/categories/${encodeURIComponent(name)}`, { method: "DELETE" });
    await loadAccounts();
  } catch (error) {
    notify(`删除分类失败：${(error as Error).message}`);
  }
}

async function loadOverview() {
  loading.overview = true;
  try {
    overview.value = await apiFetch<any>("/api/runtime/overview");
    authStatus.value = overview.value.wechat_status;
    accounts.value = (overview.value.mps || accounts.value).map((account: Account) => ({
      ...account,
      category_name: account.category_name || "其他"
    }));
    await loadCategories();
    notify("状态已刷新。");
  } catch (error) {
    notify(`概览读取失败：${(error as Error).message}`);
  } finally {
    loading.overview = false;
  }
}

async function loadJobs() {
  const data = await apiFetch<{ jobs: Job[] }>("/api/jobs");
  jobs.value = data.jobs;
}

async function startJob(selectedMpIds?: string[], sourceMpId = "") {
  loading.job = true;
  refreshingMpId.value = sourceMpId;
  try {
    const selected = selectedMpIds || accounts.value.filter((item) => item.run_enabled).map((item) => item.mp_id);
    activeJob.value = await apiFetch<Job>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        refresh_before_run: jobForm.refresh_before_run,
        use_ai_filter: jobForm.use_ai_filter,
        days_to_fetch: jobForm.days_to_fetch,
        selected_mp_ids: selected,
        start_page: jobForm.start_page,
        end_page: jobForm.end_page
      })
    });
    notify("任务已启动。");
    pollJob(activeJob.value.id);
  } catch (error) {
    refreshingMpId.value = "";
    notify(`任务启动失败：${(error as Error).message}`);
  } finally {
    loading.job = false;
  }
}

async function pollJob(jobId: string) {
  window.clearInterval(jobTimer);
  const tick = async () => {
    try {
      activeJob.value = await apiFetch<Job>(`/api/jobs/${jobId}`);
      if (!["queued", "running", "cancelling"].includes(activeJob.value.status)) {
        window.clearInterval(jobTimer);
        refreshingMpId.value = "";
        await loadJobs();
      }
    } catch (error) {
      window.clearInterval(jobTimer);
      refreshingMpId.value = "";
      notify(`任务状态读取失败：${(error as Error).message}`);
    }
  };
  await tick();
  if (activeJob.value && ["queued", "running", "cancelling"].includes(activeJob.value.status)) {
    jobTimer = window.setInterval(tick, 1500);
  }
}

async function cancelJob(jobId: string) {
  try {
    activeJob.value = await apiFetch<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  } catch (error) {
    notify(`停止任务失败：${(error as Error).message}`);
  }
}

onMounted(async () => {
  try {
    await loadSettings();
    await Promise.allSettled([loadOverview(), loadJobs(), loadCategories(), loadAiModels()]);
  } catch (error) {
    notify(`初始化失败：${(error as Error).message}`);
  }
});

onUnmounted(() => {
  window.clearInterval(jobTimer);
  window.clearInterval(qrTimer);
});
</script>
