<template>
  <div class="app-stage">
    <div class="ambient-noise"></div>
    <div class="ambient-glow"></div>

    <header class="navbar">
      <div class="nav-content">
        <div class="brand">
          <span class="brand-do">SEE</span>
          <span class="brand-video">IT</span>
          <span class="beta-badge">AI</span>
        </div>

        <div class="nav-controls">
          <button v-if="!currentUser" class="auth-btn" @click="openAuthModal">
            <span class="btn-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
            </span>
            登录 / 注册
          </button>

          <div v-else class="user-profile">
            <span class="user-name">{{ currentUser.nickname }}</span>
            <button class="logout-btn" @click="logout" title="退出登录">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
            </button>
          </div>

          <div class="status-pill" :class="{ 'is-active': uploading }">
            <div class="status-dot"></div>
            <span class="status-text">{{ uploading ? '数据传输中...' : '系统就绪' }}</span>
          </div>
        </div>
      </div>
    </header>

    <main class="main-container">
      <section class="hero-section">
        <h1 class="slogan-main">SeeIt AI</h1>
        <p class="slogan-sub">视频内容工作台</p>

        <div class="source-switch" role="tablist" aria-label="视频来源">
          <button
              class="source-switch-btn"
              :class="{ active: sourceMode === 'local' }"
              role="tab"
              :aria-selected="sourceMode === 'local'"
              @click="sourceMode = 'local'"
          >
            本地文件
          </button>
          <button
              class="source-switch-btn"
              :class="{ active: sourceMode === 'bilibili' }"
              role="tab"
              :aria-selected="sourceMode === 'bilibili'"
              @click="sourceMode = 'bilibili'"
          >
            BV 号导入
          </button>
        </div>

        <div class="upload-wrapper">
          <input
              v-if="sourceMode === 'local'"
              type="file"
              id="file-input"
              @change="handleFileChange"
              accept="video/*"
              hidden
          />

          <div
              v-if="sourceMode === 'local'"
              class="upload-magnet"
              :class="{ 'processing': uploading, 'is-dragover': isDragOver }"
              @dragover.prevent="isDragOver = true"
              @dragleave.prevent="isDragOver = false"
              @drop.prevent="handleDrop"
          >
            <div class="split-container" v-if="!uploading">

              <label for="file-input" class="skew-pane pane-local">
                <div class="pane-content unskew">
                  <div class="magnet-icon">
                    <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                  </div>
                  <span class="magnet-title">上传本地视频</span>
                  <span class="magnet-desc">{{ isDragOver ? '松手开始上传' : '点击选择或拖入文件，支持断点续传' }}</span>
                </div>
              </label>
            </div>

            <div class="magnet-content busy" v-else>
              <div class="quantum-loader"></div>
              <span class="busy-text">正在建立通道并解析资源...</span>
            </div>

            <div class="border-glow"></div>
          </div>

          <div v-else class="bilibili-import-panel">
            <div class="bilibili-form-row">
              <div class="bilibili-input-wrap">
                <label for="bvid-input">Bilibili BV 号</label>
                <input
                    id="bvid-input"
                    v-model.trim="bvidInput"
                    maxlength="200"
                    placeholder="BV1xx411c7mD"
                    :disabled="biliPreviewing || biliImporting"
                    @keyup.enter="previewBilibili"
                />
              </div>
              <button
                  class="bilibili-preview-btn"
                  :disabled="biliPreviewing || biliImporting || !bvidInput"
                  @click="previewBilibili"
              >
                {{ biliPreviewing ? '解析中' : '解析视频' }}
              </button>
            </div>

            <div v-if="biliPreview" class="bilibili-preview">
              <img v-if="biliPreview.coverUrl" :src="biliPreview.coverUrl" alt="视频封面" referrerpolicy="no-referrer" />
              <div class="bilibili-preview-copy">
                <span class="source-kicker">{{ biliPreview.bvid }}</span>
                <h2>{{ biliPreview.title }}</h2>
                <div class="preview-meta">
                  <span>{{ biliPreview.uploader || '未知作者' }}</span>
                  <span>{{ formatDuration(biliPreview.durationSeconds) }}</span>
                </div>
              </div>
              <button class="bilibili-import-btn" :disabled="biliImporting" @click="importBilibili">
                {{ biliImporting ? '正在提交' : '导入媒体库' }}
              </button>
            </div>

            <div v-if="biliTask" class="bilibili-task-status" :class="biliTask.state.toLowerCase()">
              <div class="task-status-main">
                <span class="status-dot"></span>
                <div>
                  <strong>{{ biliTask.title || biliTask.bvid }}</strong>
                  <p>{{ biliTask.message }}</p>
                </div>
              </div>
              <span class="task-state-label">{{ importStateLabel(biliTask.state) }}</span>
            </div>

            <p class="source-boundary">仅导入公开且你有权处理的视频，单条最长 10 分钟。</p>
          </div>
        </div>
        <transition name="toast-pop">
          <div v-if="message" class="notification-bar" :class="{ 'error': message.startsWith('❌') || message.startsWith('⚠️') }">
            {{ message }}
          </div>
        </transition>
      </section>

      <section v-if="list.length > 0" class="workspace-section">
        <div class="section-header"><h3>视频任务</h3><div class="count-chip">{{ list.length }}</div></div>
        <div class="card-grid">
          <div v-for="item in list" :key="item.id" class="project-card">

            <button class="delete-btn" @click.stop="deleteItem(item)" title="删除此项">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
            <div class="card-meta">
              <div class="meta-icon" :class="{ 'has-cover': item.coverUrl }">
                <img v-if="item.coverUrl" :src="item.coverUrl" alt="视频封面" referrerpolicy="no-referrer" />
                <svg v-else width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>
              </div>
              <div class="meta-info">
                <div class="filename-mask" :title="item.filename">{{ item.filename }}</div>
                <div class="meta-tags">
                  <span class="time-tag">{{ formatTime(item.uploadTime) }}</span>
                  <span v-if="item.sourceType === 'BILIBILI'" class="source-tag">{{ item.sourceRef }}</span>
                  <span class="status-indicator" :class="item.status.toLowerCase()">
                    {{ item.status === 'COMPLETED' ? '已完成' : item.status === 'FAILED' ? '失败' : '处理中' }}
                  </span>
                </div>
              </div>
            </div>

            <button
                v-if="item.hasAnalysisReport || item.agentMessageCount"
                class="card-agent-history"
                @click="openAgent(item)"
            >
              <span class="history-summary-line">
                <strong>Agent 历史</strong>
                <span>{{ item.agentMessageCount ? `${Math.floor(item.agentMessageCount / 2)} 轮追问` : '已有分析报告' }}</span>
              </span>
              <span class="history-preview">{{ agentHistoryPreview(item) }}</span>
            </button>

            <div class="action-dock">
              <button class="dock-item" @click="downloadAudio(item)">
                <span class="item-icon">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>
                </span>
                <span class="item-label">下载音频</span>
              </button>

              <button
                  class="dock-item"
                  :disabled="item.status !== 'COMPLETED'"
                  @click="transcribe(item.id)"
              >
                <span class="item-icon">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
                </span>
                <span class="item-label">提取文字</span>
              </button>

              <button
                  class="dock-item ai-core"
                  :disabled="item.status !== 'COMPLETED'"
                  @click="openAgent(item)"
              >
                <span class="item-icon">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line></svg>
                </span>
                <div class="label-group">
                  <span class="item-label">Video Agent</span>
                  <span class="item-sub">{{ item.hasAnalysisReport ? '查看历史 / 继续追问' : '开始分析' }}</span>
                </div>
                <div class="shimmer"></div>
              </button>
            </div>
          </div>
        </div>
      </section>

      <div class="sidebar-backdrop" v-if="sidebar.visible" @click="closeSidebar"></div>
      <div class="sidebar-panel" :class="{ 'is-open': sidebar.visible }">
        <div class="sidebar-header">
          <div class="sidebar-title">
            <span class="icon" v-if="sidebar.type === 'ai'">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h2"></path><path d="M20 12h2"></path><path d="M12 2v2"></path><path d="M12 20v2"></path><path d="M20.2 6.47l-1.4 1.4"></path><path d="M15.9 5.35l-1.4-1.4"></path><path d="M9 11a3 3 0 1 0 6 0a3 3 0 0 0-6 0"></path></svg>
            </span>
            <span class="icon" v-else>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
            </span>
            {{ sidebar.title }}
          </div>
          <button class="close-btn" @click="closeSidebar">×</button>
        </div>
        <div ref="sidebarBodyRef" class="sidebar-body">
          <div v-if="sidebar.type === 'ai' && sidebar.mode === 'compose'" class="agent-composer">
            <p class="agent-caption">告诉 Agent 你希望从视频中得到什么产物</p>
            <textarea v-model="sidebar.goal" maxlength="500" placeholder="例如：梳理核心观点，给出带时间戳的证据和可执行建议"></textarea>
            <div class="goal-presets">
              <button v-for="preset in goalPresets" :key="preset" @click="sidebar.goal = preset">{{ preset }}</button>
            </div>
            <button class="agent-run-btn" :disabled="!sidebar.goal.trim()" @click="submitAgent">开始分析</button>
          </div>

          <div v-else-if="sidebar.loading" class="agent-running">
            <div class="loading-state"><div class="quantum-loader small"></div><p>{{ sidebar.statusMessage || 'Agent 正在分析视频证据...' }}</p></div>
            <div v-if="sidebar.progressTotal > 0" class="progress-status" aria-live="polite">
              <div class="progress-label">
                <span>{{ sidebar.stage === 'OCR' ? 'OCR 处理中' : sidebar.stage === 'ASR' ? 'ASR 处理中' : 'Agent 生成中' }}</span>
                <span>{{ sidebar.progressCurrent }}/{{ sidebar.progressTotal }}</span>
              </div>
              <div class="progress-track"><div class="progress-fill" :style="{ width: `${sidebar.progressPercent}%` }"></div></div>
            </div>
            <div v-if="sidebar.plan?.tasks?.length" class="agent-meta-block">
              <span class="meta-label">任务计划</span>
              <ol><li v-for="task in sidebar.plan.tasks" :key="task">{{ task }}</li></ol>
            </div>
            <div v-if="traceStages.length" class="agent-meta-block">
              <span class="meta-label">已完成阶段</span>
              <div class="stage-list"><span v-for="stage in traceStages" :key="stage[0]">{{ stage[0] }} · {{ stage[1] }}ms</span></div>
            </div>
          </div>

          <div v-else>
            <div v-if="sidebar.type === 'ai'">
              <div class="markdown-content report-content" v-html="renderedMarkdown"></div>
              <section v-if="sidebar.followUps?.length" class="conversation-history" aria-label="历史追问">
                <article
                    v-for="(turn, index) in sidebar.followUps"
                    :key="turn.id || `${index}-${turn.question}`"
                    :ref="element => setFollowUpRef(element, index)"
                    class="conversation-turn"
                >
                  <div class="conversation-question">
                    <span>追问 {{ index + 1 }}</span>
                    <small v-if="turn.goal && turn.goal !== sidebar.goal">{{ turn.goal }}</small>
                    <p>{{ turn.question }}</p>
                  </div>
                  <div class="markdown-content conversation-answer" v-html="renderMarkdown(turn.answer)"></div>
                </article>
              </section>
              <div v-if="sidebar.plan?.tasks?.length || traceStages.length || traceToolCalls.length" class="agent-inspector">
                <div v-if="sidebar.plan?.tasks?.length" class="agent-meta-block">
                  <span class="meta-label">Planner 任务</span>
                  <ol><li v-for="task in sidebar.plan.tasks" :key="task">{{ task }}</li></ol>
                </div>
                <div v-if="traceStages.length" class="agent-meta-block">
                  <span class="meta-label">执行轨迹</span>
                  <div class="stage-list"><span v-for="stage in traceStages" :key="stage[0]">{{ stage[0] }} · {{ stage[1] }}ms</span></div>
                </div>
                <div v-if="sidebar.trace?.graph" class="agent-meta-block">
                  <span class="meta-label">状态图 · {{ sidebar.trace.graph.framework }}</span>
                  <div class="stage-list">
                    <span>意图 {{ sidebar.trace.graph.intent }}</span>
                    <span>执行 {{ sidebar.trace.graph.steps }}/{{ sidebar.trace.graph.maxSteps }} 步</span>
                  </div>
                </div>
                <div v-if="traceToolCalls.length" class="agent-meta-block">
                  <span class="meta-label">工具调用 · {{ sidebar.trace.agentMode }} · {{ traceToolCalls.length }} 次</span>
                  <div class="tool-call-list">
                    <div v-for="call in traceToolCalls" :key="call.index" class="tool-call-item">
                      <code>{{ call.tool }}</code>
                      <span :class="call.success ? 'is-success' : 'is-failed'">
                        {{ call.success ? '成功' : '失败' }} · {{ call.durationMs }}ms
                      </span>
                    </div>
                  </div>
                </div>
                <div v-if="sidebar.evaluation && Object.keys(sidebar.evaluation).length" class="quality-row">
                  <span>结构完整 {{ sidebar.evaluation.structuredValid ? '通过' : '待完善' }}</span>
                  <span>证据支持 {{ formatPercent(sidebar.evaluation.evidenceSupportRate) }}</span>
                  <span>Critic {{ sidebar.evaluation.criticPassed ? '通过' : '达到轮次上限' }}</span>
                </div>
                <div v-if="sidebar.memory?.sessionId" class="agent-meta-block">
                  <span class="meta-label">短期记忆 · {{ sidebar.memory.sessionCount || 1 }} 个会话 · {{ sidebar.followUps?.length || 0 }} 轮追问</span>
                  <div class="stage-list"><span>{{ sidebar.memory.goal }}</span></div>
                </div>
              </div>
              <div class="follow-up-box">
                <textarea v-model="sidebar.followUp" maxlength="500" placeholder="基于视频继续追问..."></textarea>
                <button :disabled="sidebar.followUpLoading || !sidebar.followUp.trim()" @click="submitFollowUp">
                  {{ sidebar.followUpLoading ? '分析中' : '追问' }}
                </button>
              </div>
              <div class="feedback-row">
                <span>这个结果有帮助吗？</span>
                <button :class="{ active: sidebar.feedback === 1 }" @click="sendFeedback(1)" title="有帮助">赞</button>
                <button :class="{ active: sidebar.feedback === -1 }" @click="sendFeedback(-1)" title="需改进">踩</button>
              </div>
            </div>
            <div v-else class="text-content"><pre>{{ sidebar.content }}</pre></div>
          </div>
        </div>
      </div>

      <div v-if="showAuthModal" class="auth-backdrop">
        <div class="auth-panel">
          <div class="auth-header">
            <h2 class="auth-title">{{ authMode === 'login' ? '用户登录' : '新用户注册' }}</h2>
            <button class="close-btn" @click="closeAuthModal">×</button>
          </div>
          <div class="auth-body">
            <div class="input-group">
              <label>账号</label>
              <input v-model="authForm.username" type="text" placeholder="输入账号" />
            </div>
            <div class="input-group">
              <label>密码</label>
              <input v-model="authForm.password" type="password" placeholder="输入密码" />
            </div>
            <div class="input-group" v-if="authMode === 'register'">
              <label>昵称</label>
              <input v-model="authForm.nickname" type="text" placeholder="设置一个好听的名字" />
            </div>
            <div class="auth-action">
              <button class="cyber-btn" @click="handleAuth" :disabled="authLoading">
                <span v-if="!authLoading">{{ authMode === 'login' ? '立即登录' : '提交注册' }}</span>
                <span v-else>请求处理中...</span>
              </button>
            </div>
            <div class="auth-toggle">
              <span class="toggle-text">{{ authMode === 'login' ? '没有账号?' : '已有账号?' }}</span>
              <button class="toggle-link" @click="switchAuthMode">{{ authMode === 'login' ? '去注册' : '去登录' }}</button>
            </div>
            <p v-if="authMessage" class="auth-msg" :class="{'error': authError}">{{ authMessage }}</p>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { marked } from 'marked'
