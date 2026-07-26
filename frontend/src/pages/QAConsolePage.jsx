import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Tag,
  Timeline,
  Tooltip,
  TreeSelect,
  Typography,
  Divider,
  Alert,
  message,
} from "antd";
import { BulbOutlined, DeleteOutlined, RobotOutlined, SaveOutlined, SendOutlined, QuestionCircleOutlined, StopOutlined, ToolOutlined, EyeOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import AppLayout from "../components/Layout/AppLayout";
import apiClient from "../services/api";
import useAuthStore from "../stores/authStore";
import PdfPreviewModal from "../components/Documents/PdfPreviewModal";

const { Text } = Typography;

// 保險層：舊資料的向量塊可能被 VL 模型包在 ```markdown 圍籬裡（後端 ai.py 已在
// 新資料入庫前剝除，但既有資料若不重跑 OCR 仍帶著圍籬）。若不剝掉，ReactMarkdown
// 會依規範把整段當程式碼區塊原樣顯示，表格與標題都不會渲染。
// 只處理「包裝用」圍籬；文件內真正的程式碼區塊（```python 等）保留。
const stripFenceWrapper = (text) => {
  if (!text || !text.includes("```")) return text;
  const src = text.trim();
  const whole = src.match(/^```[ \t]*(?:markdown|md)?[ \t]*\n([\s\S]*?)\n?```$/i);
  if (whole && !whole[1].includes("```")) return whole[1].trim();

  const isWrapper = (line) => /^```[ \t]*(?:markdown|md)[ \t]*$/i.test(line.trim());
  if (!src.split("\n").some(isWrapper)) return src;

  const kept = [];
  let inWrapper = false;
  for (const line of src.split("\n")) {
    const bare = line.trim();
    if (isWrapper(bare)) { inWrapper = true; continue; }
    if (inWrapper && bare === "```") { inWrapper = false; continue; }
    kept.push(line);
  }
  return kept.join("\n").trim();
};

