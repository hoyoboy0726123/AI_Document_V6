import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Descriptions, Select, Space, Spin, Tag, message, Modal, Input, Tabs, Table, Typography, Statistic, Row, Col, Tooltip, Badge, Popconfirm, Slider } from "antd";
import { FilePdfOutlined, ExclamationCircleOutlined, EditOutlined, DeleteOutlined, BookOutlined, PlusOutlined, MinusOutlined, DatabaseOutlined, ReloadOutlined, MergeCellsOutlined, ScissorOutlined, TagOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import { Document, Page, pdfjs } from "react-pdf";
import apiClient from "../../services/api";
import useAuthStore from "../../stores/authStore";
import PdfPreviewModal from "./PdfPreviewModal";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.mjs",
  import.meta.url,
).toString();
import "./DocumentDetail.css";

const DocumentDetail = ({ documentId, initialPage, initialHighlightKeyword, onBack, onEdit }) => {
  const { token } = useAuthStore();
  const [document, setDocument] = useState(null);
  const [loading, setLoading] = useState(false);
  const [pdfPreviewVisible, setPdfPreviewVisible] = useState(false);
  const [notes, setNotes] = useState([]);
  const [editingNote, setEditingNote] = useState(null);
  const [isEditNoteModalVisible, setIsEditNoteModalVisible] = useState(false);
  const [editNoteForm, setEditNoteForm] = useState({ question: "", answer: "" });

  const [isCreateNoteModalVisible, setIsCreateNoteModalVisible] = useState(false);
  const [createNoteForm, setCreateNoteForm] = useState({ question: "", answer: "" });

  // 重新向量化狀態
  const [reVectorizing, setReVectorizing] = useState(false);

  // 高精度重跑 OCR 狀態
  const [reocrLoading, setReocrLoading] = useState(false);
  // 文件是否正在後端處理（OCR / 向量化重建）+ 進行中的任務（顯示進度、避免重複觸發）
  const [polling, setPolling] = useState(false);
  const [ocrTask, setOcrTask] = useState(null);

  // 向量塊管理狀態
  const [chunks, setChunks] = useState(null); // null = 未載入
  const [chunksLoading, setChunksLoading] = useState(false);
  const [editingChunk, setEditingChunk] = useState(null);
  const [editChunkText, setEditChunkText] = useState("");
  const [chunkSaving, setChunkSaving] = useState(false);
  const [addChunkVisible, setAddChunkVisible] = useState(false);
  const [newChunk, setNewChunk] = useState({ page: "", text: "" });
  const [addingChunk, setAddingChunk] = useState(false);
  const [expandedChunkId, setExpandedChunkId] = useState(null);
  const [selectedChunkIds, setSelectedChunkIds] = useState([]);
  const [merging, setMerging] = useState(false);
  const [splittingChunk, setSplittingChunk] = useState(null);
  const [splitAt, setSplitAt] = useState(0);
  const [splitting, setSplitting] = useState(false);
  const [prefixModalVisible, setPrefixModalVisible] = useState(false);
  const [prefixText, setPrefixText] = useState("");
  const [prefixTargetChunk, setPrefixTargetChunk] = useState(null); // null = 批次模式
  const [prefixSaving, setPrefixSaving] = useState(false);
  const [selectedTags, setSelectedTags] = useState([]);
  const [viewingNote, setViewingNote] = useState(null); // 純閱讀筆記

  // 快速頁面預覽
  const [quickPdfPage, setQuickPdfPage] = useState(null);
  const [quickScale, setQuickScale] = useState(1.2);
  const [quickDragging, setQuickDragging] = useState(false);
  const [quickDragStart, setQuickDragStart] = useState({ x: 0, y: 0, scrollLeft: 0, scrollTop: 0 });
  const quickPdfContainerRef = useRef(null);

  const fetchDocument = async () => {
    if (!documentId) return;
    try {
      setLoading(true);
      const [docResp, notesResp] = await Promise.all([
        apiClient.get(`documents/${documentId}`),
        apiClient.get(`documents/${documentId}/notes`)
      ]);
      setDocument(docResp.data);
      setNotes(notesResp.data);
    } catch (error) {
      message.error(error.response?.data?.detail ?? "載入文件失敗");
    } finally {
      setLoading(false);
    }
  };

  const fetchNotes = async () => {
    try {
      const resp = await apiClient.get(`documents/${documentId}/notes`);
      setNotes(resp.data);
    } catch (error) {
      console.error("Fetch notes failed", error);
    }
  };

  const handleDeleteNote = async (noteId) => {
    Modal.confirm({
      title: '刪除筆記',
      content: '確定要刪除這則筆記嗎？',
      okText: '刪除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await apiClient.delete(`documents/${documentId}/notes/${noteId}`);
          message.success('筆記已刪除');
          fetchNotes();
        } catch (error) {
          message.error('刪除失敗');
        }
      },
    });
  };

  const openEditNoteModal = (note) => {
    setEditingNote(note);
    setEditNoteForm({ question: note.question, answer: note.answer });
    setIsEditNoteModalVisible(true);
  };

  const handleUpdateNote = async () => {
    try {
      await apiClient.put(`documents/${documentId}/notes/${editingNote.id}`, editNoteForm);
      message.success('筆記已更新');
      setIsEditNoteModalVisible(false);
      fetchNotes();
    } catch (error) {
      message.error('更新失敗');
    }
  };

  const handleCreateNote = async () => {
    if (!createNoteForm.question.trim() || !createNoteForm.answer.trim()) {
      message.warning("請填寫問題與內容");
      return;
    }
    try {
      await apiClient.post(`documents/${documentId}/notes`, createNoteForm);
      message.success('筆記已建立');
      setIsCreateNoteModalVisible(false);
      setCreateNoteForm({ question: "", answer: "" });
      fetchNotes();
    } catch (error) {
      message.error('建立失敗');
    }
  };

  const getRandomEmoji = (id) => {
    const emojis = ['📓', '💡', '🧠', '⚙️', '💻', '📚', '📝', '🎨', '🚀', '🧩'];
    let hash = 0;
    for (let i = 0; i < id.length; i++) {
      hash = id.charCodeAt(i) + ((hash << 5) - hash);
    }
    return emojis[Math.abs(hash) % emojis.length];
  };

  const NOTE_COLORS = [
    '#FFF7E0', // Light Yellow
    '#E3F2FD', // Light Blue
    '#F3E5F5', // Light Purple
    '#E0F2F1', // Light Teal
    '#FBE9E7', // Light Orange
    '#FCE4EC', // Light Pink
    '#E8EAF6', // Light Indigo
    '#F1F8E9', // Light Green
  ];

  useEffect(() => {
    fetchDocument();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId]);

  // 若文件本身狀態就是 processing（例如重新整理進來時 OCR 還沒跑完）→ 自動開始輪詢
  useEffect(() => {
    if (document?.ocr_status === "processing") setPolling(true);
  }, [document?.ocr_status]);

  // 處理中：每 5 秒輪詢文件狀態與進行中的任務；完成後刷新內容並停止
  useEffect(() => {
    if (!polling) return undefined;
    let cancelled = false;
    const INFLIGHT = ["reocr", "vectorize", "vl_vectorize", "ocr_vectorize"];
    const tick = async () => {
      try {
        const [tRes, dRes] = await Promise.all([
          apiClient.get("tasks/"),
          apiClient.get(`documents/${documentId}`),
        ]);
        if (cancelled) return;
        const t = (tRes.data || []).find(
          (x) => x.document_id === documentId &&
            INFLIGHT.includes(x.task_type) &&
            ["pending", "running", "processing"].includes(x.status)
        );
        setOcrTask(t || null);
        setDocument(dRes.data);
        if (dRes.data.ocr_status !== "processing" && !t) {
          setPolling(false);
          setOcrTask(null);
          message.success("OCR / 向量化已完成，內容已更新");
          if (chunks) fetchChunks();
        }
      } catch { /* 短暫失敗忽略，下輪再試 */ }
    };
    const id = setInterval(tick, 5000);
    tick();
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling, documentId]);

  // 是否正在後端處理中（processing 狀態或有進行中的任務）
  const isProcessing = document?.ocr_status === "processing" || polling || !!ocrTask;

  // 如果有初始頁面參數（來自搜尋結果），自動開啟 PDF 預覽
  useEffect(() => {
    if (document && initialPage) {
      setPdfPreviewVisible(true);
    }
  }, [document, initialPage]);


  const handleReVectorize = () => {
    if (!documentId) {
      return;
    }

    Modal.confirm({
      title: '重新向量化',
      icon: <ExclamationCircleOutlined />,
      content: '將重新計算此文件的向量索引，建議僅在內容變更後執行。',
      okText: '確認',
      cancelText: '取消',
      onOk: async () => {
        try {
          setReVectorizing(true);
          await apiClient.post(`documents/${documentId}/re-vectorize`);
          message.success('已重新向量化文件');
          await fetchDocument();
        } catch (error) {
          message.error(error.response?.data?.detail ?? '重新向量化失敗');
        } finally {
          setReVectorizing(false);
        }
      },
    });
  };

  const handleReocr = () => {
    if (!documentId) return;
    Modal.confirm({
      title: '高精度重跑 OCR',
      icon: <ExclamationCircleOutlined />,
      content:
        '將以 server 等級重新辨識此文件（含表格），重建向量索引。準確度最高，但在 CPU 上很慢（每頁約 1 分鐘），背景執行；GPU 正式機會快很多。',
      okText: '開始重跑',
      cancelText: '取消',
      onOk: async () => {
        try {
          setReocrLoading(true);
          const resp = await apiClient.post(`documents/${documentId}/reocr`, { engine: 'rapid', tier: 'server' });
          // 後端同文件去重：若已有進行中任務會回傳既有任務，前端一律開始輪詢進度
          setOcrTask(resp.data || null);
          setPolling(true);
          message.success('已開始高精度重跑 OCR，下方會顯示進度（背景執行，可離開此頁）');
        } catch (error) {
          message.error(error.response?.data?.detail ?? '重跑 OCR 失敗');
        } finally {
          setReocrLoading(false);
        }
      },
    });
  };

  // ── 向量塊操作 ────────────────────────────────────────────────────────────
  const fetchChunks = async () => {
    if (!documentId) return;
    try {
      setChunksLoading(true);
      const resp = await apiClient.get(`documents/${documentId}/chunks`);
      setChunks(resp.data);
    } catch (error) {
      message.error("載入向量塊失敗");
    } finally {
      setChunksLoading(false);
    }
  };

  const handleSaveChunk = async () => {
    if (!editingChunk || !editChunkText.trim()) return;
    try {
      setChunkSaving(true);
      await apiClient.put(`documents/${documentId}/chunks/${editingChunk.id}`, { text: editChunkText });
      message.success("向量塊已更新並重新向量化");
      setEditingChunk(null);
      fetchChunks();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "更新失敗");
    } finally {
      setChunkSaving(false);
    }
  };

  const handleDeleteChunk = async (chunkId) => {
    try {
      await apiClient.delete(`documents/${documentId}/chunks/${chunkId}`);
      message.success("向量塊已刪除");
      fetchChunks();
    } catch (error) {
      message.error("刪除失敗");
    }
  };

  const handleAddChunk = async () => {
    if (!newChunk.text.trim()) { message.warning("請輸入文字內容"); return; }
    try {
      setAddingChunk(true);
      await apiClient.post(`documents/${documentId}/chunks`, {
        page: newChunk.page ? parseInt(newChunk.page) : null,
        text: newChunk.text,
      });
      message.success("向量塊已新增並向量化");
      setAddChunkVisible(false);
      setNewChunk({ page: "", text: "" });
      fetchChunks();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "新增失敗");
    } finally {
      setAddingChunk(false);
    }
  };

  const handleMergeChunks = async () => {
    if (selectedChunkIds.length < 2) { message.warning("請至少選取 2 個向量塊"); return; }
    try {
      setMerging(true);
      await apiClient.post(`documents/${documentId}/chunks/merge`, { chunk_ids: selectedChunkIds });
      message.success("已合併並重新向量化");
      setSelectedChunkIds([]);
      fetchChunks();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "合併失敗");
    } finally {
      setMerging(false);
    }
  };

  const handleSplitChunk = async () => {
    if (!splittingChunk || splitAt <= 0 || splitAt >= splittingChunk.text.length) {
      message.warning("請選擇有效的分割位置");
      return;
    }
    try {
      setSplitting(true);
      await apiClient.post(`documents/${documentId}/chunks/${splittingChunk.id}/split`, { split_at: splitAt });
      message.success("已分割並重新向量化");
      setSplittingChunk(null);
      fetchChunks();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "分割失敗");
    } finally {
      setSplitting(false);
    }
  };

  // 標籤建議：從文件關鍵字、分類名稱、現有塊前綴第一行中收集
  const tagSuggestions = useMemo(() => {
    const set = new Set();
    (document?.keywords ?? []).forEach((k) => k && set.add(k));
    if (document?.classification?.name) set.add(document.classification.name);
    (chunks?.items ?? []).forEach((chunk) => {
      const firstLine = (chunk.text || "").split("\n")[0].trim();
      // 將看起來像前綴的第一行（短且含冒號或句號）拆回標籤
      if (firstLine.length > 0 && firstLine.length < 80) {
        const cleaned = firstLine.replace(/^以下內容涉及[：:]\s*/i, "").replace(/。$/, "");
        cleaned.split(/[；;、,，]/).forEach((t) => {
          const tag = t.trim();
          if (tag) set.add(tag);
        });
      }
    });
    return [...set].map((v) => ({ label: v, value: v }));
  }, [document, chunks]);

  const quickFileUrl = useMemo(() => {
    if (!documentId) return null;
    return {
      url: `/api/v1/documents/${documentId}/pdf`,
      httpHeaders: token ? { Authorization: `Bearer ${token}` } : {},
      withCredentials: false,
    };
  }, [documentId, token]);

  // 選標籤後自動組成自然語言前綴（可手動繼續修改）
  const handleTagsChange = (tags) => {
    setSelectedTags(tags);
    if (tags.length === 0) { setPrefixText(""); return; }
    const joined = tags.join("；");
    setPrefixText(`以下內容涉及：${joined}。`);
  };

  const handleApplyPrefix = async () => {
    if (!prefixText.trim()) { message.warning("請輸入前綴文字"); return; }
    const targets = prefixTargetChunk
      ? [prefixTargetChunk]
      : (chunks?.items ?? []).filter((c) => selectedChunkIds.includes(c.id));
    if (!targets.length) { message.warning("沒有選取到向量塊"); return; }
    try {
      setPrefixSaving(true);
      await Promise.all(
        targets.map((chunk) =>
          apiClient.put(`documents/${documentId}/chunks/${chunk.id}`, {
            text: prefixText.trim() + "\n" + chunk.text,
          })
        )
      );
      message.success(`已為 ${targets.length} 個向量塊加入前綴並重新向量化`);
      setPrefixModalVisible(false);
      setPrefixText("");
      setPrefixTargetChunk(null);
      setSelectedChunkIds([]);
      setSelectedTags([]);
      fetchChunks();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "加入前綴失敗");
    } finally {
      setPrefixSaving(false);
    }
  };

  const metadataEntries = document ? Object.entries(document.metadata ?? {}) : [];

  return (
    <Card
      loading={loading}
      title="文件詳情"
      extra={
        <Space>
          {isProcessing && (
            <Tag icon={<Spin size="small" style={{ marginRight: 4 }} />} color="processing">
              {ocrTask?.task_type === "reocr" ? "OCR 重跑中…" : "處理中…"}
            </Tag>
          )}
          <Button
            onClick={handleReVectorize}
            disabled={!document || reVectorizing || isProcessing}
            loading={reVectorizing}
            type="default"
          >
            重新向量化
          </Button>
          <Button
            onClick={handleReocr}
            disabled={!document || reocrLoading || isProcessing}
            loading={reocrLoading}
            type="default"
          >
            高精度重跑 OCR
          </Button>
          <Button onClick={() => onEdit?.(document)} disabled={!document}>
            編輯
          </Button>
          <Button onClick={onBack}>返回列表</Button>
        </Space>
      }
    >
      {document && (
        <>
        {isProcessing && (
          <Alert
            type="info"
            showIcon
            icon={<Spin size="small" />}
            style={{ marginBottom: 16 }}
            message="文件處理中（OCR / 向量化重建）"
            description="server 等級 OCR 在 CPU 上較慢（每頁約 1 分鐘），背景執行中。完成後此頁會自動更新，你可以先離開這一頁。處理期間「高精度重跑 OCR / 重新向量化」已暫時停用，避免重複觸發。"
          />
        )}
        <Tabs
          defaultActiveKey="info"
          onChange={(key) => { if (key === "chunks" && !chunks) fetchChunks(); }}
          items={[
            {
              key: "info",
              label: "文件資訊",
              children: (
                <>
          <Descriptions bordered column={1} size="small">
            <Descriptions.Item label="標題">{document.title}</Descriptions.Item>
            <Descriptions.Item label="PDF 文件">
              {document.pdf_path ? (
                <Button
                  type="primary"
                  icon={<FilePdfOutlined />}
                  onClick={() => setPdfPreviewVisible(true)}
                >
                  開啟 PDF 預覽
                </Button>
              ) : (
                <span style={{ color: "#999" }}>尚未上傳 PDF</span>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="分類結果">
              {document.classification ? (
                <Tag color="green">{document.classification.name}</Tag>
              ) : (
                <Tag>尚未分類</Tag>
              )}
            </Descriptions.Item>
            {document.ai_summary && (
              <Descriptions.Item label="AI 摘要">
                <Typography.Text style={{ whiteSpace: "pre-wrap" }}>{document.ai_summary}</Typography.Text>
              </Descriptions.Item>
            )}
            {metadataEntries.map(([key, value]) => (
              <Descriptions.Item
                label={{ keywords: "關鍵字", file_type: "文件類型", project_id: "專案" }[key] ?? key}
                key={key}
              >
                {Array.isArray(value) ? (
                  <Space wrap>
                    {value.map((item) => (
                      <Tag key={item}>{item}</Tag>
                    ))}
                  </Space>
                ) : (
                  value ?? "-"
                )}
              </Descriptions.Item>
            ))}
          </Descriptions>

          {/* Notes Section */}
          <div style={{ marginTop: 32 }}>
            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16 }}>
              <BookOutlined style={{ fontSize: 24, marginRight: 8, color: '#5f6368' }} />
              <span style={{ fontSize: 22, fontWeight: 400, color: '#202124' }}>我的筆記本 ({notes.length})</span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 20 }}>
              {/* Create New Note Card */}
              <div
                onClick={() => setIsCreateNoteModalVisible(true)}
                style={{
                  height: '240px',
                  backgroundColor: '#fff',
                  borderRadius: 16,
                  border: '1px solid #e0e0e0',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                  boxShadow: '0 2px 4px rgba(0,0,0,0.02)'
                }}
                className="create-note-card"
              >
                <div style={{
                  width: 48, height: 48, borderRadius: '50%',
                  background: '#E8F0FE', color: '#1967D2',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  marginBottom: 12, fontSize: 24
                }}>
                  <PlusOutlined />
                </div>
                <div style={{ fontSize: 16, fontWeight: 500, color: '#3c4043' }}>建立新的筆記</div>
              </div>

              {/* Existing Notes */}
              {notes.map((note, index) => {
                const bgColor = NOTE_COLORS[index % NOTE_COLORS.length];
                const emoji = getRandomEmoji(note.id);

                return (
                  <Card
                    key={note.id}
                    size="small"
                    bordered={false}
                    title={
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12, paddingBottom: 4 }}>
                        <div style={{ fontSize: 24 }}>{emoji}</div>
                        <div style={{
                          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                          fontSize: 16, fontWeight: 700, color: '#202124', flex: 1
                        }} title={note.question}>
                          {note.question}
                        </div>
                      </div>
                    }
                    extra={
                      <Space size={0}>
                        <Button type="text" icon={<EditOutlined style={{ color: '#5f6368' }} />} onClick={(e) => { e.stopPropagation(); openEditNoteModal(note); }} />
                        <Button type="text" icon={<DeleteOutlined style={{ color: '#5f6368' }} />} onClick={(e) => { e.stopPropagation(); handleDeleteNote(note.id); }} />
                      </Space>
                    }
                    style={{
                      height: '240px',
                      display: 'flex',
                      flexDirection: 'column',
                      backgroundColor: bgColor,
                      borderRadius: 16,
                      boxShadow: 'none',
                      transition: 'box-shadow 0.2s',
                      cursor: 'pointer'
                    }}
                    headStyle={{ borderBottom: 'none', padding: '20px 20px 0 20px', minHeight: 48 }}
                    styles={{ body: { flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column', padding: '12px 20px 20px 20px' } }}
                    hoverable
                    onClick={() => setViewingNote(note)}
                  >
                    <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
                      <div className="markdown-content" style={{ fontSize: 14, color: '#3c4043', lineHeight: 1.6 }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>
                          {note.answer}
                        </ReactMarkdown>
                      </div>
                      <div style={{
                        position: 'absolute', bottom: 0, left: 0, right: 0,
                        height: 60, background: `linear-gradient(transparent, ${bgColor})`,
                        pointerEvents: 'none'
                      }} />
                    </div>
                    <div style={{ marginTop: 12, fontSize: 12, color: 'rgba(0,0,0,0.5)', fontWeight: 500, textAlign: 'right' }}>
                      {new Date(note.created_at).toLocaleDateString()}
                    </div>
                  </Card>
                );
              })}
            </div>
          </div>

          <PdfPreviewModal
            open={pdfPreviewVisible}
            documentId={documentId}
            title={document.title}
            initialPage={initialPage}
            initialHighlightKeyword={initialHighlightKeyword}
            onClose={() => {
              setPdfPreviewVisible(false);
              fetchNotes();
            }}
          />
                </>
              ),
            },
            {
              key: "chunks",
              label: (
                <span>
                  <DatabaseOutlined /> 向量塊
                  {chunks && <Badge count={chunks.total} style={{ marginLeft: 6, backgroundColor: "#1677ff" }} />}
                </span>
              ),
              children: (
                <div>
                  {/* 統計列 */}
                  {chunks && (
                    <Row gutter={16} style={{ marginBottom: 16 }}>
                      <Col span={6}><Statistic title="總塊數" value={chunks.total} /></Col>
                      <Col span={6}><Statistic title="總字數" value={chunks.total_chars} /></Col>
                      <Col span={6}><Statistic title="平均每塊字數" value={chunks.avg_chars} /></Col>
                      <Col span={6}>
                        <Space style={{ marginTop: 24 }} wrap>
                          <Button icon={<ReloadOutlined />} onClick={fetchChunks} loading={chunksLoading}>重新載入</Button>
                          <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddChunkVisible(true)}>新增手動塊</Button>
                          {selectedChunkIds.length >= 1 && (
                            <Button
                              icon={<TagOutlined />}
                              onClick={() => { setPrefixTargetChunk(null); setPrefixText(""); setPrefixModalVisible(true); }}
                              style={{ background: "#13c2c2", color: "#fff", borderColor: "#13c2c2" }}
                            >
                              批次前綴 ({selectedChunkIds.length}) 塊
                            </Button>
                          )}
                          {selectedChunkIds.length >= 2 && (
                            <Button
                              icon={<MergeCellsOutlined />}
                              onClick={handleMergeChunks}
                              loading={merging}
                              style={{ background: "#722ed1", color: "#fff", borderColor: "#722ed1" }}
                            >
                              合併 ({selectedChunkIds.length}) 塊
                            </Button>
                          )}
                        </Space>
                      </Col>
                    </Row>
                  )}

                  {/* 塊列表 */}
                  <Table
                    loading={chunksLoading}
                    dataSource={chunks?.items ?? []}
                    rowKey="id"
                    size="small"
                    pagination={{ pageSize: 20, showSizeChanger: false }}
                    rowSelection={{
                      selectedRowKeys: selectedChunkIds,
                      onChange: (keys) => setSelectedChunkIds(keys),
                    }}
                    columns={[
                      {
                        title: "#",
                        dataIndex: "chunk_index",
                        width: 60,
                        render: (v) => <Typography.Text type="secondary">#{v}</Typography.Text>,
                      },
                      {
                        title: "頁碼",
                        dataIndex: "page",
                        width: 70,
                        render: (v) =>
                          v != null && document?.pdf_path ? (
                            <Typography.Link
                              onClick={() => { setQuickPdfPage(v); setQuickScale(1.2); }}
                              title={`快速預覽第 ${v} 頁`}
                            >
                              {v}
                            </Typography.Link>
                          ) : (v ?? "-"),
                      },
                      {
                        title: "字數",
                        dataIndex: "char_count",
                        width: 70,
                        render: (v) => (
                          <span style={{ color: v < 100 ? "#ff4d4f" : v > 1600 ? "#faad14" : "#52c41a" }}>{v}</span>
                        ),
                      },
                      {
                        title: "內容預覽",
                        dataIndex: "text",
                        render: (text, record) => (
                          <div>
                            {expandedChunkId === record.id ? (
                              <div>
                                <div
                                  style={{
                                    whiteSpace: "pre-wrap",
                                    wordBreak: "break-word",
                                    fontFamily: "monospace",
                                    fontSize: 12,
                                    lineHeight: 1.6,
                                    maxHeight: 320,
                                    overflowY: "auto",
                                    background: "#fafafa",
                                    border: "1px solid #e8e8e8",
                                    borderRadius: 4,
                                    padding: "8px 10px",
                                    marginBottom: 4,
                                  }}
                                >
                                  {text}
                                </div>
                                <Typography.Link
                                  style={{ fontSize: 12 }}
                                  onClick={() => setExpandedChunkId(null)}
                                >
                                  收起
                                </Typography.Link>
                              </div>
                            ) : (
                              <Typography.Text
                                style={{ cursor: "pointer" }}
                                onClick={() => setExpandedChunkId(record.id)}
                              >
                                {`${text.slice(0, 120)}${text.length > 120 ? "…" : ""}`}
                              </Typography.Text>
                            )}
                          </div>
                        ),
                      },
                      {
                        title: "操作",
                        width: 140,
                        render: (_, record) => (
                          <Space>
                            <Tooltip title="加入前綴並重新向量化">
                              <Button
                                size="small"
                                icon={<TagOutlined />}
                                onClick={() => { setPrefixTargetChunk(record); setPrefixText(""); setPrefixModalVisible(true); }}
                              />
                            </Tooltip>
                            <Tooltip title="編輯並重新向量化">
                              <Button
                                size="small"
                                icon={<EditOutlined />}
                                onClick={() => { setEditingChunk(record); setEditChunkText(record.text); }}
                              />
                            </Tooltip>
                            <Tooltip title="分割此塊">
                              <Button
                                size="small"
                                icon={<ScissorOutlined />}
                                onClick={() => { setSplittingChunk(record); setSplitAt(Math.floor(record.text.length / 2)); }}
                              />
                            </Tooltip>
                            <Popconfirm
                              title="確定刪除此向量塊？"
                              onConfirm={() => handleDeleteChunk(record.id)}
                              okText="刪除"
                              cancelText="取消"
                              okType="danger"
                            >
                              <Tooltip title="刪除">
                                <Button size="small" danger icon={<DeleteOutlined />} />
                              </Tooltip>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                </div>
              ),
            },
          ]}
        />

        {/* 編輯 Chunk Modal */}
        <Modal
          title={`編輯向量塊 #${editingChunk?.chunk_index ?? ""}`}
          open={!!editingChunk}
          onOk={handleSaveChunk}
          onCancel={() => setEditingChunk(null)}
          confirmLoading={chunkSaving}
          okText="儲存並重新向量化"
          cancelText="取消"
          width={800}
        >
          <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
            第 {editingChunk?.page ?? "?"} 頁 ｜ {editChunkText.length} 字
          </Typography.Text>
          <Input.TextArea
            rows={14}
            value={editChunkText}
            onChange={(e) => setEditChunkText(e.target.value)}
            style={{ fontFamily: "monospace", fontSize: 13 }}
          />
        </Modal>

        {/* 分割 Chunk Modal */}
        <Modal
          title={`分割向量塊 #${splittingChunk?.chunk_index ?? ""}`}
          open={!!splittingChunk}
          onOk={handleSplitChunk}
          onCancel={() => setSplittingChunk(null)}
          confirmLoading={splitting}
          okText="分割並重新向量化"
          cancelText="取消"
          width={800}
        >
          {splittingChunk && (
            <div>
              <div style={{ marginBottom: 8 }}>
                <Typography.Text strong>分割位置：第 {splitAt} 字 / 共 {splittingChunk.text.length} 字</Typography.Text>
              </div>
              <Slider
                min={1}
                max={splittingChunk.text.length - 1}
                value={splitAt}
                onChange={setSplitAt}
                style={{ marginBottom: 16 }}
              />
              <div style={{ display: "flex", gap: 8 }}>
                <div style={{ flex: 1 }}>
                  <Typography.Text type="secondary" style={{ display: "block", marginBottom: 4 }}>前半段（{splitAt} 字）</Typography.Text>
                  <div style={{
                    background: "#f6ffed", border: "1px solid #b7eb8f", borderRadius: 4,
                    padding: "8px 12px", fontFamily: "monospace", fontSize: 12,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                    maxHeight: 200, overflowY: "auto",
                  }}>
                    {splittingChunk.text.slice(0, splitAt)}
                  </div>
                </div>
                <div style={{ flex: 1 }}>
                  <Typography.Text type="secondary" style={{ display: "block", marginBottom: 4 }}>後半段（{splittingChunk.text.length - splitAt} 字）</Typography.Text>
                  <div style={{
                    background: "#e6f7ff", border: "1px solid #91d5ff", borderRadius: 4,
                    padding: "8px 12px", fontFamily: "monospace", fontSize: 12,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                    maxHeight: 200, overflowY: "auto",
                  }}>
                    {splittingChunk.text.slice(splitAt)}
                  </div>
                </div>
              </div>
            </div>
          )}
        </Modal>

        {/* 新增 Chunk Modal */}
        <Modal
          title="新增手動向量塊"
          open={addChunkVisible}
          onOk={handleAddChunk}
          onCancel={() => { setAddChunkVisible(false); setNewChunk({ page: "", text: "" }); }}
          confirmLoading={addingChunk}
          okText="新增並向量化"
          cancelText="取消"
          width={700}
        >
          <div style={{ marginBottom: 12 }}>
            <Typography.Text strong>頁碼（選填）：</Typography.Text>
            <Input
              type="number"
              min={1}
              value={newChunk.page}
              onChange={(e) => setNewChunk({ ...newChunk, page: e.target.value })}
              placeholder="留空表示不指定頁碼"
              style={{ marginTop: 4 }}
            />
          </div>
          <div>
            <Typography.Text strong>文字內容：</Typography.Text>
            <Input.TextArea
              rows={12}
              value={newChunk.text}
              onChange={(e) => setNewChunk({ ...newChunk, text: e.target.value })}
              placeholder="輸入要加入向量索引的文字..."
              style={{ marginTop: 4, fontFamily: "monospace", fontSize: 13 }}
            />
            <Typography.Text type="secondary">{newChunk.text.length} 字</Typography.Text>
          </div>
        </Modal>

        {/* 加入前綴 Modal */}
        {(() => {
          // 計算選取塊的頁碼範圍（用於 title）
          const batchChunks = (chunks?.items ?? []).filter((c) => selectedChunkIds.includes(c.id));
          const pages = prefixTargetChunk
            ? [prefixTargetChunk.page].filter(Boolean)
            : batchChunks.map((c) => c.page).filter(Boolean);
          const pageLabel = pages.length > 0
            ? `第 ${[...new Set(pages)].sort((a, b) => a - b).join("、")} 頁`
            : "";
          const modalTitle = prefixTargetChunk
            ? `加入前綴 — 向量塊 #${prefixTargetChunk.chunk_index}${pageLabel ? `（${pageLabel}）` : ""}`
            : `批次加入前綴（${selectedChunkIds.length} 塊${pageLabel ? `，${pageLabel}` : ""}）`;

          return (
            <Modal
              title={modalTitle}
              open={prefixModalVisible}
              onOk={handleApplyPrefix}
              onCancel={() => { setPrefixModalVisible(false); setPrefixText(""); setPrefixTargetChunk(null); setSelectedTags([]); }}
              confirmLoading={prefixSaving}
              okText="套用並重新向量化"
              cancelText="取消"
              width={700}
            >
              {/* 快速標籤選擇 */}
              <div style={{ marginBottom: 12 }}>
                <Typography.Text strong>快速標籤：</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                  選擇或輸入標籤，自動組成推薦格式填入下方
                </Typography.Text>
                <Select
                  mode="tags"
                  style={{ width: "100%", marginTop: 4 }}
                  placeholder="從建議清單選擇，或直接輸入新標籤後按 Enter"
                  value={selectedTags}
                  onChange={handleTagsChange}
                  options={tagSuggestions}
                  tokenSeparators={[",", "，", "、", ";"]}
                  allowClear
                />
              </div>

              {/* 前綴文字（可手動編輯） */}
              <div style={{ marginBottom: 12 }}>
                <Typography.Text strong>前綴文字：</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                  選標籤後自動填入；也可直接手動修改
                </Typography.Text>
                <Input.TextArea
                  rows={3}
                  value={prefixText}
                  onChange={(e) => setPrefixText(e.target.value)}
                  placeholder="例如：以下內容涉及：溫度測試；測試規範。"
                  style={{ marginTop: 4, fontFamily: "monospace", fontSize: 13 }}
                />
              </div>

              {/* 套用預覽 */}
              {prefixText.trim() && (
                <div>
                  <Typography.Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                    套用後預覽（{prefixTargetChunk ? "此塊" : "以第一塊為例"}）：
                  </Typography.Text>
                  <div style={{
                    background: "#f6ffed", border: "1px solid #b7eb8f", borderRadius: 4,
                    padding: "8px 12px", fontFamily: "monospace", fontSize: 12,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                    maxHeight: 200, overflowY: "auto",
                  }}>
                    <span style={{ color: "#389e0d", fontWeight: 600 }}>{prefixText.trim()}</span>
                    {"\n"}
                    <span style={{ color: "#595959" }}>
                      {(() => {
                        const sample = prefixTargetChunk
                          ? prefixTargetChunk.text
                          : (chunks?.items ?? []).find((c) => selectedChunkIds.includes(c.id))?.text ?? "";
                        return sample.length > 300 ? sample.slice(0, 300) + "…" : sample;
                      })()}
                    </span>
                  </div>
                  {!prefixTargetChunk && selectedChunkIds.length > 1 && (
                    <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 4, display: "block" }}>
                      此前綴將套用至所有 {selectedChunkIds.length} 個選取的向量塊
                    </Typography.Text>
                  )}
                </div>
              )}
            </Modal>
          );
        })()}

          {/* 快速頁面預覽 Modal */}
          <Modal
            open={quickPdfPage !== null}
            onCancel={() => { setQuickPdfPage(null); setQuickScale(1.2); }}
            title={`快速預覽 — 第 ${quickPdfPage} 頁`}
            width="80%"
            style={{ top: 20 }}
            styles={{ body: { height: "80vh", overflow: "hidden", display: "flex", flexDirection: "column", padding: "12px 16px" } }}
            footer={null}
            destroyOnHidden
          >
            <Space style={{ marginBottom: 8 }}>
              <Button icon={<MinusOutlined />} size="small" onClick={() => setQuickScale((s) => Math.max(0.5, parseFloat((s - 0.15).toFixed(2))))} />
              <Button icon={<PlusOutlined />} size="small" onClick={() => setQuickScale((s) => Math.min(3.0, parseFloat((s + 0.15).toFixed(2))))} />
              <Typography.Text type="secondary">{Math.round(quickScale * 100)}%</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>拖曳可平移 · 滾輪可捲動</Typography.Text>
            </Space>
            <div
              ref={quickPdfContainerRef}
              style={{
                flex: 1,
                overflow: "auto",
                border: "1px solid #f0f0f0",
                borderRadius: 6,
                cursor: quickDragging ? "grabbing" : "grab",
                userSelect: "none",
              }}
              onMouseDown={(e) => {
                const el = quickPdfContainerRef.current;
                if (!el) return;
                setQuickDragging(true);
                setQuickDragStart({ x: e.clientX, y: e.clientY, scrollLeft: el.scrollLeft, scrollTop: el.scrollTop });
                e.preventDefault();
              }}
              onMouseMove={(e) => {
                if (!quickDragging) return;
                const el = quickPdfContainerRef.current;
                if (!el) return;
                el.scrollLeft = quickDragStart.scrollLeft - (e.clientX - quickDragStart.x);
                el.scrollTop = quickDragStart.scrollTop - (e.clientY - quickDragStart.y);
              }}
              onMouseUp={() => setQuickDragging(false)}
              onMouseLeave={() => setQuickDragging(false)}
            >
              {quickFileUrl && quickPdfPage !== null && (
                <Document
                  file={quickFileUrl}
                  loading={<div style={{ padding: 40, textAlign: "center" }}><Spin tip="載入 PDF..." /></div>}
                >
                  <Page
                    pageNumber={quickPdfPage}
                    scale={quickScale}
                    renderTextLayer={false}
                    renderAnnotationLayer={false}
                  />
                </Document>
              )}
            </div>
          </Modal>

          {/* 筆記閱讀 Modal */}
          <Modal
            open={!!viewingNote}
            onCancel={() => setViewingNote(null)}
            title={
              <Space>
                <span>{viewingNote && getRandomEmoji(viewingNote.id)}</span>
                <span>{viewingNote?.question}</span>
              </Space>
            }
            footer={
              <Space>
                <Button
                  icon={<EditOutlined />}
                  onClick={() => { setViewingNote(null); openEditNoteModal(viewingNote); }}
                >
                  編輯
                </Button>
                <Button onClick={() => setViewingNote(null)}>關閉</Button>
              </Space>
            }
            width={720}
            style={{ top: 60 }}
            styles={{ body: { maxHeight: "70vh", overflowY: "auto", padding: "16px 24px" } }}
          >
            {viewingNote && (
              <div className="markdown-content" style={{ fontSize: 15, lineHeight: 1.8, color: '#202124' }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>
                  {viewingNote.answer}
                </ReactMarkdown>
              </div>
            )}
          </Modal>

          {/* Edit Note Modal */}
          <Modal
            title="編輯筆記"
            open={isEditNoteModalVisible}
            onOk={handleUpdateNote}
            onCancel={() => setIsEditNoteModalVisible(false)}
            width={800}
            okText="儲存"
            cancelText="取消"
          >
            <div style={{ marginBottom: 16 }}>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>標題 (問題)：</div>
              <Input
                value={editNoteForm.question}
                onChange={(e) => setEditNoteForm({ ...editNoteForm, question: e.target.value })}
                placeholder="輸入筆記標題..."
              />
            </div>
            <div>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>內容：</div>
              <Input.TextArea
                rows={10}
                value={editNoteForm.answer}
                onChange={(e) => setEditNoteForm({ ...editNoteForm, answer: e.target.value })}
                placeholder="輸入筆記內容 (支援 Markdown)..."
              />
            </div>
          </Modal>

          {/* Create Note Modal */}
          <Modal
            title="建立新筆記"
            open={isCreateNoteModalVisible}
            onOk={handleCreateNote}
            onCancel={() => setIsCreateNoteModalVisible(false)}
            width={800}
            okText="建立"
            cancelText="取消"
          >
            <div style={{ marginBottom: 16 }}>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>標題 (問題)：</div>
              <Input
                value={createNoteForm.question}
                onChange={(e) => setCreateNoteForm({ ...createNoteForm, question: e.target.value })}
                placeholder="輸入筆記標題..."
              />
            </div>
            <div>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>內容：</div>
              <Input.TextArea
                rows={10}
                value={createNoteForm.answer}
                onChange={(e) => setCreateNoteForm({ ...createNoteForm, answer: e.target.value })}
                placeholder="輸入筆記內容 (支援 Markdown)..."
              />
            </div>
          </Modal>
        </>
      )}
    </Card>
  );
};

export default DocumentDetail;
