import { useEffect, useState } from "react";
import { Button, Card, Empty, Input, Modal, Space, Spin, Tag, message } from "antd";
import { BookOutlined, DeleteOutlined, EditOutlined, ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import AppLayout from "../components/Layout/AppLayout";
import apiClient from "../services/api";

const NOTE_COLORS = [
  "#FFF7E0", "#E3F2FD", "#F3E5F5", "#E0F2F1",
  "#FBE9E7", "#FCE4EC", "#E8EAF6", "#F1F8E9",
];

const getRandomEmoji = (id) => {
  const emojis = ["📓", "💡", "🧠", "⚙️", "💻", "📚", "📝", "🎨", "🚀", "🧩"];
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = id.charCodeAt(i) + ((hash << 5) - hash);
  return emojis[Math.abs(hash) % emojis.length];
};

const NotebookPage = () => {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState("");

  // 閱讀 modal
  const [viewingNote, setViewingNote] = useState(null);

  // 編輯 modal
  const [editingNote, setEditingNote] = useState(null);
  const [editForm, setEditForm] = useState({ question: "", answer: "" });
  const [editSaving, setEditSaving] = useState(false);

  const navigate = useNavigate();

  const fetchNotes = async () => {
    try {
      setLoading(true);
      const resp = await apiClient.get("documents/notes");
      setNotes(resp.data ?? []);
    } catch {
      message.error("載入筆記失敗");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchNotes(); }, []);

  const handleDelete = (note) => {
    Modal.confirm({
      title: "刪除筆記",
      content: "確定要刪除這則筆記嗎？",
      okText: "刪除",
      okType: "danger",
      cancelText: "取消",
      onOk: async () => {
        try {
          await apiClient.delete(`documents/${note.document_id}/notes/${note.id}`);
          message.success("筆記已刪除");
          setNotes((prev) => prev.filter((n) => n.id !== note.id));
          if (viewingNote?.id === note.id) setViewingNote(null);
        } catch {
          message.error("刪除失敗");
        }
      },
    });
  };

  const openEdit = (note) => {
    setEditingNote(note);
    setEditForm({ question: note.question, answer: note.answer });
  };

  const handleSaveEdit = async () => {
    if (!editForm.question.trim() || !editForm.answer.trim()) {
      message.warning("請填寫標題與內容");
      return;
    }
    try {
      setEditSaving(true);
      const resp = await apiClient.put(
        `documents/${editingNote.document_id}/notes/${editingNote.id}`,
        editForm
      );
      message.success("筆記已更新");
      setNotes((prev) => prev.map((n) => n.id === editingNote.id ? { ...n, ...resp.data, document_title: n.document_title } : n));
      if (viewingNote?.id === editingNote.id) setViewingNote((v) => ({ ...v, ...editForm }));
      setEditingNote(null);
    } catch {
      message.error("更新失敗");
    } finally {
      setEditSaving(false);
    }
  };

  // Markdown link renderer — internal /documents/ links use React Router
  const markdownComponents = {
    a: ({ href, children }) => {
      if (href?.startsWith("/documents/")) {
        return (
          <a href={href} onClick={(e) => { e.preventDefault(); navigate(href); }} style={{ color: "#1677ff" }}>
            {children}
          </a>
        );
      }
      return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
    },
  };

  const filtered = notes.filter((n) => {
    if (!searchText.trim()) return true;
    const q = searchText.toLowerCase();
    return (
      n.question.toLowerCase().includes(q) ||
      n.answer.toLowerCase().includes(q) ||
      n.document_title.toLowerCase().includes(q)
    );
  });

  return (
    <AppLayout>
      <div style={{ maxWidth: 1200, margin: "0 auto" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <BookOutlined style={{ fontSize: 28, color: "#5f6368" }} />
            <span style={{ fontSize: 26, fontWeight: 400, color: "#202124" }}>
              我的筆記本 ({filtered.length})
            </span>
          </div>
          <Space>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜尋標題、內容或文件名稱..."
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              allowClear
              style={{ width: 280 }}
            />
            <Button icon={<ReloadOutlined />} onClick={fetchNotes} loading={loading}>重新載入</Button>
          </Space>
        </div>

        {loading ? (
          <div style={{ textAlign: "center", padding: 80 }}><Spin size="large" /></div>
        ) : filtered.length === 0 ? (
          <Empty description={searchText ? "找不到符合的筆記" : "尚無任何筆記"} style={{ padding: 80 }} />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 20 }}>
            {filtered.map((note, index) => {
              const bgColor = NOTE_COLORS[index % NOTE_COLORS.length];
              const emoji = getRandomEmoji(note.id);
              return (
                <Card
                  key={note.id}
                  size="small"
                  bordered={false}
                  title={
                    <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 4 }}>
                      <span style={{ fontSize: 22 }}>{emoji}</span>
                      <div style={{ flex: 1, overflow: "hidden" }}>
                        <div style={{
                          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                          fontSize: 15, fontWeight: 700, color: "#202124",
                        }} title={note.question}>
                          {note.question}
                        </div>
                        <Tag
                          color="blue"
                          style={{ fontSize: 11, cursor: "pointer", marginTop: 2 }}
                          onClick={(e) => { e.stopPropagation(); navigate(`/documents/${note.document_id}`); }}
                        >
                          {note.document_title}
                        </Tag>
                      </div>
                    </div>
                  }
                  extra={
                    <Space size={0}>
                      <Button type="text" size="small" icon={<EditOutlined style={{ color: "#5f6368" }} />}
                        onClick={(e) => { e.stopPropagation(); openEdit(note); }} />
                      <Button type="text" size="small" icon={<DeleteOutlined style={{ color: "#5f6368" }} />}
                        onClick={(e) => { e.stopPropagation(); handleDelete(note); }} />
                    </Space>
                  }
                  style={{
                    height: 240,
                    display: "flex",
                    flexDirection: "column",
                    backgroundColor: bgColor,
                    borderRadius: 16,
                    boxShadow: "none",
                    transition: "box-shadow 0.2s",
                    cursor: "pointer",
                  }}
                  headStyle={{ borderBottom: "none", padding: "20px 20px 0 20px", minHeight: 48 }}
                  styles={{ body: { flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", padding: "12px 20px 20px 20px" } }}
                  hoverable
                  onClick={() => setViewingNote(note)}
                >
                  <div style={{ flex: 1, overflow: "hidden", position: "relative" }}>
                    <div className="markdown-content" style={{ fontSize: 14, color: "#3c4043", lineHeight: 1.6 }}>
                      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={markdownComponents}>
                        {note.answer}
                      </ReactMarkdown>
                    </div>
                    <div style={{
                      position: "absolute", bottom: 0, left: 0, right: 0,
                      height: 60, background: `linear-gradient(transparent, ${bgColor})`,
                      pointerEvents: "none",
                    }} />
                  </div>
                  <div style={{ marginTop: 12, fontSize: 12, color: "rgba(0,0,0,0.5)", fontWeight: 500, textAlign: "right" }}>
                    {new Date(note.created_at).toLocaleDateString()}
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </div>

      {/* 閱讀 Modal */}
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
            <Button icon={<EditOutlined />} onClick={() => { setViewingNote(null); openEdit(viewingNote); }}>編輯</Button>
            <Button danger icon={<DeleteOutlined />} onClick={() => handleDelete(viewingNote)}>刪除</Button>
            <Button onClick={() => setViewingNote(null)}>關閉</Button>
          </Space>
        }
        width={720}
        style={{ top: 60 }}
        styles={{ body: { maxHeight: "70vh", overflowY: "auto", padding: "16px 24px" } }}
      >
        {viewingNote && (
          <>
            <Tag
              color="blue"
              style={{ cursor: "pointer", marginBottom: 16 }}
              onClick={() => { setViewingNote(null); navigate(`/documents/${viewingNote.document_id}`); }}
            >
              {viewingNote.document_title}
            </Tag>
            <div className="markdown-content" style={{ fontSize: 15, lineHeight: 1.8, color: "#202124" }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={markdownComponents}>
                {viewingNote.answer}
              </ReactMarkdown>
            </div>
          </>
        )}
      </Modal>

      {/* 編輯 Modal */}
      <Modal
        title="編輯筆記"
        open={!!editingNote}
        onOk={handleSaveEdit}
        onCancel={() => setEditingNote(null)}
        confirmLoading={editSaving}
        okText="儲存"
        cancelText="取消"
        width={720}
      >
        <div style={{ marginBottom: 16 }}>
          <div style={{ marginBottom: 8, fontWeight: 500 }}>標題（問題）：</div>
          <Input
            value={editForm.question}
            onChange={(e) => setEditForm({ ...editForm, question: e.target.value })}
            placeholder="輸入筆記標題..."
          />
        </div>
        <div>
          <div style={{ marginBottom: 8, fontWeight: 500 }}>內容：</div>
          <Input.TextArea
            rows={12}
            value={editForm.answer}
            onChange={(e) => setEditForm({ ...editForm, answer: e.target.value })}
            placeholder="輸入筆記內容（支援 Markdown）..."
          />
        </div>
      </Modal>
    </AppLayout>
  );
};

export default NotebookPage;