const QAConsolePage = () => {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [conversationHistory, setConversationHistory] = useState([]);
  const [streamingMsg, setStreamingMsg] = useState(null);
  // streamingMsg: { question, thinking, answer, isStreaming, thinkingDone, sources, is_followup, optimized_query }
  const [classifications, setClassifications] = useState([]);
  const [projectOptions, setProjectOptions] = useState([]);
  const [documents, setDocuments] = useState([]);
  const [folders, setFolders] = useState([]);
  const [pdfPreview, setPdfPreview] = useState({ open: false, documentId: null, title: "", page: 1 });
  const [followupQuestion, setFollowupQuestion] = useState("");
  const [expandedSnippets, setExpandedSnippets] = useState({});
  // 三模式:'rag'(純內容檢索) | 'hybrid'(自動路由,預設) | 'agent'(關係/多步)
  const [qaMode, setQaMode] = useState(() => {
    try {
      const v = window.localStorage.getItem("qa_mode");
      if (v === "rag" || v === "hybrid" || v === "agent") return v;
      // 舊設定相容:之前的 Agent 開關
      return window.localStorage.getItem("qa_agent_mode") === "1" ? "agent" : "hybrid";
    } catch { return "hybrid"; }
  });
  const [saveNoteModal, setSaveNoteModal] = useState({ visible: false, msg: null });
  const [saveNoteDocId, setSaveNoteDocId] = useState(null);
  const [saveNoteLoading, setSaveNoteLoading] = useState(false);
  const conversationEndRef = useRef(null);
  const abortRef = useRef(null);
  // Audit H14：鏡射最新的 streamingMsg，讓 onDone 從 ref 讀取最終值，
  // 不必把 setConversationHistory 塞進 setStreamingMsg 的 updater 內
  // （那是 React 明文禁止的副作用，StrictMode 下會 double-invoke → 訊息存兩筆、後端也被 PUT 兩次）。
  const streamingMsgRef = useRef(null);

  // streamingMsgRef 是「同步的真實來源」：每次更新都先「同步」寫入 ref，再 setStreamingMsg 觸發渲染。
  // 修正 sources 遺失：sources 事件與 done 事件常在同一個 reader tick 連續送達；
  // 若靠 setState 的 updater 更新 ref，updater 是在 render 階段（非同步）才執行，
  // onDone 這時讀 ref 仍是舊值 → 參考來源卡片消失、無法點預覽。
  // 改為在 setStreaming 呼叫當下同步更新 ref（updater 也讀 ref，確保鏈式累積正確）。
  const setStreaming = (updater) => {
    const next = typeof updater === "function" ? updater(streamingMsgRef.current) : updater;
    streamingMsgRef.current = next;
    setStreamingMsg(next);
  };

  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversationHistory, streamingMsg?.answer]);

  // Audit H12：元件卸載（切換路由）時中止進行中的串流，
  // 避免 fetch reader 繼續對已卸載元件 setState（記憶體洩漏）與後端 LLM 空跑。
  useEffect(() => {
    return () => {
      try { abortRef.current?.abort(); } catch { /* ignore */ }
      abortRef.current = null;
    };
  }, []);

  // (ref 由 setStreaming 同步維護，不需再用 effect 從 state 回寫)

  const stopInFlight = () => {
    try { abortRef.current?.abort(); } catch { /* ignore */ }
    abortRef.current = null;
    setLoading(false);
    setStreaming(null);
    message.info("已停止查詢");
  };

  const loadInitialData = async () => {
    const [classificationRes, metadataRes, documentsRes, foldersRes] = await Promise.allSettled([
      apiClient.get("documents/classifications"),
      apiClient.get("metadata-fields"),
      apiClient.get("documents/", { params: { page: 1, page_size: 200 } }),
      apiClient.get("folders"),
    ]);

    if (classificationRes.status === "fulfilled") setClassifications(classificationRes.value.data ?? []);
    if (metadataRes.status === "fulfilled") {
      const fields = metadataRes.value.data ?? [];
      const projectField = fields.find((f) => f.name === "project_id");
      setProjectOptions(projectField?.options ?? []);
    }
    if (documentsRes.status === "fulfilled") setDocuments(documentsRes.value.data?.items ?? []);
    if (foldersRes.status === "fulfilled") setFolders(foldersRes.value.data ?? []);
  };

  useEffect(() => {
    loadInitialData();
    apiClient.get("rag/conversation")
      .then((res) => setConversationHistory(res.data?.messages ?? []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (conversationHistory.length > 0) {
      apiClient.put("rag/conversation", { messages: conversationHistory }).catch(() => {});
    }
  }, [conversationHistory]);

  // Helper: get auth token
  const getToken = () => {
    let token = useAuthStore.getState().token;
    if (!token) {
      try {
        const persisted = JSON.parse(window.localStorage.getItem("auth-storage") || "{}");
        token = persisted?.state?.token;
      } catch { /* ignore */ }
    }
    return token;
  };

  // Streaming RAG via SSE
  const postRagStream = async (payload, { onThinking, onContent, onSources, onDone, onError }) => {
    const token = getToken();
    const controller = new AbortController();
    abortRef.current = controller;

    const resp = await fetch("/api/v1/rag/query/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { const err = await resp.json(); detail = err.detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false; // 是否已收到 done/error 事件

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop(); // keep incomplete last line

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const jsonStr = line.slice(6).trim();
        if (!jsonStr) continue;
        try {
          const event = JSON.parse(jsonStr);
          if (event.type === "thinking") onThinking?.(event.text || "");
          else if (event.type === "content") onContent?.(event.text || "");
          else if (event.type === "sources") onSources?.(event);
          else if (event.type === "done") { finished = true; onDone?.(); }
          else if (event.type === "error") { finished = true; onError?.(event.message); }
        } catch { /* ignore */ }
      }
    }
    // Audit H11：串流結束卻沒收到 done/error（proxy 逾時、後端重啟、generator 提前結束）
    // → 補呼叫一次 onDone，避免 UI 永遠卡在「進行中」。
    if (!finished) onDone?.();
  };

  // Agent / hybrid mode — SSE stream (default /agent/chat; hybrid uses /agent/route)
  const postAgentStream = async (payload, { onEvent, onFinal, onDone, onError }, url = "/api/v1/agent/chat") => {
    const token = getToken();
    const controller = new AbortController();
    abortRef.current = controller;

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { const err = await resp.json(); detail = err.detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE uses double-newline as event boundary
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";

      for (const block of events) {
        const lines = block.split("\n");
        let eventName = "message";
        let dataStr = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventName = line.slice(7).trim();
          else if (line.startsWith("data: ")) dataStr += line.slice(6);
        }
        if (!dataStr) continue;
        try {
          const data = JSON.parse(dataStr);
          if (eventName === "final") onFinal?.(data);
          else if (eventName === "done") { finished = true; onDone?.(); }
          else if (eventName === "error") { finished = true; onError?.(data.message || "Agent 失敗"); }
          else onEvent?.(eventName, data);
        } catch {
          // ignore malformed event
        }
      }
    }
    // Audit H11：串流沒收到 done/error 就結束 → 補呼叫 onDone，避免永久卡「進行中」。
    if (!finished) onDone?.();
  };

  const runAgentStream = async (question) => {
    setLoading(true);
    setStreaming({
      question,
      thinking: "",
      answer: "",
      isStreaming: true,
      thinkingDone: false,
      sources: [],
      agentMode: true,
      agentSteps: [],
    });

    const historyForAgent = conversationHistory.map((m) => ({
      question: m.question,
      answer: m.answer,
    }));

    try {
      await postAgentStream({ question, conversation_history: historyForAgent, max_steps: 8, top_k: 5 }, {
        onEvent: (eventName, data) => {
          setStreaming((prev) => prev ? {
            ...prev,
            agentSteps: [...(prev.agentSteps || []), { event: eventName, ...data }],
          } : null);
        },
        onFinal: (data) => {
          setStreaming((prev) => prev ? { ...prev, answer: data.text || "", sources: data.sources || [], thinkingDone: true } : null);
        },
        onDone: () => {
          const prev = streamingMsgRef.current;
          if (prev) {
            const newMsg = {
              question: prev.question,
              answer: prev.answer,
              sources: prev.sources || [],
              is_followup: false,
              optimized_query: null,
              thinking: "",
              suggested_questions: [],
              used_ai_fallback: false,
              agentMode: true,
              agentSteps: prev.agentSteps || [],
              timestamp: new Date().toISOString(),
            };
            setConversationHistory((h) => [...h, newMsg]);
          }
          setStreaming(null);
          setLoading(false);
          abortRef.current = null;
        },
        onError: (errMsg) => {
          message.error(errMsg || "Agent 查詢失敗");
          setStreaming(null);
          setLoading(false);
          abortRef.current = null;
        },
      });
    } catch (err) {
      const msg = err?.message || "Agent 查詢失敗";
      if (/abort|cancel/i.test(String(msg))) message.info("已停止查詢"); else message.error(msg);
      setStreaming(null);
      setLoading(false);
      abortRef.current = null;
    }
  };

  // 混合模式 — 打 /agent/route,先收 route 事件(知道被判到哪一邊),再串流 agent 步驟或直接 final
  const runRouteStream = async (question) => {
    setLoading(true);
    setStreaming({
      question, thinking: "", answer: "", isStreaming: true, thinkingDone: false,
      sources: [], agentMode: true, hybrid: true, routedMode: null, agentSteps: [],
    });
    const historyForAgent = conversationHistory.map((m) => ({ question: m.question, answer: m.answer }));
    try {
      await postAgentStream({ question, conversation_history: historyForAgent, max_steps: 8, top_k: 5 }, {
        onEvent: (eventName, data) => {
          if (eventName === "route") {
            // rag 子模式不需要顯示「推理過程」面板
            setStreaming((prev) => prev ? { ...prev, routedMode: data.mode, agentMode: data.mode === "agent" } : null);
            return;
          }
          setStreaming((prev) => prev ? { ...prev, agentSteps: [...(prev.agentSteps || []), { event: eventName, ...data }] } : null);
        },
        onFinal: (data) => {
          setStreaming((prev) => prev ? { ...prev, answer: data.text || "", sources: data.sources || [], thinkingDone: true } : null);
        },
        onDone: () => {
          const prev = streamingMsgRef.current;
          if (prev) {
            const newMsg = {
              question: prev.question, answer: prev.answer, sources: prev.sources || [],
              is_followup: false, optimized_query: null, thinking: "", suggested_questions: [],
              used_ai_fallback: false, agentMode: prev.routedMode === "agent", hybrid: true,
              routedMode: prev.routedMode, agentSteps: prev.agentSteps || [], timestamp: new Date().toISOString(),
            };
            setConversationHistory((h) => [...h, newMsg]);
          }
          setStreaming(null);
          setLoading(false);
          abortRef.current = null;
        },
        onError: (errMsg) => {
          message.error(errMsg || "混合查詢失敗");
          setStreaming(null); setLoading(false); abortRef.current = null;
        },
      }, "/api/v1/agent/route");
    } catch (err) {
      const msg = err?.message || "混合查詢失敗";
      if (/abort|cancel/i.test(String(msg))) message.info("已停止查詢"); else message.error(msg);
      setStreaming(null); setLoading(false); abortRef.current = null;
    }
  };

  const runStream = async (payload, question) => {
    setLoading(true);
    setStreaming({ question, thinking: "", answer: "", isStreaming: true, thinkingDone: false, sources: [], is_followup: false, optimized_query: null });

    try {
      await postRagStream(payload, {
        onThinking: (text) =>
          setStreaming((prev) => prev ? { ...prev, thinking: prev.thinking + text } : null),
        onContent: (text) =>
          setStreaming((prev) => prev ? { ...prev, answer: prev.answer + text } : null),
        onSources: (event) =>
          setStreaming((prev) =>
            prev ? {
              ...prev,
              sources: event.sources ?? [],
              is_followup: event.is_followup ?? false,
              optimized_query: event.optimized_query ?? null,
              thinkingDone: true,
            } : null
          ),
        onDone: () => {
          const prev = streamingMsgRef.current;
          if (prev) {
            const newMsg = {
              question: prev.question,
              answer: prev.answer,
              sources: prev.sources,
              is_followup: prev.is_followup,
              optimized_query: prev.optimized_query,
              thinking: prev.thinking,
              suggested_questions: [],
              used_ai_fallback: false,
              timestamp: new Date().toISOString(),
            };
            setConversationHistory((h) => [...h, newMsg]);
          }
          setStreaming(null);
          setLoading(false);
          abortRef.current = null;
        },
        onError: (errMsg) => {
          message.error(errMsg || "串流查詢失敗");
          setStreaming(null);
          setLoading(false);
          abortRef.current = null;
        },
      });
    } catch (err) {
      const msg = err?.message || "查詢失敗";
      if (/abort|cancel/i.test(String(msg))) message.info("已停止查詢"); else message.error(msg);
      setStreaming(null);
      setLoading(false);
      abortRef.current = null;
    }
  };

  const handleSubmit = async (values) => {
    // Audit H15：串流進行中禁止再送出，否則第二條 stream 會覆寫 abortRef 與
    // streamingMsg，兩條 stream 的內容交錯寫進同一個答案、還各存一筆歷史。
    if (loading) { message.warning("查詢進行中，請稍候或先按停止"); return; }
    const question = values.question?.trim();
    if (!question) { message.warning("請輸入問題"); return; }
    form.setFieldValue("question", "");

    if (qaMode === "agent") { await runAgentStream(question); return; }
    if (qaMode === "hybrid") { await runRouteStream(question); return; }

    const { document_id, folder_ids } = decodeDocScope(values.doc_scope);
    const payload = {
      question,
      top_k: values.top_k ?? 5,
      classification_id: values.classification_id || null,
      project_id: values.project_id || null,
      document_id,
      folder_ids,
      // Audit M5：只送 {question, answer}，與追問一致；送整包（含 sources/agentSteps）
      // 會讓 payload 隨對話暴增，且舊資料缺 answer 時 422。
      conversation_history: conversationHistory.map((m) => ({ question: m.question, answer: m.answer })),
      use_ai_fallback: false,
      skip_ai_understanding: true,
    };
    await runStream(payload, question);
  };

  const changeQaMode = (mode) => {
    setQaMode(mode);
    try { window.localStorage.setItem("qa_mode", mode); } catch { /* ignore */ }
  };

  const handleFollowupSubmit = async () => {
    if (loading) { message.warning("查詢進行中，請稍候或先按停止"); return; }  // audit H15
    const question = followupQuestion?.trim();
    if (!question) { message.warning("請輸入追問內容"); return; }
    setFollowupQuestion("");
    // 追問沿用目前模式。
    if (qaMode === "agent") { await runAgentStream(question); return; }
    if (qaMode === "hybrid") { await runRouteStream(question); return; }
    const currentFormValues = form.getFieldsValue();
    const { document_id, folder_ids } = decodeDocScope(currentFormValues.doc_scope);
    const payload = {
      question,
      top_k: currentFormValues.top_k ?? 5,
      classification_id: currentFormValues.classification_id || null,
      project_id: currentFormValues.project_id || null,
      document_id,
      folder_ids,
      // API 只接受 {question, answer}；送整包訊息物件（含 sources/agentSteps）會 422。
      conversation_history: conversationHistory.map((m) => ({ question: m.question, answer: m.answer })),
      use_ai_fallback: false,
      skip_ai_understanding: false,
    };
    await runStream(payload, question);
  };

  const clearHistory = () => {
    // Audit medium：先中止進行中的串流，否則其 onDone 會把新訊息塞回剛清空的畫面。
    try { abortRef.current?.abort(); } catch { /* ignore */ }
    abortRef.current = null;
    setStreaming(null);
    setLoading(false);
    setExpandedSnippets({});
    setConversationHistory([]);
    apiClient.delete("rag/conversation").catch(() => {});
    message.success("對話歷史已清除");
  };

  const openPdfPreview = (source) => {
    const page = source.page && source.page > 0 ? source.page : 1;
    setPdfPreview({ open: true, documentId: source.document_id, title: source.title, page });
  };

  // 從 sources 去重取得唯一文件清單
  const uniqueDocsFromSources = (sources) => {
    const seen = new Set();
    return (sources || []).filter((s) => {
      if (seen.has(s.document_id)) return false;
      seen.add(s.document_id);
      return true;
    });
  };

  const openSaveNoteModal = (msg) => {
    const docs = uniqueDocsFromSources(msg.sources);
    if (docs.length === 0) { message.warning("此回答無引用文件，無法儲存筆記"); return; }
    setSaveNoteDocId(docs[0].document_id); // 預設選第一份
    setSaveNoteModal({ visible: true, msg });
  };

  const handleSaveNote = async () => {
    if (!saveNoteDocId) { message.warning("請選擇要儲存的文件"); return; }
    try {
      setSaveNoteLoading(true);
      const sources = saveNoteModal.msg?.sources || [];
      const sourcesSection = sources.length > 0
        ? "\n\n---\n**📎 參考來源**\n" +
          sources.map((s, i) =>
            `${i + 1}. [${s.title}${s.page ? ` — 第 ${s.page} 頁` : ""}](/documents/${s.document_id}${s.page ? `?page=${s.page}` : ""})` +
            (s.score != null ? `（相似度 ${s.score.toFixed(2)}）` : "")
          ).join("\n")
        : "";
      await apiClient.post(`documents/${saveNoteDocId}/notes`, {
        question: saveNoteModal.msg.question,
        answer: saveNoteModal.msg.answer + sourcesSection,
      });
      message.success("筆記已儲存");
      setSaveNoteModal({ visible: false, msg: null });
    } catch {
      message.error("儲存失敗");
    } finally {
      setSaveNoteLoading(false);
    }
  };

  // Build TreeSelect data
  const [docScopeExpandedKeys, setDocScopeExpandedKeys] = useState(["__all_docs__"]);

  const documentTreeData = useMemo(() => {
    // Folder subtree
    const folderMap = {};
    folders.forEach((f) => {
      folderMap[f.id] = {
        title: f.name,
        value: `folder:${f.id}`,
        key: `folder:${f.id}`,
        children: [],
      };
    });
    const folderRoots = [];
    folders.forEach((f) => {
      if (f.parent_id && folderMap[f.parent_id]) {
        folderMap[f.parent_id].children.push(folderMap[f.id]);
      } else {
        folderRoots.push(folderMap[f.id]);
      }
    });

    // All documents as leaf nodes
    const allDocLeaves = documents.map((doc) => ({
      title: doc.title,
      value: `doc:${doc.id}`,
      key: `doc:${doc.id}`,
      isLeaf: true,
    }));

    const result = [];
    if (allDocLeaves.length > 0) {
      result.push({
        title: `所有文件 (${allDocLeaves.length})`,
        value: "__all_docs__",
        key: "__all_docs__",
        disabled: true,
        children: allDocLeaves,
      });
    }
    if (folderRoots.length > 0) {
      result.push({
        title: "依資料夾篩選",
        value: "__folders__",
        key: "__folders__",
        disabled: true,
        children: folderRoots,
      });
    }
    return result;
  }, [folders, documents]);

  // Expand "所有文件" and all folder nodes when data loads
  useEffect(() => {
    const keys = ["__all_docs__", "__folders__", ...folders.map((f) => `folder:${f.id}`)];
    setDocScopeExpandedKeys(keys);
  }, [folders, documents]);

  // Get all descendant folder IDs (including the folder itself)
  const getDescendantFolderIds = (folderId, allFolders) => {
    const result = [folderId];
    allFolders.filter((f) => f.parent_id === folderId).forEach((child) => {
      result.push(...getDescendantFolderIds(child.id, allFolders));
    });
    return result;
  };

  // Decode doc_scope selection to { document_id, folder_ids }
  const decodeDocScope = (selection) => {
    if (!selection) return { document_id: null, folder_ids: null };
    if (selection.startsWith("doc:")) return { document_id: selection.slice(4), folder_ids: null };
    if (selection.startsWith("folder:")) {
      const fid = selection.slice(7);
      return { document_id: null, folder_ids: getDescendantFolderIds(fid, folders) };
    }
    return { document_id: null, folder_ids: null };
  };

  const classificationOptions = useMemo(
    () => classifications.map((item) => ({ value: item.id, label: item.code ? `${item.name} (${item.code})` : item.name })),
    [classifications]
  );
  const projectSelectOptions = useMemo(
    () => projectOptions.map((item) => ({ value: item.value, label: item.display_value })),
    [projectOptions]
  );
  // Shared ReactMarkdown components for styled table rendering
  const markdownComponents = {
    table: ({ node, ...props }) => (
      <div style={{ overflowX: "auto", marginBottom: 8 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 14 }} {...props} />
      </div>
    ),
    thead: ({ node, ...props }) => <thead style={{ background: "#fafafa" }} {...props} />,
    th: ({ node, ...props }) => (
      <th style={{ border: "1px solid #d9d9d9", padding: "6px 12px", fontWeight: 600, textAlign: "left", whiteSpace: "nowrap" }} {...props} />
    ),
    td: ({ node, ...props }) => (
      <td style={{ border: "1px solid #d9d9d9", padding: "6px 12px" }} {...props} />
    ),
    tr: ({ node, ...props }) => <tr style={{ borderBottom: "1px solid #f0f0f0" }} {...props} />,
  };

  // Render thinking collapse for a message (history or live)
  const renderThinking = (thinking, isLive, thinkingDone) => {
    if (!thinking) return null;
    const labelText = isLive && !thinkingDone ? "思考中..." : "思考過程";
    // Force-expand only while streaming thinking; afterwards let user control it freely
    const forceProps = isLive && !thinkingDone ? { activeKey: ["t"] } : {};
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 12, background: "#fffbe6", border: "1px solid #ffe58f" }}
        {...forceProps}
        items={[{
          key: "t",
          label: (
            <Text type="secondary" style={{ fontSize: 12 }}>
              <BulbOutlined style={{ marginRight: 4 }} />{labelText}
            </Text>
          ),
          children: (
            <Text style={{ fontSize: 12, whiteSpace: "pre-wrap", color: "#888" }}>{thinking}</Text>
          ),
        }]}
      />
    );
  };

  // Render source list
  const renderSources = (sources, msgIndex) => {
    if (!sources || sources.length === 0) return null;
    return (
      <div style={{ marginTop: 16 }}>
        <Divider orientation="left" style={{ fontSize: 13, marginTop: 16, marginBottom: 12 }}>
          參考來源({sources.length})
        </Divider>
        <List
          size="small"
          dataSource={sources}
          renderItem={(source, idx) => {
            const key = `${msgIndex}-${idx}`;
            const isExpanded = !!expandedSnippets[key];
            const full = stripFenceWrapper(source.snippet || "");
            const needsTruncate = full.length > 200;
            const displaySnippet = needsTruncate && !isExpanded ? full.slice(0, 200) : full;
            return (
              <List.Item key={key}>
                <List.Item.Meta
                  title={
                    <Space size={8} wrap>
                      <Tag color="green">來源 {idx + 1}</Tag>
                      <Text>{source.title || "(未命名文件)"}{typeof source.page === "number" ? ` - 第 ${source.page} 頁` : ""}</Text>
                      {source.score != null && (<Text type="secondary">(相似度 {source.score.toFixed(3)})</Text>)}
                      <Button size="small" onClick={() => openPdfPreview(source)}>預覽</Button>
                    </Space>
                  }
                  description={
                    <div>
                      {(!needsTruncate || isExpanded) ? (
                        // 完整顯示時：以 Markdown 渲染（表格、清單等），比較美觀
                        <div className="markdown-content" style={{ fontSize: 13, color: "#595959" }}>
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                            {full}
                          </ReactMarkdown>
                        </div>
                      ) : (
                        // 收合預覽時：純文字截斷（避免顯示切一半的表格），快速掃視
                        <Text style={{ whiteSpace: "pre-wrap" }}>
                          {displaySnippet}...
                        </Text>
                      )}
                      {needsTruncate && (
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: "0 0 0 4px", height: "auto", fontSize: 12 }}
                          onClick={() => setExpandedSnippets((prev) => ({ ...prev, [key]: !prev[key] }))}
                        >
                          {isExpanded ? "收起" : "展開全文"}
                        </Button>
                      )}
                    </div>
                  }
                />
              </List.Item>
            );
          }}
        />
      </div>
    );
  };

  const renderAgentSteps = (steps, isLive) => {
    if (!steps || steps.length === 0) {
      if (isLive) {
        return (
          <div style={{ padding: "8px 0 12px" }}>
            <Text type="secondary" style={{ fontSize: 12, fontStyle: "italic" }}>
              <RobotOutlined /> Agent 啟動中...
            </Text>
          </div>
        );
      }
      return null;
    }
    const items = steps
      .filter((s) => s.event === "thought" || s.event === "tool_call" || s.event === "observation")
      .map((s) => {
        if (s.event === "thought") {
          return {
            color: "blue",
            dot: <BulbOutlined style={{ fontSize: 14 }} />,
            children: (
              <div>
                <Text strong style={{ fontSize: 12, color: "#1677ff" }}>思考 #{s.step}</Text>
                <div style={{ fontSize: 13, color: "#555" }}>{s.text}</div>
              </div>
            ),
          };
        }
        if (s.event === "tool_call") {
          return {
            color: "purple",
            dot: <ToolOutlined style={{ fontSize: 14 }} />,
            children: (
              <div>
                <Text strong style={{ fontSize: 12, color: "#722ed1" }}>呼叫 {s.tool}</Text>
                <pre style={{ fontSize: 11, background: "#f0f0f0", padding: 6, borderRadius: 4, overflow: "auto", maxHeight: 100, marginTop: 4 }}>
                  {JSON.stringify(s.input || {}, null, 2)}
                </pre>
              </div>
            ),
          };
        }
        // observation
        const outStr = JSON.stringify(s.output || {}, null, 2);
        const preview = outStr.length > 400 ? outStr.slice(0, 400) + "..." : outStr;
        return {
          color: "green",
          dot: <EyeOutlined style={{ fontSize: 14 }} />,
          children: (
            <div>
              <Text strong style={{ fontSize: 12, color: "#52c41a" }}>觀察 ({s.tool})</Text>
              <pre style={{ fontSize: 11, background: "#f6ffed", padding: 6, borderRadius: 4, overflow: "auto", maxHeight: 200, marginTop: 4 }}>
                {preview}
              </pre>
            </div>
          ),
        };
      });
    return (
      <Collapse
        size="small"
        ghost
        defaultActiveKey={isLive ? ["agent"] : []}
        style={{ marginBottom: 8 }}
        items={[
          {
            key: "agent",
            label: (
              <Space>
                <RobotOutlined style={{ color: "#1677ff" }} />
                <Text strong style={{ fontSize: 13 }}>Agent 推理過程</Text>
                <Tag color="blue" style={{ fontSize: 11 }}>{items.length} 步</Tag>
                {isLive && !steps.some((s) => s.event === "final") && (
                  <Tag color="processing" style={{ fontSize: 11 }}>進行中</Tag>
                )}
              </Space>
            ),
            children: <Timeline items={items} style={{ marginTop: 8 }} />,
          },
        ]}
      />
    );
  };

  return (
    <AppLayout>
      <Row gutter={16}>
        <Col xs={24} lg={8}>
          <Card
            title="查詢設定"
            style={{ position: "sticky", top: 16 }}
            extra={
              <Tooltip title="混合(預設):自動判斷問題類型 — 內容題(怎麼進行/數值)走純 RAG(快、完整);關係題(引用/取代/版本/有哪些子項)走 Agent(KG 工具)。也可手動鎖定純 RAG 或 Agent。">
                <Space size={4} style={{ cursor: "help" }}>
                  <Segmented
                    size="small"
                    value={qaMode}
                    onChange={changeQaMode}
                    options={[
                      { label: "純RAG", value: "rag" },
                      { label: "混合", value: "hybrid" },
                      { label: "Agent", value: "agent" },
                    ]}
                  />
                  <QuestionCircleOutlined style={{ fontSize: 11, color: "#bbb" }} />
                </Space>
              </Tooltip>
            }
          >
            {qaMode === "hybrid" && (
              <Alert
                style={{ marginBottom: 12 }}
                type="info"
                showIcon
                message="混合模式（自動路由）"
                description="系統自動判定:內容題走純 RAG（快、完整）、關係題走 Agent（跨規範追引用/版本/結構）。每次查詢會標示實際走了哪一邊。"
              />
            )}
            {qaMode === "agent" && (
              <Alert
                style={{ marginBottom: 12 }}
                type="info"
                showIcon
                message="Agent 模式已啟用"
                description="會自動跨規範追引用、查版本鏈,單次查詢較慢(3-15s),適合「我這產品要符合什麼?」這類綜合性問題。"
              />
            )}
            <Form form={form} layout="vertical" initialValues={{ top_k: 5 }} onFinish={handleSubmit}>
              <Form.Item name="question" label="請輸入問題" rules={[{ required: true, message: "請輸入想查詢的問題" }]}>
                <Input.TextArea rows={4} placeholder="例：某規範流程？或追問上一題關鍵數值" allowClear onPressEnter={(e) => { if (e.ctrlKey || e.metaKey) { form.submit(); } }} />
              </Form.Item>
              <Form.Item name="classification_id" label="分類">
                <Select allowClear showSearch placeholder="選擇分類（可留空）" options={classificationOptions} optionFilterProp="label" />
              </Form.Item>
              <Form.Item name="project_id" label="專案">
                <Select allowClear showSearch placeholder="選擇專案（可留空）" options={projectSelectOptions} optionFilterProp="label" />
              </Form.Item>
              <Form.Item name="doc_scope" label="資料夾 / 文件">
                <TreeSelect
                  allowClear
                  showSearch
                  treeNodeFilterProp="title"
                  placeholder="選擇資料夾或特定文件（可留空）"
                  treeData={documentTreeData}
                  treeExpandedKeys={docScopeExpandedKeys}
                  onTreeExpand={setDocScopeExpandedKeys}
                  listHeight={400}
                  getPopupContainer={() => document.body}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item name="top_k" label="來源筆數">
                <InputNumber min={1} max={10} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  <Button type="primary" htmlType="submit" loading={loading} icon={<SendOutlined />} style={{ flex: "1 1 auto" }}>
                    送出查詢 (Ctrl+Enter)
                  </Button>
                  {loading && (
                    <Button danger icon={<StopOutlined />} onClick={stopInFlight}>
                      停止
                    </Button>
                  )}
                  {conversationHistory.length > 0 && (
                    <Button onClick={clearHistory} icon={<DeleteOutlined />} danger>
                      清除歷史
                    </Button>
                  )}
                </div>
              </Form.Item>
              <Alert message="提示" description="此為新問題輸入，問題將直接用於文字檢索；追問會使用 AI 理解功能，請使用右側下方的追問輸入。" type="info" showIcon icon={<QuestionCircleOutlined />} />
            </Form>
          </Card>
        </Col>
        <Col xs={24} lg={16}>
          <Card title={`對話記錄 (${conversationHistory.length})`}>
            {conversationHistory.length === 0 && !streamingMsg ? (
              <Empty description="尚未開始對話，請先在左側輸入問題以開始查詢" style={{ padding: "48px 0" }} />
            ) : (
              <div style={{ maxHeight: "calc(100vh - 200px)", overflowY: "auto", padding: "0 8px" }}>
                {/* Completed conversation history */}
                {conversationHistory.map((msg, index) => (
                  <div key={index} style={{ marginBottom: 32 }}>
                    <div style={{ marginBottom: 16 }}>
                      <Tag color="blue" style={{ fontSize: 14, padding: "4px 12px" }}>問題 {index + 1}</Tag>
                      {msg.is_followup && (<Tag color="orange" style={{ marginLeft: 8 }}>追問</Tag>)}
                      {msg.hybrid && msg.routedMode && (
                        <Tag color={msg.routedMode === "agent" ? "geekblue" : "green"} style={{ marginLeft: 8 }}>
                          混合→{msg.routedMode === "agent" ? "關係查詢(Agent)" : "內容查詢(RAG)"}
                        </Tag>
                      )}
                      <Text strong style={{ fontSize: 16, marginLeft: 8 }}>{msg.question}</Text>
                      {msg.optimized_query && msg.optimized_query !== msg.question && (
                        <div style={{ marginTop: 8, paddingLeft: 12, borderLeft: "3px solid #1890ff" }}>
                          <Text type="secondary" style={{ fontSize: 13 }}>AI 理解：{msg.optimized_query}</Text>
                        </div>
                      )}
                    </div>
                    <Card size="small" style={{ background: "#f9f9f9", borderLeft: msg.agentMode ? "4px solid #1677ff" : (msg.used_ai_fallback ? "4px solid #faad14" : "4px solid #52c41a") }}>
                      {msg.used_ai_fallback && (
                        <Alert message="AI 一般知識回答" description="此答案由 AI 一般知識庫產生，可能不來自系統文件內容" type="warning" showIcon style={{ marginBottom: 12 }} />
                      )}
                      {/* Agent steps (collapsed in history) */}
                      {msg.agentMode && renderAgentSteps(msg.agentSteps, false)}
                      {/* Thinking section: collapsed by default in history */}
                      {!msg.agentMode && renderThinking(msg.thinking, false, true)}
                      <div style={{ fontSize: 15, lineHeight: 1.8 }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{msg.answer}</ReactMarkdown>
                      </div>
                      {renderSources(msg.sources, index)}
                      {msg.sources?.length > 0 && (
                        <div style={{ marginTop: 12, textAlign: "right" }}>
                          <Button
                            size="small"
                            icon={<SaveOutlined />}
                            onClick={() => openSaveNoteModal(msg)}
                          >
                            儲存筆記
                          </Button>
                        </div>
                      )}
                    </Card>
                    {(index < conversationHistory.length - 1 || streamingMsg) && <Divider />}
                  </div>
                ))}

                {/* Live streaming message */}
                {streamingMsg && (
                  <div style={{ marginBottom: 32 }}>
                    <div style={{ marginBottom: 16 }}>
                      <Tag color="blue" style={{ fontSize: 14, padding: "4px 12px" }}>問題 {conversationHistory.length + 1}</Tag>
                      {streamingMsg.is_followup && <Tag color="orange" style={{ marginLeft: 8 }}>追問</Tag>}
                      {streamingMsg.hybrid && (
                        <Tag color={streamingMsg.routedMode === "agent" ? "geekblue" : (streamingMsg.routedMode === "rag" ? "green" : "default")} style={{ marginLeft: 8 }}>
                          {streamingMsg.routedMode ? `混合→${streamingMsg.routedMode === "agent" ? "關係查詢(Agent)" : "內容查詢(RAG)"}` : "混合判定中…"}
                        </Tag>
                      )}
                      <Text strong style={{ fontSize: 16, marginLeft: 8 }}>{streamingMsg.question}</Text>
                      {streamingMsg.optimized_query && streamingMsg.optimized_query !== streamingMsg.question && (
                        <div style={{ marginTop: 8, paddingLeft: 12, borderLeft: "3px solid #1890ff" }}>
                          <Text type="secondary" style={{ fontSize: 13 }}>AI 理解：{streamingMsg.optimized_query}</Text>
                        </div>
                      )}
                    </div>
                    <Card size="small" style={{ background: "#f9f9f9", borderLeft: streamingMsg.agentMode ? "4px solid #1677ff" : "4px solid #52c41a" }}>
                      {/* Agent steps timeline (when in agent mode) */}
                      {streamingMsg.agentMode && renderAgentSteps(streamingMsg.agentSteps, true)}
                      {/* Thinking: expanded while thinking, collapsed after done */}
                      {!streamingMsg.agentMode && renderThinking(streamingMsg.thinking, true, streamingMsg.thinkingDone)}
                      <div style={{ fontSize: 15, lineHeight: 1.8 }}>
                        {streamingMsg.answer ? (
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{streamingMsg.answer}</ReactMarkdown>
                        ) : (
                          <Text type="secondary" style={{ fontStyle: "italic" }}>
                            {streamingMsg.agentMode ? "Agent 推理中..." : (streamingMsg.thinkingDone ? "生成回答中..." : "AI 思考中...")}
                          </Text>
                        )}
                      </div>
                      {streamingMsg.thinkingDone && renderSources(streamingMsg.sources, "live")}
                    </Card>
                  </div>
                )}

                <div ref={conversationEndRef} />
              </div>
            )}

            {/* Followup input */}
            {(conversationHistory.length > 0 || streamingMsg) && (
              <div style={{ marginTop: 16, borderTop: "2px solid #e8e8e8", paddingTop: 12 }}>
                <div style={{ marginBottom: 8, fontSize: 13, color: "#52c41a" }}>
                  <QuestionCircleOutlined style={{ marginRight: 6 }} />
                  追問輸入列 - AI 將延續上下文並優化您的提示
                </div>
                <Space.Compact style={{ width: "100%" }}>
                  <Input.TextArea
                    value={followupQuestion}
                    onChange={(e) => setFollowupQuestion(e.target.value)}
                    placeholder="例如：想知道更多？更詳細說明？"
                    rows={1}
                    autoSize={{ minRows: 1, maxRows: 4 }}
                    onPressEnter={(e) => { if (e.ctrlKey || e.metaKey) { e.preventDefault(); handleFollowupSubmit(); } }}
                    disabled={loading}
                  />
                  <Button type="primary" icon={<SendOutlined />} onClick={handleFollowupSubmit} loading={loading}>追問</Button>
                  {loading && (<Button danger icon={<StopOutlined />} onClick={stopInFlight}>停止</Button>)}
                </Space.Compact>
              </div>
            )}
          </Card>
        </Col>
      </Row>
      {/* 儲存筆記 Modal */}
      <Modal
        title={<Space><SaveOutlined />儲存筆記至文件</Space>}
        open={saveNoteModal.visible}
        onCancel={() => setSaveNoteModal({ visible: false, msg: null })}
        onOk={handleSaveNote}
        confirmLoading={saveNoteLoading}
        okText="儲存"
        cancelText="取消"
        width={560}
      >
        <div style={{ marginBottom: 16 }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            此回答引用了以下文件，請選擇要歸入哪份文件的筆記本：
          </Text>
        </div>
        <Select
          style={{ width: "100%", marginBottom: 16 }}
          value={saveNoteDocId}
          onChange={setSaveNoteDocId}
          options={uniqueDocsFromSources(saveNoteModal.msg?.sources).map((s) => ({
            value: s.document_id,
            label: `${s.title}${s.score != null ? `（相似度 ${s.score.toFixed(2)}）` : ""}`,
          }))}
        />
        <div style={{ marginBottom: 8 }}>
          <Text strong>問題（筆記標題）：</Text>
          <div style={{ padding: "6px 10px", background: "#f5f5f5", borderRadius: 4, marginTop: 4, fontSize: 14 }}>
            {saveNoteModal.msg?.question}
          </div>
        </div>
        <div>
          <Text strong>內容預覽：</Text>
          <div style={{
            padding: "6px 10px", background: "#f5f5f5", borderRadius: 4, marginTop: 4,
            fontSize: 13, maxHeight: 160, overflowY: "auto", whiteSpace: "pre-wrap", color: "#595959"
          }}>
            {(saveNoteModal.msg?.answer || "").slice(0, 400)}{(saveNoteModal.msg?.answer || "").length > 400 ? "…" : ""}
          </div>
        </div>
      </Modal>

      <PdfPreviewModal
        open={pdfPreview.open}
        documentId={pdfPreview.documentId}
        title={pdfPreview.title}
        initialPage={pdfPreview.page}
        onClose={() => setPdfPreview({ open: false, documentId: null, title: "", page: 1 })}
      />
    </AppLayout>
  );
};

export default QAConsolePage;