import { apiRequest, clearAuthToken, hasAuthToken, setAuthToken } from './api'
import { DEMO_EVALUATION, DEMO_ITEM, DEMO_PLAN, DEMO_RESULT, DEMO_TRACE } from './demoData'

// --- 变量定义 ---
const DEMO_MODE = new URLSearchParams(window.location.search).has('demo')
const DEFAULT_GOAL = '概括视频主要内容和核心观点，并引用有代表性的时间戳证据'
const goalPresets = ['生成学习笔记', '提炼会议结论', '梳理操作步骤']
const file = ref(null)
const sourceMode = ref('local')
const bvidInput = ref('')
const biliPreview = ref(null)
const biliPreviewing = ref(false)
const biliImporting = ref(false)
const biliTask = ref(null)
const biliPollTimer = ref(null)
const message = ref('')
const uploading = ref(false)
const list = ref([])
const isDragOver = ref(false)
const sidebar = ref({
  visible: false,
  type: 'ai',
  mode: 'compose',
  title: '',
  content: '',
  loading: false,
  mediaId: null,
  goal: DEFAULT_GOAL,
  followUp: '',
  followUpLoading: false,
  followUps: [],
  plan: null,
  trace: null,
  evaluation: null,
  memory: null,
  feedback: null,
  taskId: null,
  taskState: null,
  stage: null,
  progressCurrent: 0,
  progressTotal: 0,
  progressPercent: 0,
  statusMessage: '',
  historyLoaded: false
})
const currentUser = ref(null)
const showAuthModal = ref(false)
const authMode = ref('login')
const authLoading = ref(false)
const authMessage = ref('')
const authError = ref(false)
const authForm = ref({ username: '', password: '', nickname: '' })
const pollingTimers = new Map()
const agentStateCache = new Map()
const sidebarBodyRef = ref(null)
const followUpRefs = ref([])
const traceStages = computed(() => Object.entries(sidebar.value.trace?.stageDurationMs || {}))
const traceToolCalls = computed(() => sidebar.value.trace?.toolCalls || [])
const MARKDOWN_TAGS = new Set([
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'BR', 'HR', 'BLOCKQUOTE',
  'UL', 'OL', 'LI', 'STRONG', 'EM', 'DEL', 'CODE', 'PRE', 'A',
  'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD'
])

const stopPolling = (id) => {
  const polling = pollingTimers.get(id)
  if (!polling) return
  clearInterval(polling.timer)
  clearTimeout(polling.timeout)
  pollingTimers.delete(id)
}

const stopAllPolling = () => {
  Array.from(pollingTimers.keys()).forEach(stopPolling)
}

const normalizeModelText = (value) => String(value || '')
  .replace(/\\r\\n/g, '\n')
  .replace(/\\n/g, '\n')
  .replace(/\\r/g, '\n')
  .replace(/\r\n/g, '\n')

