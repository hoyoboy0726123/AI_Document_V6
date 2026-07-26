import { useEffect, useMemo, useState, useRef } from "react";
import { Modal, Space, Button, Typography, Spin, Input, Card, Divider, message, Tag } from "antd";
import { PlusOutlined, MinusOutlined, LeftOutlined, RightOutlined, RobotOutlined, SendOutlined, EyeOutlined, DeleteOutlined, CopyOutlined, SaveOutlined } from "@ant-design/icons";
import { Document, Page, pdfjs } from "react-pdf";
import rehypeRaw from "rehype-raw";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import useAuthStore from "../../stores/authStore";
import apiClient from "../../services/api";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.mjs",
  import.meta.url,
).toString();

const DEFAULT_MAX_ANALYSIS_PAGES = 10;

const PdfPreviewModal = ({
  open,
  documentId,
  title,
  initialPage = 1,
  initialHighlightKeyword = "",
  onClose,
}) => {
  const { token } = useAuthStore();
  const [numPages, setNumPages] = useState(null);
  const [pageNumber, setPageNumber] = useState(initialPage || 1);
  const [scale, setScale] = useState(1.15);

  const [showAnalysis, setShowAnalysis] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [conversationHistory, setConversationHistory] = useState([]);
  const [analysisQuestion, setAnalysisQuestion] = useState("");
  const [followupQuestion, setFollowupQuestion] = useState("");
  const [selectedPages, setSelectedPages] = useState([]);
  const [analyzedPages, setAnalyzedPages] = useState([]);

  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState([]);
  const [showSearchResults, setShowSearchResults] = useState(false);
  const [highlightKeyword, setHighlightKeyword] = useState("");
  const [savingNote, setSavingNote] = useState(false);
  const [maxAnalysisPages, setMaxAnalysisPages] = useState(DEFAULT_MAX_ANALYSIS_PAGES);

  const pdfContainerRef = useRef(null);
  const pendingHighlightKeyword = useRef("");
  const streamControllerRef = useRef(null);
  const analysisScrollRef = useRef(null); // AI 分析對話捲動容器

  // 分析面板開啟或有新內容時，自動捲到最下方（顯示最新一次查詢/正在生成的結果），
  // 而不是停在最上方的舊紀錄。
  useEffect(() => {
    if (!showAnalysis) return undefined;
    const el = analysisScrollRef.current;
    if (!el) return undefined;
    const t = setTimeout(() => { el.scrollTop = el.scrollHeight; }, 80);
    return () => clearTimeout(t);
  }, [conversationHistory, showAnalysis]);

  useEffect(() => {
    apiClient.get("/rag/config")
      .then((res) => setMaxAnalysisPages(res.data?.max_pdf_analysis_pages ?? DEFAULT_MAX_ANALYSIS_PAGES))
      .catch(() => {});
  }, []);

  useEffect(() => {
    // Audit H12：open 切換或換文件時，先中止上一份文件仍在跑的分析串流，
    // 否則舊 stream 的 updateAnswer 會以舊 msgIndex 繼續寫入 → 舊文件的分析文字
    // 覆寫/插進新文件的對話，甚至被存成新文件的筆記。
    try { streamControllerRef.current?.abort(); } catch { /* ignore */ }
    streamControllerRef.current = null;
    setAnalyzing(false);

    if (open) {
      setPageNumber(initialPage || 1);
      setShowAnalysis(false);
      // setConversationHistory([]); // Fetch from backend instead
      setAnalysisQuestion("");
      setFollowupQuestion("");
      setSelectedPages([]);
      setAnalyzedPages([]);
      setSearchQuery("");
      setSearchResults([]);
      setShowSearchResults(false);
      pendingHighlightKeyword.current = initialHighlightKeyword || "";
      setHighlightKeyword("");

      if (documentId) {
        fetchHistory();
      } else {
        setConversationHistory([]);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialPage, initialHighlightKeyword, documentId]);

  const fetchHistory = async () => {
    try {
      const res = await apiClient.get(`/documents/${documentId}/analysis`);
      const history = res.data?.messages ?? [];
      setConversationHistory(history);
      if (history.length > 0) setShowAnalysis(true);
    } catch (err) {
      console.error("Failed to fetch history", err);
      setConversationHistory([]);
    }
  };

  const handleClearHistory = async () => {
    if (!documentId) return;
    try {
      await apiClient.delete(`/documents/${documentId}/analysis`);
      setConversationHistory([]);
      message.success("對話紀錄已清除");
    } catch (err) {
      message.error("清除失敗");
    }
  };

  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
        return;
      }

      if (e.key === "ArrowLeft") {
        e.preventDefault();
        if (pageNumber > 1) {
          changePage(-1);
        }
      }

      if (e.key === "ArrowRight") {
        e.preventDefault();
        if (numPages && pageNumber < numPages) {
          changePage(1);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pageNumber, numPages]);

  useEffect(() => {
    if (!highlightKeyword || !pdfContainerRef.current || !open) return;

    const timer = setTimeout(() => {
      const textLayer = pdfContainerRef.current?.querySelector(".react-pdf__Page__textContent");
      if (!textLayer) {
        return;
      }

      textLayer.querySelectorAll("mark").forEach((mark) => {
        const parent = mark.parentNode;
        parent.replaceChild(document.createTextNode(mark.textContent), mark);
      });

      // Audit L 修正：
      // 1) global regex 重複 test() 會殘留 lastIndex，讓後續節點從中間比對而漏高亮
      //    → 每次 test 前重設 lastIndex。
      // 2) 不再用 innerHTML 拼字串（PDF 文字含 < / & 會被當 HTML 解析，且有注入面）
      //    → 改純 DOM：split + createElement("mark")。
      const escaped = highlightKeyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const regex = new RegExp(escaped, "gi");

      const walk = (node) => {
        if (node.nodeType === Node.TEXT_NODE) {
          const text = node.textContent;
          regex.lastIndex = 0;
          if (!regex.test(text)) return;
          regex.lastIndex = 0;

          const frag = document.createDocumentFragment();
          let lastEnd = 0;
          let m;
          while ((m = regex.exec(text)) !== null) {
            if (m.index > lastEnd) {
              frag.appendChild(document.createTextNode(text.slice(lastEnd, m.index)));
            }
            const mark = document.createElement("mark");
            mark.style.backgroundColor = "#ffff00";
            mark.style.padding = "2px 0";
            mark.textContent = m[0];
            frag.appendChild(mark);
            lastEnd = m.index + m[0].length;
            if (m[0].length === 0) regex.lastIndex += 1; // 防零長匹配死迴圈
          }
          if (lastEnd < text.length) {
            frag.appendChild(document.createTextNode(text.slice(lastEnd)));
          }
          node.parentNode.replaceChild(frag, node);
        } else {
          // 不進入已生成的 <mark>，避免重複包裹（live NodeList 會走訪到新插入的節點）
          if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "MARK") return;
          node.childNodes.forEach(walk);
        }
      };

      textLayer.childNodes.forEach(walk);
    }, 400);

    return () => clearTimeout(timer);
  }, [highlightKeyword, pageNumber, open]);

  const fileUrl = useMemo(() => {
    if (!documentId) return null;
    return {
      url: `/api/v1/documents/${documentId}/pdf`,
      httpHeaders: token ? { Authorization: `Bearer ${token}` } : {},
      withCredentials: false,
    };
  }, [documentId, token]);

  const handleLoadSuccess = ({ numPages: total }) => {
    setNumPages(total);
    // Audit M：來源頁碼可能超過實際頁數（OCR 頁偏移 / 換版）→ 夾在 [1,total]，避免開壞頁白屏。
    setPageNumber((prev) => Math.min(Math.max(1, prev || 1), total || 1));
    if (pendingHighlightKeyword.current) {
      setTimeout(() => {
        setHighlightKeyword(pendingHighlightKeyword.current);
        pendingHighlightKeyword.current = "";
      }, 300);
    }
  };

  const changePage = (offset) => {
    setPageNumber((prev) => {
      const next = prev + offset;
      if (next < 1) return 1;
      if (numPages && next > numPages) return numPages;
      return next;
    });
  };

  const handleZoom = (delta) => {
    setScale((prev) => {
      const next = prev + delta;
      if (next < 0.5) return 0.5;
      if (next > 2.5) return 2.5;
      return next;
    });
  };

  const buildHistoryPayload = () =>
    conversationHistory.map((entry) => ({
      question: entry.question,
      answer: entry.answer,
    }));

  const startStreamAnalysis = async (pages, questionText, history) => {
    if (!documentId) return;

    const label =
      (questionText && questionText.trim()) ||
      (pages.length === 1
        ? `請分析第 ${pages[0]} 頁的重點內容`
        : `請分析第 ${pages.join(", ")} 頁的整體內容`);
    const newMsg = { question: label, answer: "" };
    setConversationHistory((prev) => [...prev, newMsg]);
    const msgIndex = conversationHistory.length;

    setAnalyzing(true);
    setShowAnalysis(true);

    const controller = new AbortController();
    streamControllerRef.current = controller;

    try {
      const res = await fetch(`/api/v1/rag/analyze-pdf-pages/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          document_id: documentId,
          page_numbers: pages,
          question: (questionText && questionText.trim()) || null,
          conversation_history: history || [],
        }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) throw new Error(`串流連線失敗 (${res.status})`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let thinkingText = "";
      let contentText = "";

      const updateAnswer = (newText, isThinkingPhase) => {
        if (isThinkingPhase) {
          thinkingText += newText;
        } else {
          contentText += newText;
        }

        let finalHtml = "";

        if (thinkingText) {
          finalHtml += `<details class="thinking-process" style="margin-bottom: 1em; border: 1px solid #d9d9d9; border-radius: 4px; padding: 8px; background: #f5f5f5;"><summary style="cursor: pointer; color: #888; font-size: 12px;">思考過程 (點擊展開)</summary><div style="margin-top: 8px; font-size: 13px; color: #666; white-space: pre-wrap;">${thinkingText}</div></details>`;
        }

        if (contentText) {
          finalHtml += contentText;
        } else if (!thinkingText) {
          finalHtml = "";
        }

        setConversationHistory((prev) => {
          const next = [...prev];
          if (!next[msgIndex]) return prev;
          next[msgIndex] = { ...next[msgIndex], answer: finalHtml };
          return next;
        });
      };

      let hadError = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const part of parts) {
          let eventType = "content";
          let dataObj = null;
          for (const line of part.split("\n")) {
            if (line.startsWith("event:")) eventType = line.replace("event:", "").trim();
            if (line.startsWith("data:")) {
              const raw = line.replace("data:", "").trim();
              try { dataObj = JSON.parse(raw); } catch { dataObj = { text: raw }; }
            }
          }

          // Audit H8：後端錯誤走 event:error + {message}，舊版因 text 為空被 continue 掉，
          // 迴圈結束又無條件顯示「分析完成」→ 使用者看到空答案配成功提示。
          if (eventType === "error") {
            hadError = true;
            message.error(dataObj?.message || "分析失敗");
            break;
          }

          const text = (dataObj && dataObj.text) || "";
          if (!text) continue;

          if (eventType === "thinking") {
            updateAnswer(text, true);
          } else {
            updateAnswer(text, false);
          }
        }
        if (hadError) break;
      }

      if (!hadError) message.success("分析完成");
    } catch (err) {
      const msg = err?.message || "串流分析失敗";
      message.error(msg);
    } finally {
      setAnalyzing(false);
      streamControllerRef.current = null;
    }
  };

  const handleAddCurrentPageToSelection = () => {
    if (!documentId) return;
    if (selectedPages.includes(pageNumber)) {
      message.info(`第 ${pageNumber} 頁已在多頁分析清單中`);
      return;
    }
    if (selectedPages.length >= maxAnalysisPages) {
      message.warning(`最多僅能加入 ${maxAnalysisPages} 頁`);
      return;
    }
    setSelectedPages((prev) => {
      const next = [...prev, pageNumber].sort((a, b) => a - b);
      return next;
    });
    message.success(`已加入第 ${pageNumber} 頁`);
  };

  const handleRemoveSelectedPage = (page) => {
    setSelectedPages((prev) => prev.filter((item) => item !== page));
  };

  const handleClearSelectedPages = () => {
    setSelectedPages([]);
  };

  const handleAnalyzeCurrentPage = async () => {
    if (!documentId) return;
    // Streaming path
    try {
      const targetPages = [pageNumber];
      const customQuestion = analysisQuestion.trim();
      setAnalyzedPages(targetPages);
      setAnalysisQuestion("");
      await startStreamAnalysis(targetPages, customQuestion, []);
      return;
    } catch (e) {
      // fall through to legacy path on error
    }

    const customQuestion = analysisQuestion.trim();
    setAnalyzing(true);
    setShowAnalysis(true);

    try {
      const targetPages = [pageNumber];
      const response = await apiClient.post("/rag/analyze-pdf-pages", {
        document_id: documentId,
        page_numbers: targetPages,
        question: customQuestion || null,
        conversation_history: [],
      });

      const newMessage = {
        question: customQuestion || `請分析第 ${pageNumber} 頁的內容`,
        answer: response.data.answer,
      };

      setConversationHistory([newMessage]);
      setAnalyzedPages(targetPages);
      setAnalysisQuestion("");
      message.success("分析完成！");
    } catch (error) {
      reportError(error, "AI 分析失敗");
    } finally {
      setAnalyzing(false);
    }
  };

  const handleAnalyzeSelectedPages = async () => {
    if (!documentId) return;
    if (selectedPages.length === 0) {
      message.warning("請先加入欲分析的頁面（最多 10 頁）");
      return;
    }

    const uniquePages = Array.from(new Set(selectedPages)).slice(0, maxAnalysisPages);
    const customQuestion = analysisQuestion.trim();
    try {
      setAnalyzedPages(uniquePages);
      setSelectedPages(uniquePages);
      setAnalysisQuestion("");
      await startStreamAnalysis(uniquePages, customQuestion, []);
      return;
    } catch (e) {
      // fallback to legacy path
    }

    try {
      const response = await apiClient.post("/rag/analyze-pdf-pages", {
        document_id: documentId,
        page_numbers: uniquePages,
        question: customQuestion || null,
        conversation_history: [],
      });

      const label = uniquePages.length === 1 ? `請分析第 ${uniquePages[0]} 頁的內容` : `請分析第 ${uniquePages.join(", ")} 頁的整體內容`;
      const newMessage = {
        question: customQuestion || label,
        answer: response.data.answer,
      };

      setConversationHistory([newMessage]);
      setAnalyzedPages(uniquePages);
      setSelectedPages(uniquePages);
      setAnalysisQuestion("");
      message.success("多頁分析完成！");
    } catch (error) {
      reportError(error, "AI 多頁分析失敗");
    } finally {
      setAnalyzing(false);
    }
  };



  const handleTextSearch = async () => {
    const query = searchQuery.trim();
    if (!query || !documentId) {
      message.warning("請輸入要搜尋的關鍵字");
      return;
    }

    setSearching(true);
    setShowSearchResults(true);

    try {
      const response = await apiClient.get(`/documents/${documentId}/search-text`, {
        params: { q: query },
      });

      setSearchResults(response.data.matches || []);

      if (response.data.total_matches === 0) {
        message.info("沒有找到相符的文字");
      } else {
        message.success(`找到 ${response.data.total_matches} 筆結果`);
      }
    } catch (error) {
      reportError(error, "搜尋失敗");
    } finally {
      setSearching(false);
    }
  };

  const handleSearchResultClick = (page) => {
    setPageNumber(page);
    setShowSearchResults(false);
    setHighlightKeyword(searchQuery.trim());
  };

  const handleCopy = (text) => {
    // Remove HTML tags for copying
    const tempDiv = document.createElement("div");
    tempDiv.innerHTML = text;
    const plainText = tempDiv.textContent || tempDiv.innerText || "";
    navigator.clipboard.writeText(plainText).then(() => {
      message.success("已複製到剪貼簿");
    });
  };

  const handleSaveNote = async (question, answer) => {
    if (!documentId) return;
    try {
      setSavingNote(true);
      const pageRef = analyzedPages.length > 0
        ? analyzedPages.map((p) => `第 ${p} 頁`).join("、")
        : pageNumber ? `第 ${pageNumber} 頁` : null;
      const sourceSection =
        `\n\n---\n**📎 來源文件**\n` +
        `[${title || "文件"}${pageRef ? ` — ${pageRef}` : ""}](/documents/${documentId}` +
        `${analyzedPages.length === 1 ? `?page=${analyzedPages[0]}` : pageNumber ? `?page=${pageNumber}` : ""})`;
      await apiClient.post(`/documents/${documentId}/notes`, {
        question: question,
        answer: answer + sourceSection,
      });
      message.success("已儲存至記事");
    } catch (error) {
      reportError(error, "儲存筆記失敗");
    } finally {
      setSavingNote(false);
    }
  };

  const reportError = (error, fallback) => {
    let errorMsg = fallback;
    if (error.response) {
      if (error.response.status === 422) {
        errorMsg = "請求格式錯誤，請聯繫系統管理員";
      } else if (error.response.data?.detail) {
        errorMsg = error.response.data.detail;
      } else if (typeof error.response.data === "string") {
        errorMsg = error.response.data;
      }
    } else if (error.request) {
      errorMsg = "無法連接至伺服器，請檢查網絡";
    }

    if (
      error.response?.status === 503 ||
      errorMsg.includes("overloaded") ||
      errorMsg.includes("暫時") ||
      errorMsg.includes("網絡")
    ) {
      message.warning(errorMsg, 10);
    } else {
      message.error(errorMsg, 8);
    }
  };

  const fileLoading = (
    <div style={{ padding: 24 }}>
      <Spin tip="載入 PDF..."><div style={{ minHeight: 80 }} /></Spin>
    </div>
  );

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={title ? `PDF 預覽 - ${title}` : "PDF 預覽"}
      width="95%"
      style={{ top: 20 }}
      styles={{ body: { height: "85vh", overflow: "hidden" } }}
      footer={null}
      destroyOnHidden
    >
      {fileUrl ? (
        <div style={{ height: "100%", display: "flex", gap: 16 }}>
          <div style={{ flex: showAnalysis ? "0 0 55%" : "1", minWidth: 0, display: "flex", flexDirection: "column" }}>
            <Space style={{ marginBottom: 12 }} wrap>
              <Button icon={<MinusOutlined />} onClick={() => handleZoom(-0.15)} />
              <Button icon={<PlusOutlined />} onClick={() => handleZoom(0.15)} />
              <Button icon={<LeftOutlined />} onClick={() => changePage(-1)} disabled={pageNumber <= 1}>
                上一頁
              </Button>
              <Button icon={<RightOutlined />} onClick={() => changePage(1)} disabled={numPages ? pageNumber >= numPages : false}>
                下一頁
              </Button>
              <Typography.Text>
                第 {pageNumber} 頁{numPages ? ` / 共 ${numPages} 頁` : ""}
              </Typography.Text>
              <Divider type="vertical" />
              <Input
                placeholder="想讓 AI 聚焦的問題（可留空）"
                value={analysisQuestion}
                onChange={(e) => setAnalysisQuestion(e.target.value)}
                disabled={analyzing}
                style={{ width: 280 }}
              />
              <Button type="primary" icon={<RobotOutlined />} loading={analyzing} onClick={handleAnalyzeCurrentPage}>
                分析本頁
              </Button>
              <Button
                onClick={handleAddCurrentPageToSelection}
                disabled={selectedPages.includes(pageNumber) || selectedPages.length >= maxAnalysisPages}
              >
                加入多頁清單
              </Button>
              <Button
                type="dashed"
                icon={<RobotOutlined />}
                loading={analyzing}
                onClick={handleAnalyzeSelectedPages}
                disabled={selectedPages.length === 0}
              >
                多頁分析
              </Button>
              {showAnalysis && (
                <Button icon={<EyeOutlined />} onClick={() => setShowAnalysis(false)}>
                  隱藏分析面板
                </Button>
              )}
              <Divider type="vertical" />
              <Input.Search
                placeholder="全文搜尋..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onSearch={handleTextSearch}
                loading={searching}
                style={{ width: 260 }}
              />
            </Space>

            {selectedPages.length > 0 && (
              <Space size={[4, 4]} wrap style={{ marginBottom: 12 }}>
                <Typography.Text type="secondary">
                  多頁分析列表（{selectedPages.length}/{maxAnalysisPages}）
                </Typography.Text>
                {selectedPages.map((page) => (
                  <Tag
                    color="geekblue"
                    key={`selected-${page}`}
                    closable
                    onClose={(e) => {
                      e.preventDefault();
                      handleRemoveSelectedPage(page);
                    }}
                  >
                    第 {page} 頁
                  </Tag>
                ))}
                <Button size="small" onClick={handleClearSelectedPages}>
                  清空
                </Button>
              </Space>
            )}

            {showSearchResults && (
              <Card size="small" title="搜尋結果" style={{ marginBottom: 12 }}>
                {searchResults.length === 0 ? (
                  <Typography.Text type="secondary">沒有結果</Typography.Text>
                ) : (
                  searchResults.map((result, idx) => (
                    <Card.Grid
                      key={`${result.page}-${idx}`}
                      style={{ width: "50%", cursor: "pointer", padding: 12 }}
                      onClick={() => handleSearchResultClick(result.page)}
                    >
                      <Tag color="blue">第 {result.page} 頁</Tag>
                      <Typography.Text style={{ fontSize: 13 }}>
                        {result.snippet || result.text}
                      </Typography.Text>
                    </Card.Grid>
                  ))
                )}
              </Card>
            )}

            <div
              ref={pdfContainerRef}
              style={{
                flex: 1,
                overflow: "auto",
                border: "1px solid #f0f0f0",
                borderRadius: 6,
                position: "relative",
              }}
            >
              <Document file={fileUrl} onLoadSuccess={handleLoadSuccess} loading={fileLoading}>
                <Page
                  pageNumber={pageNumber}
                  scale={scale}
                  loading={fileLoading}
                  renderTextLayer
                  renderAnnotationLayer={false}
                />
              </Document>
            </div>
          </div>

          {showAnalysis && (
            <div style={{ flex: "0 0 43%", minWidth: 0, display: "flex", flexDirection: "column", borderLeft: "2px solid #f0f0f0", paddingLeft: 16 }}>
              <div style={{ marginBottom: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <Typography.Title level={5} style={{ margin: 0 }}>
                    <RobotOutlined /> AI 分析結果
                  </Typography.Title>
                  {conversationHistory.length > 0 && (
                    <Button
                      type="text"
                      danger
                      size="small"
                      icon={<DeleteOutlined />}
                      onClick={handleClearHistory}
                    >
                      清除紀錄
                    </Button>
                  )}
                </div>
                {analyzedPages.length > 0 && (
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      已分析頁面
                    </Typography.Text>
                    <Space size={[4, 4]} wrap style={{ marginTop: 4 }}>
                      {analyzedPages.map((page) => (
                        <Tag color="blue" key={`analyzed-${page}`}>
                          第 {page} 頁
                        </Tag>
                      ))}
                    </Space>
                  </div>
                )}
              </div>

              <div ref={analysisScrollRef} style={{ flex: 1, overflow: "auto", marginBottom: 12 }}>
                {conversationHistory.length === 0 ? (
                  <Card size="small">
                    <Typography.Text type="secondary">
                      請輸入問題並點擊「分析本頁」，即可獲得針對目前頁面的 AI 說明。
                    </Typography.Text>
                  </Card>
                ) : (
                  conversationHistory.map((msg, idx) => (
                    <div key={idx} style={{ marginBottom: 16 }}>
                      <Card
                        size="small"
                        style={{ background: "#e6f7ff", borderLeft: "4px solid #1890ff", marginBottom: 8 }}
                      >
                        <Typography.Text strong>{msg.question}</Typography.Text>
                      </Card>
                      <Card size="small" style={{ background: "#f9f9f9", borderLeft: "4px solid #52c41a" }}>
                        <div style={{ fontSize: 14, lineHeight: 1.6 }} className="markdown-content">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>{msg.answer}</ReactMarkdown>
                        </div>
                        <div style={{ marginTop: 8, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                          <Button
                            size="small"
                            icon={<CopyOutlined />}
                            onClick={() => handleCopy(msg.answer)}
                          >
                            複製文字
                          </Button>
                          <Button
                            size="small"
                            icon={<SaveOutlined />}
                            onClick={() => handleSaveNote(msg.question, msg.answer)}
                            loading={savingNote}
                          >
                            儲存至記事
                          </Button>
                        </div>
                      </Card>
                    </div>
                  ))
                )}
              </div>

              <Card size="small" title="追問">
                <Space.Compact style={{ width: "100%" }}>
                  <Input.TextArea
                    placeholder="輸入追問內容..."
                    value={followupQuestion}
                    onChange={(e) => setFollowupQuestion(e.target.value)}
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    disabled={analyzing || analyzedPages.length === 0}
                  />
                  <Button
                    type="primary"
                    icon={<SendOutlined />}
                    loading={analyzing}
                    disabled={analyzedPages.length === 0 || !followupQuestion.trim()}
                    onClick={async () => {
                      const pagesParam = (analyzedPages && analyzedPages.length) ? analyzedPages : [pageNumber];
                      await startStreamAnalysis(pagesParam, followupQuestion.trim(), buildHistoryPayload());
                      setFollowupQuestion("");
                    }}
                    style={{ height: "auto" }}
                  >
                    追問
                  </Button>
                </Space.Compact>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  提示：Enter 送出，Shift + Enter 換行
                </Typography.Text>
              </Card>
            </div>
          )}
        </div>
      ) : (
        <div style={{ textAlign: "center", padding: 40 }}>
          <Spin tip="載入 PDF..."><div style={{ minHeight: 80 }} /></Spin>
        </div>
      )}
    </Modal>
  );
};

export default PdfPreviewModal;
