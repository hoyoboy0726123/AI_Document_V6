import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Checkbox, Form, Input, Modal, Progress, Select, Space, Spin, Upload, Typography, message, Divider } from "antd";
import { InboxOutlined, PlusOutlined } from "@ant-design/icons";
import { useLocation } from "react-router-dom";
import apiClient from "../../services/api";
import AISuggestionModal from "./AISuggestionModal";
import { useTaskStatus } from "../../contexts/TaskStatusContext";

const { Dragger } = Upload;

const DocumentForm = ({ document, onSuccess, onCancel, loading = false }) => {
  const { tasks, addTask, taskStatuses } = useTaskStatus();
  const location = useLocation();
  const [form] = Form.useForm();
  const [metadataFields, setMetadataFields] = useState([]);
  const [classificationOptions, setClassificationOptions] = useState([]);
  const [projectOptions, setProjectOptions] = useState([]);
  const [keywordOptions, setKeywordOptions] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [processingStatus, setProcessingStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [suggestionState, setSuggestionState] = useState(null);
  const [suggestionVisible, setSuggestionVisible] = useState(false);

  // 新增選項的 Modal 狀態
  const [addOptionModalVisible, setAddOptionModalVisible] = useState(false);
  const [addOptionField, setAddOptionField] = useState(null);
  const [addOptionForm] = Form.useForm();
  const [addingOption, setAddingOption] = useState(false);
  // 分類的即時新增（分類不是 metadata 欄位，走 /admin/classifications）。
  // 本表單所在頁面（建立/編輯文件）皆為 admin 專用，權限與端點一致。
  const [addClsModalVisible, setAddClsModalVisible] = useState(false);
  const [addClsForm] = Form.useForm();
  const [addingCls, setAddingCls] = useState(false);

  // 強制 VL 視覺解析
  const [forceVision, setForceVision] = useState(false);

  // 強制 PaddleOCR（即使是文字型 PDF）
  const [forceOcr, setForceOcr] = useState(false);

  // 上傳分析任務追蹤
  const [uploadTaskId, setUploadTaskId] = useState(null);

  // OCR 相關狀態
  const [ocrModalVisible, setOcrModalVisible] = useState(false);
  const [ocrProcessing, setOcrProcessing] = useState(false);
  const [ocrData, setOcrData] = useState(null); // { filename, total_pages, pdf_temp_path }

  const isEdit = Boolean(document);

  const fetchMetadataFields = async () => {
    try {
      const resp = await apiClient.get("metadata-fields");
      setMetadataFields(resp.data);
      const projectField = resp.data.find((f) => f.name === "project_id");
      if (projectField && projectField.options) {
        setProjectOptions(
          projectField.options
            .filter((opt) => opt.is_active !== false)
            .map((opt) => ({ label: opt.display_value, value: opt.value }))
        );
      }
    } catch (error) {
      message.error(error.response?.data?.detail ?? "載入中繼資料欄位失敗");
    }
  };

  const fetchClassifications = async () => {
    try {
      const resp = await apiClient.get("documents/classifications");
      setClassificationOptions(resp.data ?? []);
    } catch (error) {
      message.error(error.response?.data?.detail ?? "載入分類清單失敗");
    }
  };

  const fetchKeywordOptions = async () => {
    try {
      const resp = await apiClient.get("documents/keywords");
      const kws = resp.data ?? [];
      setKeywordOptions(kws.map((k) => ({ label: k, value: k })));
    } catch {
      // 靜默失敗，不影響表單使用
    }
  };

  useEffect(() => {
    fetchMetadataFields();
    fetchClassifications();
    fetchKeywordOptions();
  }, []);

  useEffect(() => {
    if (isEdit && document) {
      form.setFieldsValue({
        title: document.title,
        ai_summary: document.ai_summary ?? "",
        metadata: {
          ...(document.metadata ?? {}),
          keywords: document.metadata?.keywords ?? [],
        },
        classification_id: document.classification?.id ?? null,
        source_pdf_path: null,
      });
    } else {
      form.resetFields();
    }
  }, [document, isEdit, form]);

  // 從 banner「前往填寫表單」按鈕跳轉過來時，帶入分析結果
  // 必須在 isEdit effect 之後，避免 resetFields() 覆蓋帶入的值
  useEffect(() => {
    const result = location.state?.uploadResult;
    if (!result || isEdit) return;
    // 清除 state，避免頁面重整時重複套用
    window.history.replaceState({}, "", window.location.pathname);
    applyUploadResult(result);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const metadataInitialValues = useMemo(() => {
    if (isEdit && document?.metadata) {
      return document.metadata;
    }
    return {};
  }, [document, isEdit]);

  const findClassificationId = (label) => {
    if (!label) return null;
    const normalized = label.trim().toLowerCase();
    const byName = classificationOptions.find(
      (option) => option.name?.trim().toLowerCase() === normalized,
    );
    if (byName) {
      return byName.id;
    }

    const codeMatch = label.match(/\(([^)]+)\)\s*$/);
    if (codeMatch) {
      const codeNormalized = codeMatch[1].trim().toLowerCase();
      const byCode = classificationOptions.find(
        (option) => option.code && option.code.trim().toLowerCase() === codeNormalized,
      );
      if (byCode) {
        return byCode.id;
      }
    }
    return null;
  };

  // 開啟新增選項 Modal
  const handleOpenAddOption = (field) => {
    setAddOptionField(field);
    setAddOptionModalVisible(true);
    addOptionForm.resetFields();
  };

  // 處理圖片型 PDF（僅支持預覽）
  const handleOcrChoice = async (mode) => {
    if (!ocrData) return;

    try {
      setOcrProcessing(true);
      setOcrModalVisible(false);

      const title = form.getFieldValue("title") || ocrData.filename.replace(/\.pdf$/i, "");
      const metadata = form.getFieldValue("metadata") || {};
      const classification_id = form.getFieldValue("classification_id");

      if (mode === "vl") {
        // VL 視覺模型解析：當作普通 PDF 送出，後端背景排程 vl_vectorize
        setProcessingStatus("正在建立文件並啟動 VL 解析...");
        const payload = {
          title,
          content: "",
          metadata,
          classification_id: classification_id || null,
          source_pdf_path: ocrData.pdf_temp_path,
          is_image_based: false, // 讓後端走背景任務路徑
          force_vision: true,
        };
        const resp = await apiClient.post("documents/", payload);
        const taskId = resp.data?.task_id;
        const docId = resp.data?.id;
        const docTitle = resp.data?.title ?? title;
        if (taskId && docId) {
          addTask({ task_id: taskId, document_id: docId, document_title: docTitle });
          message.success("文件已建立，VL 視覺解析正在背景執行，完成後即可搜尋");
        } else {
          message.success("文件已建立");
        }
      } else {
        // 僅供預覽（skip）
        setProcessingStatus("正在建立文件...");
        const payload = {
          title,
          content: "",
          metadata,
          classification_id: classification_id || null,
          source_pdf_path: ocrData.pdf_temp_path,
          is_image_based: true,
        };
        const createResp = await apiClient.post("documents/", payload);
        const documentId = createResp.data.id;
        await apiClient.post(`documents/${documentId}/ocr/process`, { mode: "skip" });
        message.success("文件已建立（僅支持預覽功能）");
      }

      setProcessingStatus("");
      onSuccess?.();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "處理失敗");
    } finally {
      setOcrProcessing(false);
      setProcessingStatus("");
    }
  };

  // 新增選項
  const handleAddOption = async () => {
    try {
      const values = await addOptionForm.validateFields();
      setAddingOption(true);

      const resp = await apiClient.post(`metadata-fields/${addOptionField.id}/options`, {
        value: values.value,
        display_value: values.display_value,
        order_index: 0,
      });

      const newOption = resp.data;

      // 更新 metadataFields 中的選項列表
      setMetadataFields((prevFields) =>
        prevFields.map((f) => {
          if (f.id === addOptionField.id) {
            return {
              ...f,
              options: [...f.options, newOption],
            };
          }
          return f;
        })
      );

      // 專案選單讀的是獨立的 projectOptions state（歷史包袱），要一併同步，
      // 否則新選項存進了後端、下拉裡卻看不到。
      if (addOptionField.name === "project_id") {
        setProjectOptions((prev) => [
          ...prev, { label: newOption.display_value, value: newOption.value },
        ]);
      }

      // 自動選擇新增的選項
      const fieldPath = ["metadata", addOptionField.name];
      form.setFieldValue(fieldPath, newOption.value);

      message.success(`已新增選項：${newOption.display_value}`);
      setAddOptionModalVisible(false);
      addOptionForm.resetFields();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "新增選項失敗");
    } finally {
      setAddingOption(false);
    }
  };

  const handleAddClassification = async () => {
    try {
      const values = await addClsForm.validateFields();
      setAddingCls(true);
      const resp = await apiClient.post("admin/classifications", {
        name: values.name,
        code: values.code || null,
      });
      await fetchClassifications();
      form.setFieldValue("classification_id", resp.data.id);
      message.success(`已新增分類：${resp.data.name}`);
      setAddClsModalVisible(false);
      addClsForm.resetFields();
    } catch (error) {
      if (error?.errorFields) return;   // 表單驗證錯誤，Modal 內已顯示
      message.error(error.response?.data?.detail ?? "新增分類失敗");
    } finally {
      setAddingCls(false);
    }
  };

  const handleSubmit = async (values) => {
    console.log('handleSubmit called with values:', values);
    console.log('ocrData:', ocrData);
    console.log('isEdit:', isEdit);

    // 檢查是否有待處理的圖片型 PDF
    if (ocrData && !isEdit) {
      console.log('Opening OCR Modal...');
      // 顯示 OCR Modal 讓用戶選擇處理方式
      setOcrModalVisible(true);
      return;
    }

    // form.getFieldValue("metadata") 包含 setFieldsValue 設定的值（AI 建議）
    // values.metadata 只包含有 Form.Item 的欄位；合併以確保 AI 建議的關鍵字等不會丟失
    const storedMetadata = form.getFieldValue("metadata") || {};
    const payload = {
      title: values.title,
      content: values.content || null,
      metadata: { ...storedMetadata, ...(values.metadata || {}) },
      ai_summary: values.ai_summary || undefined,
    };

    if (values.classification_id) {
      payload.classification_id = values.classification_id;
    }

    const pdfTempPath = form.getFieldValue("source_pdf_path");
    if (pdfTempPath) {
      payload.source_pdf_path = pdfTempPath;
      // VL / OCR 模式下不傳 segments，讓後端在向量化時重新解析
      if (!forceVision && !forceOcr && suggestionState?.segments) {
        payload.segments = suggestionState.segments;
      }
      // 傳遞 AI 生成的文件摘要
      if (suggestionState?.suggestion?.summary) {
        payload.ai_summary = suggestionState.suggestion.summary;
      }
      if (forceVision) {
        payload.force_vision = true;
      }
      if (forceOcr) {
        payload.force_ocr = true;
      }
    }

    try {
      setSubmitting(true);
      if (isEdit) {
        setProcessingStatus("正在更新文件...");
        await apiClient.put(`documents/${document.id}`, payload);
        message.success("文件已更新");
      } else {
        setProcessingStatus("正在建立文件...");
        const resp = await apiClient.post("documents/", payload);
        const taskId = resp.data?.task_id;
        const docId = resp.data?.id;
        const docTitle = resp.data?.title ?? payload.title;
        if (taskId && docId) {
          // 有 task_id → 向量化在背景執行，立刻可以繼續操作
          addTask({ task_id: taskId, document_id: docId, document_title: docTitle });
          message.success(
            forceOcr
              ? "文件已建立，PaddleOCR 辨識正在背景執行，完成後即可搜尋"
              : forceVision
              ? "文件已建立，VL 視覺解析正在背景執行，可繼續使用系統"
              : "文件已建立，向量索引正在背景執行，稍後即可搜尋"
          );
        } else {
          message.success("文件已建立");
        }
      }
      onSuccess?.();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "儲存文件時發生錯誤");
    } finally {
      setSubmitting(false);
      setProcessingStatus("");
    }
  };

  // 接收分析完成的結果，填入表單並開啟建議 Modal
  const applyUploadResult = useCallback((result) => {
    if (!result) return;

    if (result.is_image_based) {
      setOcrData({
        filename: result.filename,
        total_pages: result.total_pages,
        pdf_temp_path: result.pdf_temp_path,
      });
      form.setFieldsValue({
        title: form.getFieldValue("title") || (result.filename?.replace(/\.pdf$/i, "") ?? ""),
        source_pdf_path: result.pdf_temp_path,
      });
      setUploading(false);
      setUploadTaskId(null);
      message.warning({
        content: `檢測到圖片型 PDF（共 ${result.total_pages ?? "?"} 頁），僅支持預覽功能。`,
        duration: 5,
      });
      return;
    }

    const currentMetadata = form.getFieldValue("metadata") || {};
    const suggestedMetadata = result.suggested_metadata || {};
    const mergedKeywords = Array.from(
      new Set([...(currentMetadata.keywords || []), ...(suggestedMetadata.keywords ?? [])]),
    );
    const metadataPatch = { ...currentMetadata, keywords: mergedKeywords };
    if (suggestedMetadata.file_type) metadataPatch.file_type = suggestedMetadata.file_type;

    form.setFieldsValue({
      title: form.getFieldValue("title") || (result.filename?.replace(/\.pdf$/i, "") ?? ""),
      content: result.text || "",
      metadata: metadataPatch,
      source_pdf_path: result.pdf_temp_path,
    });

    setSuggestionState({
      suggestion: result.suggestion,
      segments: result.segments ?? [],
      suggestedMetadata,
      text: result.text,
    });
    setSuggestionVisible(true);
    setUploading(false);
    setUploadTaskId(null);
    message.success("PDF 分析完成，請確認 AI 建議");
  }, [form]);

  // 組件重新 mount 時（用戶切換頁面後回來），恢復進行中的上傳任務狀態
  useEffect(() => {
    if (isEdit) return;
    const activeUpload = tasks.find(
      (t) => t.task_type === "pdf_analyze" &&
        (taskStatuses[t.task_id]?.status === "pending" ||
         taskStatuses[t.task_id]?.status === "running" ||
         !taskStatuses[t.task_id])  // 尚未收到第一次 poll 結果
    );
    if (!activeUpload) return;

    setUploadTaskId(activeUpload.task_id);
    setUploading(true);
    setProcessingStatus("PDF 正在背景分析中，可先填寫其他欄位...");

    // 重新註冊 callback，確保任務完成時能觸發新的 applyUploadResult
    addTask(
      { task_id: activeUpload.task_id, task_type: "pdf_analyze", document_title: activeUpload.document_title },
      (taskData) => {
        if (taskData.status === "completed" && taskData.result) {
          applyUploadResult(taskData.result);
        } else if (taskData.status === "failed") {
          message.error(taskData.error ?? "PDF 分析失敗");
          setUploading(false);
          setUploadTaskId(null);
        }
      }
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handlePdfUpload = async (file) => {
    const formData = new FormData();
    formData.append("file", file);

    try {
      setUploading(true);
      setProcessingStatus("正在上傳 PDF...");

      // 呼叫非同步 upload endpoint，立刻取得 task_id
      const resp = await apiClient.post("documents/upload/async", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });

      const taskId = resp.data.id;
      setUploadTaskId(taskId);
      setProcessingStatus("PDF 正在背景分析中，可先填寫其他欄位...");

      // 把任務加入全域追蹤，完成時呼叫 applyUploadResult
      addTask(
        { task_id: taskId, task_type: "pdf_analyze", document_title: file.name },
        (taskData) => {
          if (taskData.status === "completed" && taskData.result) {
            applyUploadResult(taskData.result);
          } else if (taskData.status === "failed") {
            message.error(taskData.error ?? "PDF 分析失敗");
            setUploading(false);
            setUploadTaskId(null);
          }
        }
      );

    } catch (error) {
      message.error(error.response?.data?.detail ?? "上傳 PDF 時發生錯誤");
      setUploading(false);
    }

    return false;
  };

  const handleApplySuggestion = async (editedSuggestion) => {
    const suggestion = editedSuggestion ?? suggestionState?.suggestion;
    if (!suggestion) {
      setSuggestionVisible(false);
      return;
    }

    const { suggestedMetadata } = suggestionState ?? {};
    const currentMetadata = form.getFieldValue("metadata") || {};
    const nextMetadata = { ...currentMetadata };

    // 使用用戶在 Modal 中可能修改過的 keywords
    const mergedKeywords = Array.from(
      new Set([
        ...(Array.isArray(currentMetadata.keywords) ? currentMetadata.keywords : []),
        ...(Array.isArray(suggestion.keywords) ? suggestion.keywords : []),
      ]),
    );
    if (mergedKeywords.length) {
      nextMetadata.keywords = mergedKeywords;
    }

    if (suggestedMetadata?.file_type) {
      nextMetadata.file_type = suggestedMetadata.file_type;
    }

    let classificationId = suggestion.classification_is_new
      ? null
      : findClassificationId(suggestion.classification);

    const payload = {};
    // 只處理分類的新增建議
    if (suggestion.classification_is_new && suggestion.classification) {
      payload.classification = {
        name: suggestion.classification,
        description: suggestion.classification_reason ?? null,
      };
    }

    if (Object.keys(payload).length > 0) {
      try {
        const resp = await apiClient.post("documents/suggestions/accept", payload);
        const createdClassification = resp.data?.classification;

        if (createdClassification) {
          classificationId = createdClassification.id;
          setClassificationOptions((prev) => {
            if (prev.some((item) => item.id === createdClassification.id)) {
              return prev;
            }
            return [...prev, createdClassification];
          });
        }
      } catch (error) {
        message.error(error.response?.data?.detail ?? "無法新增 AI 建議的分類");
        return;
      }
    }

    if (!classificationId && suggestion.classification && !suggestion.classification_is_new) {
      classificationId = findClassificationId(suggestion.classification);
    }

    const updates = { metadata: nextMetadata };
    if (classificationId) {
      updates.classification_id = classificationId;
    }
    if (editedSuggestion?.summary !== undefined) {
      updates.ai_summary = editedSuggestion.summary;
    }

    form.setFieldsValue(updates);
    if (editedSuggestion?.summary !== undefined) {
      setSuggestionState((prev) => ({
        ...prev,
        suggestion: { ...prev.suggestion, summary: editedSuggestion.summary },
      }));
    }
    setSuggestionVisible(false);
    message.success("已套用 AI 建議，請確認內容後再儲存文件");
  };

  const renderMetadataField = (field) => {
    const baseName = ["metadata", field.name];
    const rules = field.is_required
      ? [{ required: true, message: `請填寫「${field.display_name}」` }]
      : [];

    switch (field.field_type) {
      case "text":
      case "textarea":
        return (
          <Form.Item key={field.id} name={baseName} label={field.display_name} rules={rules}>
            <Input.TextArea rows={field.field_type === "textarea" ? 4 : 2} />
          </Form.Item>
        );
      case "number":
        return (
          <Form.Item key={field.id} name={baseName} label={field.display_name} rules={rules}>
            <Input type="number" />
          </Form.Item>
        );
      case "select":
        return (
          <Form.Item key={field.id} name={baseName} label={field.display_name} rules={rules}>
            <Select
              options={field.options.map((opt) => ({
                label: opt.display_value,
                value: opt.value,
              }))}
              dropdownRender={(menu) => (
                <>
                  {menu}
                  <Divider style={{ margin: "8px 0" }} />
                  <Button
                    type="text"
                    icon={<PlusOutlined />}
                    style={{ width: "100%", textAlign: "left" }}
                    onClick={() => handleOpenAddOption(field)}
                  >
                    新增 {field.display_name}
                  </Button>
                </>
              )}
            />
          </Form.Item>
        );
      case "multi_select":
        return (
          <Form.Item key={field.id} name={baseName} label={field.display_name} rules={rules}>
            <Select
              mode="multiple"
              options={field.options.map((opt) => ({
                label: opt.display_value,
                value: opt.value,
              }))}
              dropdownRender={(menu) => (
                <>
                  {menu}
                  <Divider style={{ margin: "8px 0" }} />
                  <Button
                    type="text"
                    icon={<PlusOutlined />}
                    style={{ width: "100%", textAlign: "left" }}
                    onClick={() => handleOpenAddOption(field)}
                  >
                    新增 {field.display_name}
                  </Button>
                </>
              )}
            />
          </Form.Item>
        );
      default:
        return null;
    }
  };

  return (
    <Card title={isEdit ? "編輯文件" : "建立文件"} loading={loading}>
      <Typography.Paragraph type="secondary">
        上傳 PDF 後系統會自動擷取文字與分段資訊，並呼叫 AI 產出繁體中文建議。
      </Typography.Paragraph>
      <Dragger
        multiple={false}
        accept=".pdf"
        beforeUpload={handlePdfUpload}
        showUploadList={false}
        disabled={uploading}
        style={{ marginBottom: 16 }}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">點擊或拖曳 PDF 檔案到此上傳</p>
        <p className="ant-upload-hint">僅支援 PDF，系統會即時解析並提供 AI 建議。</p>
      </Dragger>

      <div style={{ marginBottom: 12 }}>
        <Checkbox
          checked={forceVision}
          onChange={(e) => {
            setForceVision(e.target.checked);
            if (e.target.checked) setForceOcr(false);
          }}
          disabled={uploading || forceOcr}
        >
          強制使用視覺模型解析（適合含表格、欄位對齊或大量圖片的 PDF/PPT 轉檔）
        </Checkbox>
        {forceVision && (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 4, fontSize: 12 }}>
            解析時間較長（每頁約 10–30 秒），圖片內容也會被描述並納入向量索引。
          </Typography.Text>
        )}
      </div>

      <div style={{ marginBottom: 12 }}>
        <Checkbox
          checked={forceOcr}
          onChange={(e) => {
            setForceOcr(e.target.checked);
            if (e.target.checked) setForceVision(false);
          }}
          disabled={uploading || forceVision}
        >
          強制使用 PaddleOCR 辨識（即使是文字型 PDF 也重新走 OCR）
        </Checkbox>
        {forceOcr && (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 4, fontSize: 12 }}>
            適合 pdfminer 抽取結果不完整、亂碼、或想統一用 OCR 重做索引的情況；
            首次執行需下載 PaddleOCR 模型，速度較慢。
          </Typography.Text>
        )}
      </div>

      {uploading && (
        <Card style={{ marginBottom: 16 }} size="small">
          <Space direction="vertical" style={{ width: "100%" }}>
            <Typography.Text strong>{processingStatus}</Typography.Text>
            {uploadTaskId && taskStatuses[uploadTaskId] ? (
              <>
                <Progress
                  percent={taskStatuses[uploadTaskId].progress ?? 0}
                  status="active"
                  strokeColor={{ from: "#1677ff", to: "#52c41a" }}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {taskStatuses[uploadTaskId].message ?? ""}
                </Typography.Text>
              </>
            ) : (
              <Progress percent={5} status="active" strokeColor="#1677ff" />
            )}
          </Space>
        </Card>
      )}

      {/* 圖片型 PDF 提示 */}
      {ocrData && !isEdit && (
        <Alert
          message="檢測到圖片型 PDF（僅支持預覽）"
          description={`此文件共 ${ocrData.total_pages} 頁，無法直接提取文字。圖片型 PDF 僅支持預覽功能，無法進行全文檢索或 AI 問答。請填寫下方必填欄位後保存文件。`}
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <Form
        layout="vertical"
        form={form}
        onFinish={handleSubmit}
        initialValues={{ metadata: metadataInitialValues }}
      >
        <Form.Item name="title" label="標題" rules={[{ required: true, message: "請輸入標題" }]}>
          <Input />
        </Form.Item>

        {/* content 欄位已移除，直接從 PDF 提取，使用 PDF 預覽查看內容 */}
        <Form.Item name="content" hidden>
          <Input type="hidden" />
        </Form.Item>

        <Form.Item name="classification_id" label="分類">
          <Select
            allowClear
            placeholder="選擇分類"
            options={classificationOptions.map((option) => ({
              value: option.id,
              label: option.code ? `${option.name} (${option.code})` : option.name,
            }))}
            dropdownRender={(menu) => (
              <>
                {menu}
                <Divider style={{ margin: "8px 0" }} />
                <Button
                  type="text"
                  icon={<PlusOutlined />}
                  style={{ width: "100%", textAlign: "left" }}
                  onClick={() => { addClsForm.resetFields(); setAddClsModalVisible(true); }}
                >
                  新增分類
                </Button>
              </>
            )}
          />
        </Form.Item>

        <Form.Item name="source_pdf_path" hidden>
          <Input type="hidden" />
        </Form.Item>

        {/* 條件改為「欄位存在」而非「有選項」：欄位剛建立、還沒有任何選項時，
            也要看得到選單才能用下面的「新增專案」。 */}
        {metadataFields.some((f) => f.name === "project_id") && (
          <Form.Item name={["metadata", "project_id"]} label="所屬專案">
            <Select
              allowClear
              showSearch
              placeholder="選擇專案"
              optionFilterProp="label"
              options={projectOptions}
              dropdownRender={(menu) => (
                <>
                  {menu}
                  <Divider style={{ margin: "8px 0" }} />
                  <Button
                    type="text"
                    icon={<PlusOutlined />}
                    style={{ width: "100%", textAlign: "left" }}
                    onClick={() => {
                      const pf = metadataFields.find((f) => f.name === "project_id");
                      if (pf) handleOpenAddOption(pf);
                    }}
                  >
                    新增專案
                  </Button>
                </>
              )}
            />
          </Form.Item>
        )}

        <Form.Item name="ai_summary" label="AI 摘要">
          <Input.TextArea rows={4} placeholder="AI 自動生成的文件摘要（可手動修改）" />
        </Form.Item>

        {/* 關鍵字：若 metadata fields 系統未定義 keywords 欄位，則顯示此直接欄位 */}
        {!metadataFields.some((f) => f.name === "keywords") && (
          <Form.Item name={["metadata", "keywords"]} label="關鍵字">
            <Select
              mode="tags"
              placeholder="輸入關鍵字後按 Enter 新增"
              tokenSeparators={[","]}
              options={keywordOptions}
              filterOption={(input, option) =>
                option.value.toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>
        )}

        {metadataFields.filter((f) => f.name !== "project_id").map((field) => renderMetadataField(field))}

        {submitting && processingStatus && (
          <Card style={{ marginBottom: 16 }} size="small">
            <Space direction="vertical" style={{ width: "100%", textAlign: "center" }}>
              <Spin size="large" />
              <Typography.Text strong>{processingStatus}</Typography.Text>
            </Space>
          </Card>
        )}

        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={submitting} disabled={uploading || submitting}>
              {isEdit ? "更新" : "建立"}
            </Button>
            <Button onClick={onCancel} disabled={submitting}>取消</Button>
          </Space>
        </Form.Item>
      </Form>

      <AISuggestionModal
        open={suggestionVisible}
        suggestion={suggestionState?.suggestion}
        suggestedMetadata={suggestionState?.suggestedMetadata}
        segments={suggestionState?.segments ?? []}
        onApply={handleApplySuggestion}
        onClose={() => setSuggestionVisible(false)}
      />

      {/* 新增選項 Modal */}
      <Modal
        title={`新增${addOptionField?.display_name ?? "選項"}`}
        open={addOptionModalVisible}
        onOk={handleAddOption}
        onCancel={() => {
          setAddOptionModalVisible(false);
          addOptionForm.resetFields();
        }}
        confirmLoading={addingOption}
        okText="新增"
        cancelText="取消"
      >
        <Form form={addOptionForm} layout="vertical">
          <Form.Item
            name="value"
            label="選項值（英文代碼）"
            rules={[
              { required: true, message: "請輸入選項值" },
              { pattern: /^[a-z0-9_-]+$/, message: "只能包含小寫英文、數字、底線和連字號" },
            ]}
            extra="例如：ai_research, project_alpha"
          >
            <Input placeholder="輸入選項值（英文代碼）" />
          </Form.Item>
          <Form.Item
            name="display_value"
            label="顯示名稱"
            rules={[{ required: true, message: "請輸入顯示名稱" }]}
            extra="顯示在介面上的名稱"
          >
            <Input placeholder="輸入顯示名稱" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 新增分類 Modal（分類不是 metadata 欄位，另走 /admin/classifications） */}
      <Modal
        title="新增分類"
        open={addClsModalVisible}
        onOk={handleAddClassification}
        onCancel={() => {
          setAddClsModalVisible(false);
          addClsForm.resetFields();
        }}
        confirmLoading={addingCls}
        okText="新增"
        cancelText="取消"
      >
        <Form form={addClsForm} layout="vertical">
          <Form.Item
            name="name"
            label="分類名稱"
            rules={[{ required: true, message: "請輸入分類名稱" }]}
            extra="例如：教育訓練文件、產品規格書"
          >
            <Input placeholder="輸入分類名稱" />
          </Form.Item>
          <Form.Item
            name="code"
            label="分類代碼（選填）"
            rules={[{ pattern: /^[a-z0-9_-]*$/, message: "只能包含小寫英文、數字、底線和連字號" }]}
            extra="例如：training、spec。留空則不設代碼"
          >
            <Input placeholder="輸入英文代碼" />
          </Form.Item>
        </Form>
      </Modal>

      {/* OCR 選項 Modal */}
      <Modal
        title="偵測到圖片型 PDF"
        open={ocrModalVisible}
        onCancel={() => setOcrModalVisible(false)}
        footer={null}
        width={620}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="large">
          <Alert
            message="此 PDF 無法直接提取文字"
            description={`這可能是掃描件、傳真或 PPT 轉檔（共 ${ocrData?.total_pages || "?"} 頁），請選擇處理方式。`}
            type="warning"
            showIcon
          />

          <div style={{ display: "flex", gap: 16 }}>
            {/* VL 解析選項 */}
            <Card
              hoverable
              style={{ flex: 1, borderColor: "#1677ff", cursor: "pointer" }}
              onClick={() => !ocrProcessing && handleOcrChoice("vl")}
            >
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Typography.Text strong style={{ fontSize: 15, color: "#1677ff" }}>
                  🤖 VL 視覺模型解析（建議）
                </Typography.Text>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: "#444" }}>
                  <li>✅ 提取所有可見文字及排版結構</li>
                  <li>✅ 識別並描述圖片、圖表內容</li>
                  <li>✅ 保留表格的行列關係</li>
                  <li>✅ 支援全文檢索與 AI 問答</li>
                  <li>⏳ 解析時間較長（每頁約 10–30 秒）</li>
                </ul>
              </Space>
            </Card>

            {/* 僅預覽選項 */}
            <Card
              hoverable
              style={{ flex: 1, cursor: "pointer" }}
              onClick={() => !ocrProcessing && handleOcrChoice("skip")}
            >
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Typography.Text strong style={{ fontSize: 15 }}>
                  👁 僅供預覽
                </Typography.Text>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: "#444" }}>
                  <li>✅ 可查看 PDF 原始頁面</li>
                  <li>✅ 可管理 metadata</li>
                  <li>❌ 無法全文檢索</li>
                  <li>❌ 無法 AI 問答</li>
                  <li>⚡ 立即完成，無等待</li>
                </ul>
              </Space>
            </Card>
          </div>

          {ocrProcessing && (
            <div style={{ textAlign: "center" }}>
              <Spin tip={processingStatus || "處理中..."} />
            </div>
          )}
        </Space>
      </Modal>
    </Card>
  );
};

export default DocumentForm;