const renderMarkdown = (value) => {
  if (!value) return ''
  let cleanText = normalizeModelText(value).replace(/<think>[\s\S]*?<\/think>/gi, '')
  if (cleanText.includes('</think>')) cleanText = cleanText.split('</think>').pop()
  if (!cleanText.trim()) cleanText = normalizeModelText(value)
  const template = document.createElement('template')
  template.innerHTML = marked.parse(cleanText)
  template.content.querySelectorAll('*').forEach(node => {
    if (!MARKDOWN_TAGS.has(node.tagName)) {
      node.replaceWith(document.createTextNode(node.textContent || ''))
      return
    }
    for (const attribute of [...node.attributes]) {
      const isAllowedLinkAttribute = node.tagName === 'A'
        && (attribute.name === 'href' || attribute.name === 'title')
      if (!isAllowedLinkAttribute) {
        node.removeAttribute(attribute.name)
      }
    }
    if (node.tagName === 'A') {
      const href = node.getAttribute('href') || ''
      if (!/^(https?:|mailto:|\/|#)/i.test(href)) node.removeAttribute('href')
      node.setAttribute('rel', 'noopener noreferrer')
      node.setAttribute('target', '_blank')
    }
  })
  return template.innerHTML
}

const renderedMarkdown = computed(() => renderMarkdown(sidebar.value.content))

// --- 核心业务逻辑 ---

const handleFileChange = async (e) => {
  if (!currentUser.value) {
    e.target.value = ''
    showMsg('⚠️ 权限受限：请先登录系统', true)
    openAuthModal()
    return
  }
  const selectedFile = e.target.files[0]
  if (!selectedFile) return
  if (!selectedFile.type.startsWith('video/')) {
    e.target.value = ''
    showMsg('⚠️ 仅支持上传视频文件', true)
    return
  }
  file.value = selectedFile
  await uploadFile()
}

const handleDrop = async (e) => {
  isDragOver.value = false
  if (!currentUser.value) {
    showMsg('⚠️ 权限受限：请先登录系统', true)
    openAuthModal()
    return
  }
  const droppedFiles = e.dataTransfer.files
  if (!droppedFiles || droppedFiles.length === 0) return
  const selectedFile = droppedFiles[0]
  if (!selectedFile.type.startsWith('video/')) {
    showMsg('⚠️ 仅支持上传视频文件', true)
    return
  }
  file.value = selectedFile
  await uploadFile()
}

const CHUNK_SIZE = 5 * 1024 * 1024
const UPLOAD_CONCURRENCY = 3

const uploadFile = async () => {
  if (!file.value) return
  if (DEMO_MODE) {
    showMsg('演示模式：已模拟完成分片上传')
    return
  }
  uploading.value = true
  const selectedFile = file.value
  const totalChunks = Math.ceil(selectedFile.size / CHUNK_SIZE)
  const storageKey = `upload:${selectedFile.name}:${selectedFile.size}:${selectedFile.lastModified}`

  try {
    let uploadId = localStorage.getItem(storageKey)
    let uploadedChunks = new Set()

    if (uploadId) {
      const statusRes = await apiRequest(`/media/upload-status?uploadId=${encodeURIComponent(uploadId)}`)
      if (statusRes.ok) {
        uploadedChunks = new Set(await statusRes.json())
      } else {
        localStorage.removeItem(storageKey)
        uploadId = null
      }
    }

    if (!uploadId) {
      const params = new URLSearchParams({
        filename: selectedFile.name,
        totalChunks: String(totalChunks)
      })
      const initRes = await apiRequest(`/media/init-upload?${params}`, { method: 'POST' })
      if (!initRes.ok) throw new Error(await initRes.text() || '初始化上传失败')
      uploadId = await initRes.json()
      localStorage.setItem(storageKey, uploadId)
    }

    const pendingChunks = Array.from({ length: totalChunks }, (_, index) => index)
      .filter(index => !uploadedChunks.has(index))
    let cursor = 0
    let completedChunks = uploadedChunks.size

    const uploadNext = async () => {
      while (cursor < pendingChunks.length) {
        const index = pendingChunks[cursor++]
        const formData = new FormData()
        formData.append('uploadId', uploadId)
        formData.append('chunkIndex', String(index))
        formData.append('totalChunks', String(totalChunks))
        formData.append('file', selectedFile.slice(index * CHUNK_SIZE, Math.min(selectedFile.size, (index + 1) * CHUNK_SIZE)))

        const chunkRes = await apiRequest('/media/upload-chunk', {
          method: 'POST',
          body: formData
        })
        if (!chunkRes.ok) throw new Error(await chunkRes.text() || `分片 ${index} 上传失败`)
        completedChunks++
        message.value = `正在上传分片 ${completedChunks}/${totalChunks}...`
      }
    }
    await Promise.all(Array.from(
      { length: Math.min(UPLOAD_CONCURRENCY, pendingChunks.length) }, uploadNext
    ))

    message.value = '分片上传完成，正在合并文件...'
    const completeParams = new URLSearchParams({ uploadId })
    const completeRes = await apiRequest(`/media/complete-upload?${completeParams}`, { method: 'POST' })
    if (!completeRes.ok) throw new Error(await completeRes.text() || '文件合并失败')

    localStorage.removeItem(storageKey)
    showMsg('✅ 本地上传完成')
    fetchList()
  } catch (error) {
    console.error(error)
    showMsg('❌ 上传失败: ' + error.message, true)
  } finally {
    uploading.value = false
  }
}

const responseError = async (response, fallback) => {
  const text = await response.text()
  if (!text) return fallback
  try {
    const data = JSON.parse(text)
    return data.detail || data.message || fallback
  } catch (_) {
    return text
  }
}

const formatDuration = (seconds) => {
  const value = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(Math.floor(value % 60)).padStart(2, '0')}`
}

const importStateLabel = (state) => ({
  QUEUED: '排队中',
  PROCESSING: '下载中',
  RETRYING: '重试中',
  COMPLETED: '已完成',
  FAILED: '失败'
}[state] || state)

const stopBiliPolling = () => {
  if (biliPollTimer.value) clearTimeout(biliPollTimer.value)
  biliPollTimer.value = null
}

const pollBilibiliImport = async (taskId, startedAt = Date.now()) => {
  stopBiliPolling()
  try {
    const response = await apiRequest(`/media/bilibili/import-status?taskId=${encodeURIComponent(taskId)}`)
    if (!response.ok) throw new Error(await responseError(response, '导入状态查询失败'))
    const task = await response.json()
    biliTask.value = task
    if (task.state === 'COMPLETED') {
      showMsg('✅ BV 视频已导入媒体库')
      await fetchList()
      return
    }
    if (task.state === 'FAILED') {
      showMsg(`❌ ${task.message}`, true)
      return
    }
    if (Date.now() - startedAt > 15 * 60 * 1000) {
      showMsg('⚠️ 导入仍在后台执行，可稍后重新登录查看', true)
      return
    }
    biliPollTimer.value = setTimeout(() => pollBilibiliImport(taskId, startedAt), 1500)
  } catch (error) {
    console.error(error)
    showMsg(`❌ ${error.message}`, true)
  }
}

const previewBilibili = async () => {
  if (!currentUser.value) {
    showMsg('⚠️ 请先登录后解析 BV 视频', true)
    openAuthModal()
    return
  }
  if (!bvidInput.value) return
  if (DEMO_MODE) {
    biliPreview.value = {
      bvid: 'BV1xx411c7mD',
      title: '数据结构课程 · 二叉树遍历',
      uploader: '公开课演示账号',
      durationSeconds: 542,
      coverUrl: ''
    }
    return
  }
  biliPreviewing.value = true
  biliPreview.value = null
  try {
    const response = await apiRequest('/media/bilibili/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bvid: bvidInput.value })
    })
    if (!response.ok) throw new Error(await responseError(response, 'BV 视频解析失败'))
    biliPreview.value = await response.json()
    bvidInput.value = biliPreview.value.bvid
  } catch (error) {
    showMsg(`❌ ${error.message}`, true)
  } finally {
    biliPreviewing.value = false
  }
}

const importBilibili = async () => {
  if (!biliPreview.value || biliImporting.value) return
  if (DEMO_MODE) {
    biliTask.value = {
      taskId: 'demo-import',
      bvid: biliPreview.value.bvid,
      title: biliPreview.value.title,
      state: 'COMPLETED',
      message: '导入完成'
    }
    showMsg('演示模式：BV 视频已加入媒体库')
    return
  }
  biliImporting.value = true
  try {
    const response = await apiRequest('/media/bilibili/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bvid: biliPreview.value.bvid })
    })
    const data = await response.json()
    if (response.status === 200 && data.mediaId) {
      showMsg('该 BV 视频已在媒体库中')
      await fetchList()
      return
    }
    if (![202, 409].includes(response.status)) {
      throw new Error(data.detail || data.message || '创建导入任务失败')
    }
    biliTask.value = {
      ...data,
      title: biliPreview.value.title,
      state: data.state || 'QUEUED'
    }
    await pollBilibiliImport(data.taskId)
  } catch (error) {
    showMsg(`❌ ${error.message}`, true)
  } finally {
    biliImporting.value = false
  }
}

const restoreBilibiliImport = async () => {
  if (!currentUser.value || DEMO_MODE) return
  try {
    const response = await apiRequest('/media/bilibili/imports')
    if (!response.ok) return
    const tasks = await response.json()
    const active = tasks.find(task => ['QUEUED', 'PROCESSING', 'RETRYING'].includes(task.state))
    if (active) {
      sourceMode.value = 'bilibili'
      biliTask.value = active
      bvidInput.value = active.bvid
      await pollBilibiliImport(active.taskId)
    }
  } catch (error) {
    console.warn('BV 导入任务恢复失败', error)
  }
}

const showMsg = (msg, isError = false) => {
  message.value = msg
  setTimeout(() => { if(message.value === msg) message.value = '' }, 4000)
}

const fetchList = async () => {
  if (DEMO_MODE) return
  try {
    let url = '/media/list'
    if (currentUser.value) {
      const timestamp = new Date().getTime()
      url += `?_t=${timestamp}`

      const res = await apiRequest(url)
      if (!res.ok) throw new Error('加载视频列表失败')
      const data = await res.json()
      list.value = data
    } else {
      list.value = []
    }
  } catch (error) {
    console.error(error)
  }
}

const deleteItem = async (item) => {
  if (DEMO_MODE) {
    list.value = list.value.filter(i => i.id !== item.id)
    showMsg('演示任务已移除')
    return
  }
  if (!confirm(`确认要永久删除 "${item.filename}" 吗？`)) return
  try {
    const res = await apiRequest(`/media/delete?id=${item.id}`, { method: 'DELETE' })
    const text = await res.text()
    if (text === '删除成功') {
      showMsg('文件已销毁')
      list.value = list.value.filter(i => i.id !== item.id)
      agentStateCache.delete(item.id)
    } else {
      showMsg('❌ ' + text, true)
    }
  } catch (e) {
    showMsg('❌ 删除请求失败', true)
  }
}

const formatTime = (timeStr) => {
  if (!timeStr) return '--'
  const date = new Date(timeStr)
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

const downloadAudio = async (item) => {
  if (DEMO_MODE) {
    showMsg(`演示模式：${item.filename} 音频已准备`)
    return
  }
  let fileName = item.filename || 'audio.mp3';
  fileName = fileName.replace(/\.[^/.]+$/, "") + ".mp3";
  try {
    showMsg('正在转码并下载...')
    const res = await apiRequest(`/analysis/download?id=${item.id}`)
    if(!res.ok) throw new Error("Fail")
    const blob = await res.blob()
    const downloadUrl = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = downloadUrl
    link.download = fileName
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(downloadUrl)
    showMsg('✅ 下载完成')
  } catch (e) {
    alert("下载失败")
  }
}

const transcribe = async (id) => {
  const item = list.value.find(i => i.id === id)
  if (DEMO_MODE) {
    openSidebar('text', 'ASR 转写结果')
    sidebar.value.content = item?.transcriptText || DEMO_ITEM.transcriptText
    sidebar.value.loading = false
    return
  }
  if (pollingTimers.get(id)?.type === 'text') {
    openSidebar('text', '全量文字提取')
    sidebar.value.mediaId = id
    sidebar.value.loading = true
    sidebar.value.content = "📝 文字提取正在后台进行中..."
    return
  }
  openSidebar('text', '全量文字提取')
  sidebar.value.mediaId = id
  sidebar.value.loading = true
  sidebar.value.content = "📝 提取任务已提交，正在识别语音流..."
  try {
    const current = await apiRequest(`/analysis/transcription-status?id=${id}`)
    if (!current.ok) throw new Error(await current.text())
    const currentStatus = await current.json()
    if (currentStatus.state === 'COMPLETED') {
      sidebar.value.content = currentStatus.result || ''
      sidebar.value.loading = false
      return
    }
    if (currentStatus.state === 'QUEUED' || currentStatus.state === 'PROCESSING') {
      startPolling(id, 'text')
      return
    }
    const response = await apiRequest(`/analysis/transcribe?id=${id}`, { method: 'POST' })
    if (!response.ok) throw new Error(await response.text())
    startPolling(id, 'text')
  } catch (e) {
    sidebar.value.content = "错误：" + e
    sidebar.value.loading = false
  }
}

const aiAnalyze = async (id, goal) => {
  if (pollingTimers.get(id)?.type === 'ai') {
    sidebar.value.mode = 'result'
    sidebar.value.loading = true
    sidebar.value.statusMessage = '正在恢复分析任务状态...'
    return
  }

  sidebar.value.loading = true
  sidebar.value.mode = 'result'
  sidebar.value.content = ''
  sidebar.value.followUps = []
  sidebar.value.memory = null
  sidebar.value.taskState = 'SUBMITTING'
  sidebar.value.stage = 'ASR'
  sidebar.value.progressCurrent = 0
  sidebar.value.progressTotal = 0
  sidebar.value.progressPercent = 0
  sidebar.value.statusMessage = '正在提交分析任务...'

  try {
    const params = new URLSearchParams({ id: String(id), goal })
    const res = await apiRequest(`/analysis/ai?${params}`, { method: 'POST', timeoutMs: 15000 })
    const text = await res.text()
    let payload = {}
    try { payload = text ? JSON.parse(text) : {} } catch (_) {}
    if (!res.ok && !(res.status === 409 && payload.taskId)) {
      const errorMessage = payload.detail || payload.message || text || '分析任务提交失败'
      showMsg(errorMessage, true)
      sidebar.value.loading = false
      sidebar.value.taskState = 'FAILED'
      sidebar.value.statusMessage = errorMessage
      sidebar.value.content = errorMessage
      return
    }

    if (!payload.taskId) throw new Error('服务端未返回分析任务 ID')
    sidebar.value.taskId = payload.taskId
    sidebar.value.taskState = 'QUEUED'
    sidebar.value.stage = payload.stage || 'QUEUED'
    sidebar.value.progressCurrent = Number(payload.progressCurrent || 0)
    sidebar.value.progressTotal = Number(payload.progressTotal || 0)
    sidebar.value.progressPercent = Number(payload.progressPercent || 0)
    sidebar.value.statusMessage = res.status === 409 ? '正在恢复已存在的分析任务...' : '任务已排队，等待 Worker 接收...'
    startPolling(id, 'ai', goal, payload.taskId)
    refreshAgentMeta(id, goal, false)

  } catch (e) {
    sidebar.value.content = '错误：' + e.message
    sidebar.value.loading = false
    sidebar.value.taskState = 'FAILED'
    sidebar.value.statusMessage = e.message
  }
}

const startPolling = (id, type, goal = '', taskId = null) => {
  stopPolling(id)
  const polling = {
    timer: null,
    timeout: null,
    type,
    taskId,
    inFlight: false,
    metaTick: 0,
    failures: 0,
    startedAt: Date.now()
  }

  const finish = async (result, failed = false) => {
    if (sidebar.value.type === type && sidebar.value.mediaId === id) {
      sidebar.value.content = result
      sidebar.value.loading = false
      sidebar.value.taskState = failed ? 'FAILED' : 'COMPLETED'
      sidebar.value.statusMessage = failed ? result : '分析完成'
      if (type === 'ai' && !failed) await refreshAgentMeta(id, goal, true)
    }
    showMsg(failed ? '任务执行失败，请稍后重试' : '任务完成', failed)
    stopPolling(id)
  }

  const poll = async () => {
    if (polling.inFlight || pollingTimers.get(id) !== polling) return
    polling.inFlight = true
    try {
      if (type === 'ai') {
        const params = new URLSearchParams({ id: String(id), goal })
        const statusPath = polling.taskId
          ? `/analysis/task-status?taskId=${encodeURIComponent(polling.taskId)}`
          : `/analysis/analysis-status?${params}`
        const response = await apiRequest(statusPath, { timeoutMs: 10000 })
        if (!response.ok) throw new Error(await response.text())
        const status = await response.json()
        polling.failures = 0
        sidebar.value.taskState = status.state
        sidebar.value.stage = status.stage || status.state
        sidebar.value.progressCurrent = Number(status.progressCurrent || 0)
        sidebar.value.progressTotal = Number(status.progressTotal || 0)
        sidebar.value.progressPercent = Number(status.progressPercent || 0)
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - polling.startedAt) / 1000))
        const processingMessage = elapsedSeconds < 15
          ? 'Worker 已接收，正在准备视频证据...'
          : `正在执行语音转写与证据分析，已用时 ${elapsedSeconds} 秒。首次处理会比重复分析更久。`
        sidebar.value.statusMessage = status.message || ({
          QUEUED: '任务已排队，等待 Worker 接收...',
          PROCESSING: processingMessage,
          RETRYING: status.message || '本次执行失败，正在等待重试...'
        })[status.state] || status.message || '正在同步任务状态...'
        if (status.state === 'COMPLETED') {
          await fetchList()
          await finish(status.report || status.result || '分析完成')
          return
        }
        if (status.state === 'FAILED') {
          await finish(status.message || '分析失败', true)
          return
        }
        polling.metaTick += 1
        if (sidebar.value.mediaId === id && polling.metaTick % 4 === 0) {
          await refreshAgentMeta(id, goal, false)
        }
        return
      }

      const response = await apiRequest(`/analysis/transcription-status?id=${id}`)
      if (!response.ok) throw new Error(await response.text())
      const status = await response.json()
      if (status.state === 'FAILED') {
        await finish(status.message || '文字提取失败', true)
      } else if (status.state === 'COMPLETED') {
        await finish(status.result || '')
      }
    } catch (error) {
      console.error('任务状态轮询失败', error)
      polling.failures += 1
      if (sidebar.value.mediaId === id) {
        sidebar.value.statusMessage = `状态查询失败，正在重试（${polling.failures}/3）...`
      }
      if (polling.failures >= 3) {
        await finish(`无法获取任务状态：${error.message}`, true)
      }
    } finally {
      polling.inFlight = false
    }
  }

  polling.timer = setInterval(poll, 3000)
  polling.timeout = setTimeout(() => {
    if (pollingTimers.get(id) === polling) {
      stopPolling(id)
      showMsg('任务仍在后台执行，可稍后重新打开查看', true)
      if (sidebar.value.mediaId === id) {
        sidebar.value.loading = false
        sidebar.value.taskState = 'BACKGROUND'
        sidebar.value.statusMessage = '任务仍在后台执行'
        sidebar.value.content = '任务仍在后台执行，可稍后重新打开或刷新页面查看结果。'
      }
    }
  }, 900000)
  pollingTimers.set(id, polling)
  poll()
}

const openSidebar = (type, title) => {
  if (sidebar.value.type === 'ai' && type !== 'ai') cacheCurrentAgentState()
  sidebar.value.visible = true
  sidebar.value.type = type
  sidebar.value.title = title
  sidebar.value.loading = true
  sidebar.value.content = ''
  sidebar.value.taskId = null
  sidebar.value.taskState = null
  sidebar.value.stage = null
  sidebar.value.progressCurrent = 0
  sidebar.value.progressTotal = 0
  sidebar.value.progressPercent = 0
  sidebar.value.statusMessage = ''
}

const cloneAgentState = (state) => JSON.parse(JSON.stringify(state))

const cacheCurrentAgentState = () => {
  if (sidebar.value.type !== 'ai' || !sidebar.value.mediaId) return
  agentStateCache.set(sidebar.value.mediaId, cloneAgentState({
    ...sidebar.value,
    visible: false,
    followUpLoading: false
  }))
}

const closeSidebar = () => {
  cacheCurrentAgentState()
  sidebar.value.visible = false
}

const createAgentState = (item) => ({
    visible: true,
    type: 'ai',
    mode: 'compose',
    title: `Video Agent · ${item.filename}`,
    content: '',
    loading: false,
    mediaId: item.id,
    goal: DEFAULT_GOAL,
    followUp: '',
    followUpLoading: false,
    followUps: [],
    plan: null,
    trace: null,
    evaluation: null,
    memory: null,
    feedback: null,
    taskId: null,
    taskState: null,
    stage: null,
    progressCurrent: 0,
    progressTotal: 0,
    progressPercent: 0,
    statusMessage: '',
    historyLoaded: false
})

const memoryToFollowUps = (memory) => {
  if (!memory) return []
  const sessions = Array.isArray(memory.sessions) && memory.sessions.length
    ? [...memory.sessions].reverse()
    : [memory]
  const turns = []
  sessions.forEach(session => {
    let question = ''
    ;(session.messages || []).forEach((message, index) => {
      if (message.role === 'user') {
        question = normalizeModelText(message.content).trim()
      } else if (message.role === 'assistant') {
        turns.push({
          id: `${session.sessionId || 'memory'}-${index}`,
          goal: session.goal || '',
          question: question || '继续追问',
          answer: normalizeModelText(message.content)
        })
        question = ''
      }
    })
  })
  return turns
}

const restoreAgentState = async (item) => {
  const id = item.id
  sidebar.value.loading = true
  sidebar.value.statusMessage = '正在恢复历史分析与对话...'
  try {
    const [reportResponse, memoryResponse] = await Promise.all([
      apiRequest(`/analysis/report?id=${encodeURIComponent(id)}`),
      apiRequest(`/analysis/agent-memory?id=${encodeURIComponent(id)}`)
    ])
    if (sidebar.value.mediaId !== id) return

    const memory = memoryResponse.ok ? await memoryResponse.json() : null
    if (memory) {
      sidebar.value.memory = memory
      sidebar.value.followUps = memoryToFollowUps(memory)
    }

    if (reportResponse.ok) {
      const report = await reportResponse.json()
      sidebar.value.goal = report.goal || memory?.goal || item.analysisGoal || DEFAULT_GOAL
      sidebar.value.taskId = report.taskId || null
      sidebar.value.taskState = report.state
      sidebar.value.stage = report.stage || report.state
      sidebar.value.progressCurrent = Number(report.progressCurrent || 0)
      sidebar.value.progressTotal = Number(report.progressTotal || 0)
      sidebar.value.progressPercent = Number(report.progressPercent || 0)
      sidebar.value.content = normalizeModelText(report.report || '')
      sidebar.value.evaluation = report.evaluation || null
      sidebar.value.trace = report.trace || null
      sidebar.value.mode = report.report ? 'result' : 'compose'
      sidebar.value.loading = ['QUEUED', 'PROCESSING', 'RETRYING'].includes(report.state)
      sidebar.value.statusMessage = report.message || (report.report ? '历史分析已恢复' : '')
      if (sidebar.value.loading && report.taskId) {
        startPolling(id, 'ai', sidebar.value.goal, report.taskId)
      } else if (report.report) {
        await refreshAgentMeta(id, sidebar.value.goal, true)
      }
    } else {
      sidebar.value.mode = 'compose'
      sidebar.value.loading = false
      sidebar.value.statusMessage = ''
    }
    sidebar.value.historyLoaded = true
    cacheCurrentAgentState()
    sidebar.value.visible = true
  } catch (error) {
    console.warn('Agent 历史恢复失败', error)
    if (sidebar.value.mediaId === id) {
      sidebar.value.mode = 'compose'
      sidebar.value.loading = false
      sidebar.value.statusMessage = ''
      sidebar.value.historyLoaded = true
    }
  }
}

const openAgent = async (item) => {
  if (sidebar.value.type === 'ai' && sidebar.value.mediaId === item.id) {
    sidebar.value.visible = true
    return
  }
  cacheCurrentAgentState()
  const cached = agentStateCache.get(item.id)
  followUpRefs.value = []
  if (cached) {
    sidebar.value = {
      ...cloneAgentState(cached),
      visible: true,
      followUpLoading: false
    }
    return
  }
  sidebar.value = createAgentState(item)
  if (!DEMO_MODE) await restoreAgentState(item)
}

const submitAgent = () => {
  const goal = sidebar.value.goal.trim()
  if (!goal) return
  if (DEMO_MODE) {
    sidebar.value.mode = 'result'
    sidebar.value.loading = true
    sidebar.value.plan = DEMO_PLAN
    sidebar.value.trace = DEMO_TRACE
    setTimeout(showDemoResult, 450)
    return
  }
  aiAnalyze(sidebar.value.mediaId, goal)
}

const showDemoResult = () => {
  sidebar.value.mode = 'result'
  sidebar.value.loading = false
  sidebar.value.content = DEMO_RESULT
  sidebar.value.plan = DEMO_PLAN
  sidebar.value.trace = DEMO_TRACE
  sidebar.value.evaluation = DEMO_EVALUATION
  sidebar.value.historyLoaded = true
}

const refreshAgentMeta = async (id, goal, includeEvaluation) => {
  const params = new URLSearchParams({ id: String(id), goal })
  try {
    const requests = [
      apiRequest(`/analysis/agent-plan?${params}`),
      apiRequest(`/analysis/agent-trace?id=${id}`),
      apiRequest(`/analysis/agent-memory?id=${id}`)
    ]
    if (includeEvaluation) requests.push(apiRequest(`/analysis/agent-evaluation?${params}`))
    const responses = await Promise.all(requests)
    if (sidebar.value.mediaId !== id) return
    const planText = responses[0].ok ? await responses[0].text() : ''
    const traceText = responses[1].ok ? await responses[1].text() : ''
    const memoryText = responses[2].ok ? await responses[2].text() : ''
    if (planText) sidebar.value.plan = JSON.parse(planText)
    if (traceText) sidebar.value.trace = JSON.parse(traceText)
    if (memoryText) sidebar.value.memory = JSON.parse(memoryText)
    if (includeEvaluation && responses[3]?.ok) {
      const evaluationText = await responses[3].text()
      if (evaluationText) sidebar.value.evaluation = JSON.parse(evaluationText)
    }
  } catch (error) {
    console.warn('Agent 元数据暂不可用', error)
  }
}

const submitFollowUp = async () => {
  const question = sidebar.value.followUp.trim()
  if (!question) return
  if (DEMO_MODE) {
    sidebar.value.followUps.push({
      id: `demo-${Date.now()}`,
      goal: sidebar.value.goal,
      question,
      answer: '根据 08:42 的讲解，迭代写法使用显式栈保存待访问节点，时间复杂度仍为 O(n)，额外空间复杂度为 O(h)。'
    })
    sidebar.value.followUp = ''
    await scrollToFollowUp(sidebar.value.followUps.length - 1)
    return
  }
  sidebar.value.followUpLoading = true
  try {
    const params = new URLSearchParams({ id: String(sidebar.value.mediaId), question })
    const res = await apiRequest(`/analysis/follow-up?${params}`, { method: 'POST' })
    const rawAnswer = await res.text()
    if (!res.ok) throw new Error(rawAnswer || '追问失败')
    let answer = rawAnswer
    try {
      const parsed = JSON.parse(rawAnswer)
      if (typeof parsed === 'string') answer = parsed
    } catch (_) {}
    sidebar.value.followUps.push({
      id: `follow-up-${Date.now()}`,
      goal: sidebar.value.goal,
      question,
      answer: normalizeModelText(answer)
    })
    sidebar.value.followUp = ''
    await scrollToFollowUp(sidebar.value.followUps.length - 1)
    await refreshAgentMeta(sidebar.value.mediaId, sidebar.value.goal, true)
    await fetchList()
    cacheCurrentAgentState()
    sidebar.value.visible = true
  } catch (error) {
    showMsg(`❌ ${error.message}`, true)
  } finally {
    sidebar.value.followUpLoading = false
  }
}

const setFollowUpRef = (element, index) => {
  if (element) followUpRefs.value[index] = element
}

const scrollToFollowUp = async (index) => {
  await nextTick()
  const body = sidebarBodyRef.value
  const target = followUpRefs.value[index]
  if (!body || !target) return
  const top = target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - 12
  body.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
}

const agentHistoryPreview = (item) => {
  if (item.agentLastMessage) {
    return normalizeModelText(item.agentLastMessage).replace(/\s+/g, ' ').trim()
  }
  return item.analysisGoal || '已有分析报告，点击查看并继续追问'
}

const sendFeedback = async (rating) => {
  if (DEMO_MODE) {
    sidebar.value.feedback = rating
    showMsg('演示反馈已记录')
    return
  }
  try {
    const res = await apiRequest('/analysis/agent-feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mediaId: sidebar.value.mediaId, goal: sidebar.value.goal, rating })
    })
    if (!res.ok) throw new Error(await res.text())
    sidebar.value.feedback = rating
    showMsg('反馈已记录')
  } catch (error) {
    showMsg(`❌ ${error.message}`, true)
  }
}

const formatPercent = (value) => `${Math.round((Number(value) || 0) * 100)}%`

const openAuthModal = () => {
  showAuthModal.value = true
  authMessage.value = ''
  authForm.value = { username: '', password: '', nickname: '' }
}
const closeAuthModal = () => { showAuthModal.value = false }
const switchAuthMode = () => {
  authMode.value = authMode.value === 'login' ? 'register' : 'login'
  authMessage.value = ''
}
const handleAuth = async () => {
  if (!authForm.value.username || !authForm.value.password) {
    authMessage.value = '请输入完整的账号和密码'
    authError.value = true
    return
  }
  authLoading.value = true
  authMessage.value = ''
  const endpoint = authMode.value === 'login' ? '/user/login' : '/user/register'
  try {
    const res = await apiRequest(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(authForm.value)
    })
    const data = await res.json()
    if (data.code === 200) {
      if (authMode.value === 'login') {
        currentUser.value = data.userInfo
        localStorage.setItem('user', JSON.stringify(data.userInfo))
        setAuthToken(data.token)
        closeAuthModal()
        showMsg(`欢迎回来，${data.userInfo.nickname}`)
        fetchList()
        restoreBilibiliImport()
      } else {
        authMessage.value = '注册成功，请直接登录'
        authError.value = false
        setTimeout(() => switchAuthMode(), 1000)
      }
    } else {
      authMessage.value = data.message || '操作失败'
      authError.value = true
    }
  } catch (e) {
    console.error(e)
    authMessage.value = '网络连接错误'
    authError.value = true
  } finally {
    authLoading.value = false
  }
}
const logout = () => {
  if (hasAuthToken()) {
    apiRequest('/user/logout', { method: 'POST' }).catch(() => {})
  }
  stopAllPolling()
  stopBiliPolling()
  currentUser.value = null
  localStorage.removeItem('user')
  clearAuthToken()
  agentStateCache.clear()
  list.value = []
  biliPreview.value = null
  biliTask.value = null
  showMsg('已退出系统')
}

const handleAuthExpired = () => {
  stopAllPolling()
  stopBiliPolling()
  currentUser.value = null
  list.value = []
  sidebar.value.visible = false
  agentStateCache.clear()
  localStorage.removeItem('user')
  showMsg('登录状态已失效，请重新登录', true)
  openAuthModal()
}

onMounted(() => {
  window.addEventListener('auth-expired', handleAuthExpired)
  if (DEMO_MODE) {
    currentUser.value = { id: 1, nickname: '演示用户' }
    list.value = [DEMO_ITEM]
    openAgent(DEMO_ITEM)
    showDemoResult()
    return
  }
  const savedUser = localStorage.getItem('user')
  if (savedUser && hasAuthToken()) {
    try {
      currentUser.value = JSON.parse(savedUser)
    } catch(e) {}
  }
  fetchList()
  restoreBilibiliImport()
})
onUnmounted(() => {
  window.removeEventListener('auth-expired', handleAuthExpired)
  stopAllPolling()
  stopBiliPolling()
})
</script>

<style>
/* 确保字体引用在最上方 */
:root {
  --bg-deep: #0b0c10;
  --bg-card: #121418;
  --accent-lime: #c5f946;
  --accent-purple: #8a2be2;
  --text-main: #e0e0e0;
  --text-sub: #71757a;
  --text-inverse: #0b0c10;
  --border-tech: #2a2d35;
  --shadow-float: 0 10px 30px -10px rgba(0, 0, 0, 0.7);
  --shadow-glow-lime: 0 0 20px rgba(197, 249, 70, 0.2);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html, body, #app {
  margin: 0 !important; padding: 0 !important; width: 100vw !important;
  max-width: 100vw !important; min-height: 100vh !important;
  overflow-x: hidden; background-color: var(--bg-deep);
}

.app-stage { position: relative; z-index: 1; width: 100%; min-height: 100vh; color: var(--text-main); font-family: 'Space Grotesk', 'Noto Sans SC', monospace; }

.ambient-noise { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)' opacity='0.05'/%3E%3C/svg%3E"); pointer-events: none; z-index: -1; }
.ambient-glow { position: fixed; top: -20%; left: 20%; width: 60vw; height: 60vh; background: radial-gradient(circle, rgba(197, 249, 70, 0.08) 0%, rgba(11, 12, 16, 0) 70%); pointer-events: none; z-index: -2; }

/* 导航 */
.navbar { position: sticky; top: 0; z-index: 100; width: 100%; padding: 1.2rem 0; background: rgba(11, 12, 16, 0.85); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border-tech); }
.nav-content { max-width: 1400px; margin: 0 auto; padding: 0 2rem; display: flex; justify-content: space-between; align-items: center; }
.brand { display: flex; align-items: baseline; gap: 2px; }
.brand-do { font-family: 'Dela Gothic One', sans-serif; font-size: 1.8rem; color: var(--text-main); letter-spacing: -1px; }
.brand-video { font-family: 'Space Grotesk', sans-serif; font-size: 1.8rem; font-weight: 300; }
.beta-badge { font-size: 0.7rem; font-weight: 700; background: var(--accent-lime); color: var(--text-inverse); padding: 2px 6px; border-radius: 2px; margin-left: 8px; transform: translateY(-4px); box-shadow: 0 0 5px var(--accent-lime); }

.nav-controls { display: flex; align-items: center; gap: 15px; }
.auth-btn { background: transparent; border: 1px solid var(--border-tech); color: var(--accent-lime); padding: 6px 16px; border-radius: 4px; font-family: 'Noto Sans SC', sans-serif; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.3s; font-size: 0.85rem; }
.auth-btn:hover { background: rgba(197, 249, 70, 0.1); border-color: var(--accent-lime); box-shadow: 0 0 10px rgba(197, 249, 70, 0.2); }
.user-profile { display: flex; align-items: center; gap: 10px; font-family: monospace; font-size: 0.9rem; color: var(--text-main); }
.user-name { color: var(--accent-lime); }
.logout-btn { background: none; border: none; color: var(--text-sub); cursor: pointer; padding: 4px; display: flex; align-items: center; transition: color 0.3s; }
.logout-btn:hover { color: #ff4757; }

.status-pill { display: flex; align-items: center; gap: 8px; background: var(--bg-card); padding: 6px 12px; border-radius: 4px; border: 1px solid var(--border-tech); font-size: 0.8rem; color: var(--text-sub); }
.status-dot { width: 6px; height: 6px; background: var(--accent-lime); border-radius: 50%; }
.status-pill.is-active .status-dot { animation: pulse-lime 1.5s infinite alternate; }

/* 核心操作区 */
.main-container { max-width: 1200px; margin: 0 auto; padding: 4rem 2rem; }
.hero-section { text-align: center; margin-bottom: 6rem; animation: slideUpFade 0.8s forwards; }
.slogan-main { font-family: 'Syncopate', sans-serif; font-size: clamp(2.5rem, 6vw, 4.5rem); font-weight: 700; margin-bottom: 0.5rem; text-shadow: 0 0 20px rgba(197, 249, 70, 0.2); }
.slogan-sub { font-size: 1.1rem; color: var(--text-sub); letter-spacing: 2px; margin-bottom: 3rem; }

/* === [START] 核心重构：Upload Wrapper (Physical Skew) === */
.upload-wrapper { max-width: 800px; margin: 0 auto; perspective: 1000px; opacity: 0; animation: slideUpFade 0.8s 0.2s forwards; }

.upload-magnet {
  position: relative; height: 300px;
  background: var(--bg-card);
  border-radius: 16px;
  box-shadow: var(--shadow-float);
  border: 2px solid var(--border-tech);
  overflow: hidden; /* 必须隐藏溢出 */
  transition: all 0.3s;
}
.upload-magnet:hover { border-color: var(--accent-lime); box-shadow: var(--shadow-glow-lime); transform: translateY(-5px); }

/* 容器布局 */
.split-container {
  display: flex; height: 100%; width: 100%;
  position: relative; overflow: hidden;
}

/* 左右面板 (物理倾斜) */
.skew-pane {
  flex: 1; height: 100%; position: relative; cursor: pointer;
  background: rgba(11, 12, 16, 0.5); /* 默认深色底 */
  transition: all 0.4s ease;
  display: flex; align-items: center; justify-content: center;
  z-index: 1;
  /* 核心：直接对容器进行 skew，而不是 clip-path */
  transform: skewX(-10deg);
}

/* 增加左右面板的宽度，确保覆盖边缘 */
.pane-local { margin-left: -20px; padding-right: 20px; border-right: 2px solid var(--accent-lime); }
.pane-url { margin-right: -20px; padding-left: 20px; }

/* 鼠标悬停逻辑：只改变背景色，不加外发光，防止穿模 */
.skew-pane:hover {
  background: rgba(197, 249, 70, 0.05); /* 极淡的绿色背景，限制在斜框内 */
  z-index: 10;
}

/* 中间缝隙 */
.split-gap { width: 4px; background: transparent; transform: skewX(-10deg); }

/* 内容回正 */
.pane-content {
  /* 必须反向 skew 回来，否则文字是斜的 */
  transform: skewX(10deg);
  display: flex; flex-direction: column; align-items: center;
  z-index: 2; transition: transform 0.3s;
}
.skew-pane:hover .pane-content { transform: skewX(10deg) scale(1.05); }

/* 互斥变暗 */
.split-container:has(.skew-pane:hover) .skew-pane:not(:hover) { opacity: 0.3; filter: grayscale(1); }

.magnet-icon { color: var(--accent-lime); margin-bottom: 1rem; filter: drop-shadow(0 0 5px var(--accent-lime)); }
.magnet-title { font-size: 1.4rem; font-weight: 700; letter-spacing: 1px; margin-bottom: 5px; font-family: 'Dela Gothic One', sans-serif; }
.magnet-desc { font-size: 0.8rem; color: var(--text-sub); font-family: monospace; }

/* URL 输入框 (需回正) */
.url-input-box {
  display: flex; margin-top: 15px; border-bottom: 2px solid var(--border-tech);
  transition: all 0.3s; position: relative; z-index: 30;
}
.skew-pane:hover .url-input-box { border-color: var(--accent-lime); }
.url-input-box input {
  background: transparent; border: none; outline: none; color: var(--text-main);
  font-family: monospace; padding: 8px 5px; width: 180px; font-size: 0.9rem;
}
.url-go-btn {
  background: transparent; border: none; color: var(--accent-lime); cursor: pointer;
  padding: 0 8px; opacity: 0.7; transition: all 0.3s;
}
.url-go-btn:hover { opacity: 1; transform: translateX(3px); }

/* 处理中状态 */
.magnet-content.busy {
  height: 100%; width: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: var(--bg-card); position: relative; z-index: 50;
}
.busy-text { margin-top: 15px; color: var(--accent-lime); font-family: monospace; animation: pulse-lime 2s infinite; }
/* === [END] 重构结束 === */

.notification-bar { margin-top: 2rem; display: inline-block; background: var(--accent-lime); color: var(--text-inverse); padding: 10px 24px; font-weight: 700; border-radius: 4px; clip-path: polygon(5% 0%, 100% 0%, 95% 100%, 0% 100%); }
.notification-bar.error { background: #ff4757; color: #fff; }

.quantum-loader { width: 50px; height: 50px; border: 4px solid var(--border-tech); border-top-color: var(--accent-lime); border-radius: 50%; animation: spin 0.8s linear infinite; margin-bottom: 1rem; box-shadow: 0 0 10px var(--accent-lime); }
.quantum-loader.small { width: 30px; height: 30px; margin: 0 auto; }

/* 视频任务区 */
.workspace-section { opacity: 0; animation: slideUpFade 0.8s 0.4s forwards; }
.section-header { display: flex; align-items: center; gap: 12px; margin-bottom: 2rem; border-bottom: 2px solid var(--border-tech); padding-bottom: 10px; }
.section-header h3 { font-size: 1.5rem; font-weight: 700; }
.count-chip { background: var(--border-tech); padding: 4px 10px; border-radius: 4px; font-size: 0.75rem; font-family: monospace; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
.project-card { background: var(--bg-card); border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.3); border: 1px solid var(--border-tech); overflow: hidden; transition: transform 0.2s; position: relative; }
.project-card:hover { transform: translateY(-2px); border-color: var(--accent-lime); }
.card-meta { display: flex; gap: 1.5rem; padding: 1.5rem; align-items: center; border-bottom: 1px solid var(--border-tech); background: rgba(18, 21, 18, 0.5); }
.meta-icon { width: 56px; height: 56px; background: rgba(197, 249, 70, 0.05); border: 1px solid var(--accent-lime); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--accent-lime); }
.filename-mask { font-size: 1.1rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
.meta-tags { display: flex; gap: 12px; font-size: 0.85rem; font-family: monospace; margin-top: 5px; }
.time-tag { color: var(--text-sub); }
.status-indicator { font-weight: 600; padding: 2px 8px; border-radius: 4px; }
.status-indicator.completed { color: var(--accent-lime); border: 1px solid var(--accent-lime); background: rgba(197, 249, 70, 0.1); }
.status-indicator.processing { color: var(--accent-purple); border: 1px solid var(--accent-purple); animation: blink 1s infinite; }

.action-dock { display: grid; grid-template-columns: 1fr 1fr 1.5fr; gap: 12px; padding: 12px; background: rgba(5, 8, 5, 0.5); }
.dock-item { position: relative; border: 1px solid var(--border-tech); background: var(--bg-card); border-radius: 8px; padding: 16px; display: flex; align-items: center; justify-content: center; gap: 10px; cursor: pointer; transition: all 0.3s; color: var(--text-sub); font-family: monospace; overflow: hidden; }
.dock-item:hover:not(:disabled) { color: var(--accent-lime); border-color: var(--accent-lime); background: rgba(197, 249, 70, 0.05); }
.dock-item:disabled { opacity: 0.3; cursor: not-allowed; }
.dock-item.ai-core { border-color: var(--accent-purple); color: var(--accent-purple); }
.dock-item.ai-core .label-group { display: flex; flex-direction: column; align-items: flex-start; z-index: 1; }
.dock-item.ai-core .item-sub { font-size: 0.75rem; color: var(--accent-purple); opacity: 0.8; }
.dock-item.ai-core:hover:not(:disabled) { border-color: var(--accent-lime); color: var(--text-inverse); background: var(--accent-lime); }
.dock-item.ai-core:hover:not(:disabled) .item-sub { color: var(--text-inverse); }

/* 分析侧边栏 */
.sidebar-backdrop { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); z-index: 998; }
.sidebar-panel { position: fixed; top: 0; right: -600px; width: 550px; max-width: 90vw; height: 100%; background: var(--bg-card); border-left: 2px solid var(--accent-lime); z-index: 999; transition: right 0.4s cubic-bezier(0.19, 1, 0.22, 1); display: flex; flex-direction: column; box-shadow: -10px 0 40px rgba(0,0,0,0.8); }
.sidebar-panel.is-open { right: 0; }
.sidebar-header { padding: 20px 30px; border-bottom: 1px solid var(--border-tech); display: flex; justify-content: space-between; align-items: center; background: rgba(11, 12, 16, 0.9); }
.sidebar-title { font-size: 1.4rem; font-weight: 700; color: var(--text-main); display: flex; align-items: center; gap: 10px; }
.icon { color: var(--accent-lime); display: flex; align-items: center; }
.close-btn { background: none; border: none; color: var(--text-sub); padding: 5px; cursor: pointer; transition: color 0.3s; }
.close-btn:hover { color: var(--accent-lime); }
.sidebar-body { flex: 1; overflow-y: auto; padding: 30px; }
.loading-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--text-sub); gap: 20px; }
.markdown-content, .text-content { line-height: 1.8; color: var(--text-main); font-size: 0.95rem; }
.text-content pre { white-space: pre-wrap; font-family: monospace; background: #000; padding: 15px; border-radius: 8px; border: 1px solid var(--border-tech); color: #ccc; }
.markdown-content h1, .markdown-content h2, .markdown-content h3 { color: var(--accent-lime); margin-top: 1.5em; margin-bottom: 0.5em; font-family: 'Space Grotesk', sans-serif; }
.markdown-content h1 { border-bottom: 1px solid var(--border-tech); padding-bottom: 10px; }
.markdown-content ul { padding-left: 20px; }
.markdown-content li { margin-bottom: 8px; color: #d4d4d8; }
.markdown-content strong { color: var(--accent-lime); font-weight: 700; }
.markdown-content p { margin-bottom: 1em; }

/* Agent 工作区 */
.agent-composer { display: flex; flex-direction: column; gap: 18px; }
.agent-caption { color: var(--text-sub); line-height: 1.7; }
.agent-composer textarea, .follow-up-box textarea {
  width: 100%; min-height: 130px; resize: vertical; background: #090a0d; color: var(--text-main);
  border: 1px solid var(--border-tech); border-radius: 6px; padding: 14px; line-height: 1.6; outline: none;
}
.agent-composer textarea:focus, .follow-up-box textarea:focus { border-color: var(--accent-lime); }
.goal-presets { display: flex; flex-wrap: wrap; gap: 8px; }
.goal-presets button, .feedback-row button {
  border: 1px solid var(--border-tech); border-radius: 4px; background: transparent; color: var(--text-sub);
  padding: 7px 10px; cursor: pointer;
}
.goal-presets button:hover, .feedback-row button:hover, .feedback-row button.active {
  color: var(--accent-lime); border-color: var(--accent-lime); background: rgba(197, 249, 70, 0.08);
}
.agent-run-btn {
  border: 0; border-radius: 4px; padding: 13px 18px; background: var(--accent-lime); color: var(--text-inverse);
  font-weight: 700; cursor: pointer;
}
.agent-run-btn:disabled, .follow-up-box button:disabled { opacity: 0.4; cursor: not-allowed; }
.agent-running { display: flex; flex-direction: column; gap: 20px; }
.agent-running .loading-state { min-height: 210px; height: auto; }
.agent-progress-message { min-height: 1.5em; }
.progress-status { width: min(100%, 420px); margin: 4px auto 0; }
.progress-label { display: flex; justify-content: space-between; gap: 12px; color: var(--text-sub); font-size: 0.78rem; margin-bottom: 7px; }
.progress-track { height: 5px; overflow: hidden; border-radius: 99px; background: #252a32; }
.progress-fill { height: 100%; min-width: 2px; border-radius: inherit; background: var(--accent-lime); transition: width 0.35s ease; }
.agent-inspector { margin-top: 28px; border-top: 1px solid var(--border-tech); padding-top: 20px; }
.agent-meta-block { margin-bottom: 18px; padding: 14px; background: #0c0e12; border-left: 2px solid var(--accent-lime); }
.meta-label { display: block; color: var(--accent-lime); font-size: 0.78rem; font-weight: 700; margin-bottom: 10px; }
.agent-meta-block ol { padding-left: 20px; color: #c9cbd0; }
.agent-meta-block li { margin: 7px 0; }
.stage-list { display: flex; flex-wrap: wrap; gap: 8px; }
.stage-list span, .quality-row span {
  border: 1px solid var(--border-tech); border-radius: 4px; padding: 6px 8px; color: var(--text-sub); font-size: 0.78rem;
}
.quality-row { display: flex; flex-wrap: wrap; gap: 8px; }
.tool-call-list { display: grid; gap: 7px; margin-top: 10px; }
.tool-call-item {
  min-height: 34px; display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 7px 9px; border: 1px solid #242933; background: #080a0d;
}
.tool-call-item code { color: #d9dee7; font-size: 12px; overflow-wrap: anywhere; }
.tool-call-item span { flex: 0 0 auto; color: #9da6b2; font-size: 11px; }
.tool-call-item span.is-success { color: var(--accent-lime); }
.tool-call-item span.is-failed { color: #ff6b6b; }
.follow-up-box { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 24px; }
.follow-up-box textarea { min-height: 76px; }
.follow-up-box button {
  align-self: stretch; min-width: 76px; border: 1px solid var(--accent-lime); border-radius: 4px;
  background: rgba(197, 249, 70, 0.08); color: var(--accent-lime); cursor: pointer;
}
.feedback-row { display: flex; align-items: center; gap: 8px; margin-top: 18px; color: var(--text-sub); font-size: 0.85rem; }

/* 登录框 */
.auth-backdrop { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); backdrop-filter: blur(5px); z-index: 2000; display: flex; justify-content: center; align-items: center; }
.auth-panel { width: 400px; max-width: 90vw; background: var(--bg-card); border: 1px solid var(--border-tech); border-top: 2px solid var(--accent-lime); box-shadow: 0 20px 50px rgba(0,0,0,0.8); display: flex; flex-direction: column; animation: slideUpFade 0.3s forwards; }
.auth-header { padding: 20px; border-bottom: 1px solid var(--border-tech); display: flex; justify-content: space-between; align-items: center; background: rgba(11,12,16,0.9); }
.auth-title { font-family: 'Noto Sans SC', sans-serif; font-size: 1.2rem; color: var(--text-main); font-weight: 700; letter-spacing: 1px; }
.auth-body { padding: 30px; }
.input-group { margin-bottom: 20px; }
.input-group label { display: block; font-family: 'Noto Sans SC', monospace; color: var(--text-sub); font-size: 0.75rem; margin-bottom: 8px; letter-spacing: 1px; }
.input-group input { width: 100%; background: #000; border: 1px solid var(--border-tech); padding: 12px; color: var(--text-main); font-family: monospace; font-size: 1rem; outline: none; transition: all 0.3s; }
.input-group input:focus { border-color: var(--accent-lime); box-shadow: 0 0 10px rgba(197, 249, 70, 0.2); }
.cyber-btn { width: 100%; background: var(--text-main); color: var(--bg-deep); border: none; padding: 12px; font-weight: 700; font-family: 'Noto Sans SC', sans-serif; cursor: pointer; transition: all 0.3s; clip-path: polygon(5% 0%, 100% 0%, 95% 100%, 0% 100%); margin-bottom: 20px; }
.cyber-btn:hover:not(:disabled) { background: var(--accent-lime); color: var(--text-inverse); box-shadow: 0 0 20px rgba(197, 249, 70, 0.4); }
.cyber-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.auth-toggle { text-align: center; font-size: 0.85rem; font-family: 'Noto Sans SC', sans-serif; color: var(--text-sub); }
.toggle-link { background: none; border: none; color: var(--accent-lime); cursor: pointer; font-weight: 700; margin-left: 5px; text-decoration: underline; }
.toggle-link:hover { color: #fff; }
.auth-msg { margin-top: 15px; text-align: center; font-family: 'Noto Sans SC', monospace; font-size: 0.8rem; color: var(--accent-lime); }
.auth-msg.error { color: #ff4757; }

/* 删除按钮 */
.delete-btn {
  position: absolute; top: 10px; right: 10px; background: transparent; border: none;
  color: #71757a; cursor: pointer; opacity: 0; transition: all 0.3s ease; z-index: 10; padding: 5px;
}
.project-card:hover .delete-btn { opacity: 1; }
.delete-btn:hover { color: #ff4757; transform: scale(1.2) rotate(90deg); }

@media (max-width: 720px) {
  .navbar { padding: 0.8rem 0; }
  .nav-content { padding: 0 1rem; }
  .brand-do, .brand-video { font-size: 1.25rem; }
  .status-pill { display: none; }
  .auth-btn { padding: 6px 10px; }
  .main-container { padding: 2.5rem 1rem; }
  .hero-section { margin-bottom: 3rem; }
  .slogan-main { font-size: 2rem; }
  .slogan-sub { margin-bottom: 2rem; }
  .upload-magnet { height: auto; min-height: 420px; border-radius: 8px; }
  .split-container { flex-direction: column; }
  .skew-pane, .pane-local, .pane-url { min-height: 210px; margin: 0; padding: 0; transform: none; }
  .pane-local { border-right: 0; border-bottom: 1px solid var(--accent-lime); }
  .pane-content, .skew-pane:hover .pane-content { transform: none; }
  .split-gap { display: none; }
  .card-grid { grid-template-columns: 1fr; }
  .action-dock { grid-template-columns: 1fr; }
  .filename-mask { max-width: 55vw; }
  .sidebar-panel { width: 100%; max-width: 100vw; right: -100vw; }
  .sidebar-header { padding: 16px 18px; }
  .sidebar-title { font-size: 1rem; max-width: calc(100vw - 70px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-body { padding: 20px 16px; }
  .follow-up-box { grid-template-columns: 1fr; }
  .follow-up-box button { min-height: 44px; }
}

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes slideUpFade { from { opacity: 0; transform: translateY(40px); } to { opacity: 1; transform: translateY(0); } }
@keyframes pulse-lime { 0% { opacity: 0.5; box-shadow: 0 0 5px var(--accent-lime); } 100% { opacity: 1; box-shadow: 0 0 15px var(--accent-lime); } }
@keyframes blink { 50% { opacity: 0.5; } }
</style>

<style>
:root {
  --bg-deep: #f3f5f7;
  --bg-card: #ffffff;
  --accent-lime: #147d64;
  --accent-purple: #e45f4f;
  --text-main: #172129;
  --text-sub: #68747f;
  --text-inverse: #ffffff;
  --border-tech: #d8dee4;
  --shadow-float: 0 10px 28px rgba(23, 33, 41, 0.08);
  --shadow-glow-lime: 0 0 0 3px rgba(20, 125, 100, 0.12);
}

html, body, #app {
  background: var(--bg-deep) !important;
}

.app-stage {
  color: var(--text-main) !important;
  font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif !important;
}

.app-stage .ambient-noise,
.app-stage .ambient-glow,
.app-stage .border-glow,
.app-stage .shimmer {
  display: none !important;
}

.app-stage .navbar {
  padding: 0 !important;
  background: rgba(255, 255, 255, 0.96) !important;
  border-bottom: 1px solid var(--border-tech) !important;
  backdrop-filter: blur(12px);
}

.app-stage .nav-content {
  min-height: 64px;
  max-width: 1280px !important;
  padding: 0 28px !important;
}

.app-stage .brand {
  align-items: center !important;
  gap: 3px !important;
}

.app-stage .brand-do,
.app-stage .brand-video {
  font-family: 'Inter', sans-serif !important;
  font-size: 1.18rem !important;
  font-weight: 700 !important;
  letter-spacing: 0 !important;
}

.app-stage .brand-do { color: var(--accent-lime) !important; }
.app-stage .brand-video { color: var(--text-main) !important; }

.app-stage .beta-badge {
  margin-left: 7px !important;
  padding: 3px 6px !important;
  border-radius: 4px !important;
  background: #eaf5f1 !important;
  color: var(--accent-lime) !important;
  box-shadow: none !important;
  transform: none !important;
}

.app-stage .auth-btn,
.app-stage .status-pill {
  min-height: 34px;
  border-radius: 6px !important;
  background: #ffffff !important;
  box-shadow: none !important;
}

.app-stage .auth-btn {
  border-color: #bfc8cf !important;
  color: var(--text-main) !important;
}

.app-stage .auth-btn:hover {
  border-color: var(--accent-lime) !important;
  background: #f3faf7 !important;
  box-shadow: none !important;
}

.app-stage .user-name { color: var(--text-main) !important; }
.app-stage .status-dot { background: #2f9e6f !important; }

.app-stage .main-container {
  max-width: 1280px !important;
  padding: 32px 28px 64px !important;
}

.app-stage .hero-section {
  margin-bottom: 40px !important;
  text-align: left !important;
}

.app-stage .slogan-main {
  margin: 0 0 5px !important;
  font-family: 'Inter', sans-serif !important;
  font-size: 1.8rem !important;
  line-height: 1.2;
  font-weight: 700 !important;
  letter-spacing: 0 !important;
  text-shadow: none !important;
}

.app-stage .slogan-sub {
  margin: 0 0 22px !important;
  color: var(--text-sub) !important;
  font-size: 0.92rem !important;
  letter-spacing: 0 !important;
}

.app-stage .upload-wrapper {
  max-width: none !important;
  margin: 0 !important;
  perspective: none !important;
}

.app-stage .upload-magnet {
  height: 220px !important;
  border: 1px solid var(--border-tech) !important;
  border-radius: 8px !important;
  background: #ffffff !important;
  box-shadow: var(--shadow-float) !important;
}

.app-stage .upload-magnet:hover,
.app-stage .upload-magnet.is-dragover {
  border-color: var(--accent-lime) !important;
  box-shadow: var(--shadow-glow-lime) !important;
  transform: none !important;
}

.app-stage .split-container {
  display: grid !important;
  grid-template-columns: 1fr;
}

.app-stage .skew-pane {
  margin: 0 !important;
  padding: 28px !important;
  transform: none !important;
  background: #ffffff !important;
  transition: background 0.18s ease, color 0.18s ease !important;
}

.app-stage .pane-local {
  border-right: 0 !important;
}

.app-stage .skew-pane:hover {
  background: #f4faf7 !important;
}

.app-stage .split-container:has(.skew-pane:hover) .skew-pane:not(:hover) {
  opacity: 1 !important;
  filter: none !important;
}

.app-stage .pane-content,
.app-stage .skew-pane:hover .pane-content {
  transform: none !important;
}

.app-stage .split-gap { display: none !important; }

.app-stage .magnet-icon {
  margin-bottom: 13px !important;
  color: var(--accent-lime) !important;
  filter: none !important;
}

.app-stage .magnet-title {
  margin-bottom: 6px !important;
  font-family: 'Noto Sans SC', sans-serif !important;
  font-size: 1rem !important;
  font-weight: 600 !important;
  letter-spacing: 0 !important;
}

.app-stage .magnet-desc {
  font-family: 'Noto Sans SC', sans-serif !important;
  color: var(--text-sub) !important;
}

.app-stage .url-input-box {
  width: min(320px, 100%);
  margin-top: 15px !important;
  border: 1px solid var(--border-tech) !important;
  border-radius: 6px;
  background: #ffffff;
}

.app-stage .url-input-box input {
  width: 100% !important;
  color: var(--text-main) !important;
  font-family: 'Noto Sans SC', sans-serif !important;
  padding: 9px 10px !important;
}

.app-stage .url-go-btn {
  min-width: 38px;
  color: var(--accent-lime) !important;
  border-left: 1px solid var(--border-tech) !important;
}

.app-stage .notification-bar {
  margin-top: 14px !important;
  padding: 9px 13px !important;
  border: 1px solid #9acabb;
  border-radius: 6px !important;
  clip-path: none !important;
  background: #eaf5f1 !important;
  color: #0c604c !important;
  font-weight: 500 !important;
}

.app-stage .notification-bar.error {
  border-color: #efb4ac !important;
  background: #fff0ee !important;
  color: #a33b2f !important;
}

.app-stage .quantum-loader {
  border-color: #d9e1e5 !important;
  border-top-color: var(--accent-lime) !important;
  box-shadow: none !important;
}

.app-stage .busy-text { color: var(--text-sub) !important; }

.app-stage .section-header {
  margin-bottom: 16px !important;
  padding-bottom: 10px !important;
  border-bottom: 1px solid var(--border-tech) !important;
}

.app-stage .section-header h3 {
  font-size: 1.05rem !important;
}

.app-stage .count-chip {
  min-width: 26px;
  text-align: center;
  background: #e9edf0 !important;
  color: var(--text-sub);
  border-radius: 4px !important;
}

.app-stage .card-grid {
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)) !important;
  gap: 14px !important;
}

.app-stage .project-card {
  border: 1px solid var(--border-tech) !important;
  border-radius: 8px !important;
  background: #ffffff !important;
  box-shadow: 0 3px 12px rgba(23, 33, 41, 0.05) !important;
}

.app-stage .project-card:hover {
  border-color: #aab7bf !important;
  transform: none !important;
}

.app-stage .card-meta {
  padding: 18px !important;
  gap: 14px !important;
  background: #ffffff !important;
}

.app-stage .card-agent-history {
  display: block;
  width: 100%;
  border: 0;
  border-top: 1px solid var(--border-tech);
  padding: 12px 18px;
  background: #fbfcfc;
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
}

.app-stage .card-agent-history:hover {
  background: #f4faf7;
}

.app-stage .history-summary-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--accent-lime);
  font-size: 0.78rem;
}

.app-stage .history-summary-line span {
  color: var(--text-sub);
  white-space: nowrap;
}

.app-stage .history-preview {
  display: block;
  overflow: hidden;
  margin-top: 6px;
  color: var(--text-sub);
  font-size: 0.78rem;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-stage .meta-icon {
  width: 44px !important;
  height: 44px !important;
  border: 0 !important;
  border-radius: 6px !important;
  background: #eaf5f1 !important;
  color: var(--accent-lime) !important;
}

.app-stage .filename-mask {
  max-width: 230px !important;
  font-size: 0.95rem !important;
}

.app-stage .status-indicator.completed {
  border-color: #9acabb !important;
  background: #eaf5f1 !important;
  color: #0c604c !important;
}

.app-stage .status-indicator.processing {
  border-color: #efb4ac !important;
  background: #fff0ee !important;
  color: #a33b2f !important;
}

.app-stage .status-indicator.failed {
  border-color: #d86b5f !important;
  background: #fff0ee !important;
  color: #8b2f27 !important;
}

.app-stage .action-dock {
  grid-template-columns: 1fr 1fr 1.25fr !important;
  gap: 8px !important;
  padding: 10px !important;
  background: #f8f9fa !important;
}

.app-stage .dock-item {
  min-height: 44px;
  padding: 10px !important;
  border-radius: 6px !important;
  background: #ffffff !important;
  color: #4f5c66 !important;
  font-family: 'Noto Sans SC', sans-serif !important;
}

.app-stage .dock-item:hover:not(:disabled) {
  border-color: var(--accent-lime) !important;
  background: #f4faf7 !important;
  color: var(--accent-lime) !important;
}

.app-stage .dock-item.ai-core {
  border-color: var(--accent-lime) !important;
  background: var(--accent-lime) !important;
  color: #ffffff !important;
}

.app-stage .dock-item.ai-core:hover:not(:disabled) {
  border-color: #0f6b56 !important;
  background: #0f6b56 !important;
  color: #ffffff !important;
}

.app-stage .dock-item.ai-core .item-sub {
  display: block;
  margin-top: 3px;
  color: rgba(255, 255, 255, 0.78) !important;
  font-size: 0.7rem;
  line-height: 1.3;
}

.app-stage .sidebar-backdrop,
.app-stage .auth-backdrop {
  background: rgba(23, 33, 41, 0.42) !important;
  backdrop-filter: blur(2px) !important;
}

.app-stage .sidebar-panel {
  width: 600px !important;
  border-left: 1px solid var(--border-tech) !important;
  background: #ffffff !important;
  box-shadow: -12px 0 36px rgba(23, 33, 41, 0.12) !important;
}

.app-stage .sidebar-header,
.app-stage .auth-header {
  background: #ffffff !important;
}

.app-stage .sidebar-title,
.app-stage .auth-title {
  color: var(--text-main) !important;
  font-size: 1.05rem !important;
}

.app-stage .markdown-content,
.app-stage .text-content,
.app-stage .markdown-content li {
  color: #34414a !important;
}

.app-stage .markdown-content h1,
.app-stage .markdown-content h2,
.app-stage .markdown-content h3,
.app-stage .markdown-content strong,
.app-stage .icon,
.app-stage .meta-label {
  color: var(--accent-lime) !important;
}

.app-stage .conversation-history {
  display: grid;
  gap: 14px;
  margin-top: 22px;
}

.app-stage .conversation-turn {
  scroll-margin-top: 12px;
  overflow: hidden;
  border: 1px solid var(--border-tech);
  border-radius: 6px;
  background: #fbfcfc;
}

.app-stage .conversation-question {
  padding: 12px 14px 9px;
  border-bottom: 1px solid var(--border-tech);
  background: #f4f8f6;
}

.app-stage .conversation-question span {
  color: var(--accent-lime);
  font-size: 0.75rem;
  font-weight: 700;
}

.app-stage .conversation-question small {
  display: block;
  overflow: hidden;
  margin-top: 4px;
  color: var(--text-sub);
  font-size: 0.7rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-stage .conversation-question p {
  margin: 7px 0 0;
  color: var(--text-main);
  line-height: 1.6;
}

.app-stage .conversation-answer {
  padding: 13px 14px 3px;
}

.app-stage .conversation-answer p:last-child,
.app-stage .conversation-answer ul:last-child,
.app-stage .conversation-answer ol:last-child {
  margin-bottom: 0;
}

.app-stage .markdown-content pre {
  max-width: 100%;
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.app-stage .agent-composer textarea,
.app-stage .follow-up-box textarea,
.app-stage .input-group input,
.app-stage .text-content pre {
  border: 1px solid var(--border-tech) !important;
  border-radius: 6px !important;
  background: #f8fafb !important;
  color: var(--text-main) !important;
}

.app-stage .agent-meta-block {
  border-left: 3px solid var(--accent-lime) !important;
  background: #f4f8f6 !important;
}

.app-stage .agent-meta-block ol { color: #4f5c66 !important; }

.app-stage .tool-call-item {
  border-color: var(--border-tech) !important;
  background: #ffffff !important;
}

.app-stage .tool-call-item code { color: #34414a !important; }

.app-stage .goal-presets button,
.app-stage .feedback-row button,
.app-stage .stage-list span,
.app-stage .quality-row span {
  background: #ffffff !important;
}

.app-stage .agent-run-btn,
.app-stage .cyber-btn {
  border-radius: 6px !important;
  clip-path: none !important;
  background: var(--accent-lime) !important;
  color: #ffffff !important;
  box-shadow: none !important;
}

.app-stage .follow-up-box button {
  border-color: var(--accent-lime) !important;
  border-radius: 6px !important;
  background: #eaf5f1 !important;
  color: var(--accent-lime) !important;
}

.app-stage .auth-panel {
  border: 1px solid var(--border-tech) !important;
  border-radius: 8px;
  box-shadow: 0 20px 48px rgba(23, 33, 41, 0.18) !important;
  overflow: hidden;
}

.app-stage .source-switch {
  width: min(360px, 100%);
  height: 42px;
  margin: 0 auto 16px;
  padding: 3px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  border: 1px solid var(--border-tech);
  border-radius: 6px;
  background: #e9edf0;
}

.app-stage .source-switch-btn {
  min-width: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: #68747f;
  font-size: 0.86rem;
  font-weight: 600;
  cursor: pointer;
}

.app-stage .source-switch-btn.active {
  background: #ffffff;
  color: #172129;
  box-shadow: 0 1px 4px rgba(23, 33, 41, 0.12);
}

.app-stage .bilibili-import-panel {
  min-height: 360px;
  padding: 34px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 22px;
  border: 1px solid var(--border-tech);
  border-radius: 8px;
  background: #ffffff;
  box-shadow: 0 8px 26px rgba(23, 33, 41, 0.06);
  text-align: left;
}

.app-stage .bilibili-form-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 12px;
}

.app-stage .bilibili-input-wrap label {
  display: block;
  margin-bottom: 8px;
  color: #34414a;
  font-size: 0.82rem;
  font-weight: 600;
}

.app-stage .bilibili-input-wrap input {
  box-sizing: border-box;
  width: 100%;
  height: 48px;
  padding: 0 14px;
  border: 1px solid #cbd4da;
  border-radius: 6px;
  background: #f8fafb;
  color: #172129;
  font-family: 'Inter', sans-serif;
}

.app-stage .bilibili-input-wrap input:focus {
  border-color: var(--accent-lime);
  outline: 0;
  box-shadow: 0 0 0 3px rgba(20, 125, 100, 0.12);
}

.app-stage .bilibili-preview-btn,
.app-stage .bilibili-import-btn {
  height: 48px;
  padding: 0 22px;
  border: 0;
  border-radius: 6px;
  background: #172129;
  color: #ffffff;
  font-weight: 600;
  cursor: pointer;
}

.app-stage .bilibili-import-btn {
  background: #e45f4f;
  white-space: nowrap;
}

.app-stage .bilibili-preview-btn:disabled,
.app-stage .bilibili-import-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.app-stage .bilibili-preview {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
  padding-top: 20px;
  border-top: 1px solid var(--border-tech);
}

.app-stage .bilibili-preview img {
  width: 180px;
  aspect-ratio: 16 / 9;
  object-fit: cover;
  border-radius: 6px;
  background: #edf1f3;
}

.app-stage .bilibili-preview-copy { min-width: 0; }

.app-stage .source-kicker {
  display: block;
  margin-bottom: 5px;
  color: #d14f43;
  font-size: 0.76rem;
  font-weight: 700;
}

.app-stage .bilibili-preview-copy h2 {
  margin: 0 0 10px;
  overflow: hidden;
  color: #172129;
  font-size: 1.05rem;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-stage .preview-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  color: #68747f;
  font-size: 0.8rem;
}

.app-stage .bilibili-task-status {
  min-height: 66px;
  padding: 14px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border: 1px solid #cfe0da;
  border-radius: 6px;
  background: #f4faf7;
}

.app-stage .task-status-main {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.app-stage .task-status-main .status-dot {
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent-lime);
}

.app-stage .task-status-main strong {
  display: block;
  overflow: hidden;
  color: #172129;
  font-size: 0.9rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-stage .task-status-main p {
  margin: 4px 0 0;
  color: #68747f;
  font-size: 0.78rem;
}

.app-stage .task-state-label,
.app-stage .source-tag {
  padding: 3px 7px;
  border-radius: 4px;
  background: #eaf5f1;
  color: #0c604c;
  font-size: 0.7rem;
  font-weight: 700;
  white-space: nowrap;
}

.app-stage .bilibili-task-status.failed {
  border-color: #efb4ac;
  background: #fff4f2;
}

.app-stage .bilibili-task-status.failed .status-dot { background: #d14f43; }
.app-stage .bilibili-task-status.failed .task-state-label { background: #ffe4df; color: #8b2f27; }

.app-stage .source-boundary {
  margin: 0;
  color: #7b8790;
  font-size: 0.75rem;
  text-align: center;
}

.app-stage .meta-icon.has-cover {
  overflow: hidden;
  background: #edf1f3 !important;
}

.app-stage .meta-icon.has-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

@media (max-width: 760px) {
  .app-stage .nav-content { padding: 0 16px !important; }
  .app-stage .main-container { padding: 24px 16px 48px !important; }
  .app-stage .split-container { grid-template-columns: 1fr !important; }
  .app-stage .upload-magnet { height: auto !important; min-height: 360px !important; }
  .app-stage .pane-local { border-right: 0 !important; border-bottom: 1px solid var(--border-tech) !important; }
  .app-stage .skew-pane { min-height: 180px !important; }
  .app-stage .card-grid { grid-template-columns: 1fr !important; }
  .app-stage .action-dock { grid-template-columns: 1fr !important; }
  .app-stage .sidebar-panel { width: 100% !important; }
  .app-stage .bilibili-import-panel { min-height: 390px; padding: 22px 18px; }
  .app-stage .bilibili-form-row { grid-template-columns: 1fr; }
  .app-stage .bilibili-preview-btn { width: 100%; }
  .app-stage .bilibili-preview { grid-template-columns: 1fr; }
  .app-stage .bilibili-preview img { width: 100%; }
  .app-stage .bilibili-preview-copy h2 { white-space: normal; }
  .app-stage .bilibili-import-btn { width: 100%; }
  .app-stage .bilibili-task-status { align-items: flex-start; flex-direction: column; }
}
</style>
