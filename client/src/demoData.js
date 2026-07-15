export const DEMO_ITEM = {
  id: 1001,
  filename: '数据结构课程 · 二叉树遍历.mp4',
  status: 'COMPLETED',
  uploadTime: '2026-07-10T14:30:00',
  transcriptText: '本节课介绍二叉树前序、中序和后序遍历，并对比递归与迭代实现。'
}

export const DEMO_PLAN = {
  understoodGoal: '理解视频核心内容，提炼关键结论，并给出带时间戳的证据和可执行建议',
  intent: 'STRUCTURED_SUMMARY',
  tasks: ['读取视频元数据并确定总结范围', '检索主题、观点和示例相关的时间轴证据', '展开关键证据窗口并生成结构化报告', '校验结论、引用和报告结构']
}

export const DEMO_TRACE = {
  agentMode: 'MODEL_TOOL_CALLING',
  toolCallCount: 5,
  toolCalls: [
    { index: 1, tool: 'get_video_metadata', durationMs: 3, success: true },
    { index: 2, tool: 'search_timeline', durationMs: 18, success: true },
    { index: 3, tool: 'get_evidence_window', durationMs: 2, success: true },
    { index: 4, tool: 'verify_citations', durationMs: 4, success: true },
    { index: 5, tool: 'generate_report', durationMs: 5, success: true }
  ],
  stageDurationMs: {
    VIDEO_CONTEXT: 12840,
    RETRIEVAL: 860,
    PLANNER: 1150,
    EXECUTOR: 3420,
    CRITIC: 1260
  }
}

export const DEMO_EVALUATION = {
  structuredValid: true,
  evidenceSupportRate: 0.92,
  criticPassed: true
}

export const DEMO_RESULT = `## 二叉树遍历课程分析

## 核心结论
- 前序遍历遵循“根节点、左子树、右子树”，适合构建树结构副本。
- 中序遍历二叉搜索树时可得到有序序列。
- 递归实现更直观，迭代实现通过栈显式保存访问状态。

## 视频证据
- [02:05] ASR：老师开始讲解前序遍历的访问顺序。
- [02:08] OCR：课件显示“根节点 → 左子树 → 右子树”。
- [08:42] ASR + OCR：对比递归调用栈与显式栈实现。

## 学习建议
- 用同一棵树手动推演三种遍历顺序。
- 分别实现递归版与迭代版，重点理解栈中保存的状态。`
