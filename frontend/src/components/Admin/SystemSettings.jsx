import { useEffect, useState } from "react";
import {
  Card,
  Button,
  AutoComplete,
  message,
  Radio,
  Statistic,
  Row,
  Col,
  Alert,
  Modal,
  Spin,
  Form,
  InputNumber,
  Switch,
  Tag,
  Divider,
  Space,
  Typography,
} from "antd";
import {
  DatabaseOutlined,
  DeleteOutlined,
  WarningOutlined,
  CheckCircleOutlined,
  CloudOutlined,
  ExperimentOutlined,
  SaveOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  EyeOutlined,
  FileSearchOutlined,
  NodeIndexOutlined,
} from "@ant-design/icons";
import apiClient from "../../services/api";

const { Text } = Typography;

// 預設模型清單(可自由打字覆寫,只是當下拉提示用)
const PRESET_LLM_OLLAMA = [
  { label: "Gemma 4 E2B  (2.3B, 128K ctx, 1.5GB)", value: "gemma4:e2b" },
  { label: "Gemma 4 E4B  (4B, 128K ctx)", value: "gemma4:e4b" },
  { label: "Gemma 3 12B", value: "gemma3:12b" },
  { label: "Gemma 3 27B", value: "gemma3:27b" },
  { label: "Qwen 2.5 7B", value: "qwen2.5:7b" },
  { label: "Qwen 2.5 14B", value: "qwen2.5:14b" },
  { label: "Qwen 3 8B", value: "qwen3:8b" },
  { label: "Llama 3.1 8B", value: "llama3.1:8b" },
  { label: "Mistral 7B", value: "mistral:7b" },
];

// AiHub OpenAPI v0.9 提供的模型。值對應 provider 的 service/version 簡寫。
const PRESET_LLM_AIHUB = [
  { label: "GPT-OSS 120B  (自建, 適合機密資料)", value: "gpt-oss" },
  { label: "GPT-4.1", value: "gpt41" },
  { label: "GPT-4.1 mini  (較快)", value: "gpt41-mini" },
  { label: "Claude 4.5", value: "claude45" },
  { label: "Claude 3.7", value: "claude37" },
  { label: "Gemini 2.5 Pro", value: "gemini2.5pro" },
  { label: "Gemini 2.5 Flash  (較快)", value: "gemini2.5flash" },
];

const PRESET_EMBED_OLLAMA = [
  { label: "BGE Large 中文 v1.5  (1024 dim, 中文最強)", value: "quentinz/bge-large-zh-v1.5:latest" },
  { label: "Qwen3 Embedding 4B", value: "qwen3-embedding:4b" },
  { label: "MXBai Embed Large  (1024 dim)", value: "mxbai-embed-large" },
  { label: "Nomic Embed Text  (768 dim)", value: "nomic-embed-text" },
];


const PRESET_VL_OLLAMA = [
  { label: "Gemma 4 E2B  (新, 128K ctx, 沒 GGML bug, 細節較弱)", value: "gemma4:e2b" },
  { label: "Gemma 4 E4B  (新, 128K ctx)", value: "gemma4:e4b" },
  { label: "Gemma 3 4B  (vision)", value: "gemma3:4b" },
  { label: "Gemma 3 12B  (vision)", value: "gemma3:12b" },
  { label: "Gemma 3 27B  (vision, 品質高 / VRAM 大)", value: "gemma3:27b" },
  { label: "Qwen 2.5-VL 3B  (⚠️ Ollama 0.23.x GGML_ASSERT bug)", value: "qwen2.5vl:3b" },
  { label: "Qwen 2.5-VL 7B  (⚠️ 同上)", value: "qwen2.5vl:7b" },
  { label: "MiniCPM-V 8B", value: "minicpm-v:8b" },
  { label: "Llava 7B", value: "llava:7b" },
  { label: "Llava 13B", value: "llava:13b" },
  { label: "Llama 3.2 Vision 11B", value: "llama3.2-vision:11b" },
];

const filterOption = (input, option) => {
  const q = (input || "").toLowerCase();
  return (option?.value || "").toLowerCase().includes(q) || (option?.label || "").toLowerCase().includes(q);
};

const SystemSettings = () => {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [savingQuery, setSavingQuery] = useState(false);
  const [savingVector, setSavingVector] = useState(false);
  const [queryForm] = Form.useForm();
  const [vectorForm] = Form.useForm();
  const [llmProviderForm] = Form.useForm();
  const [llmProviderConfig, setLlmProviderConfig] = useState(null);
  const [savingLlm, setSavingLlm] = useState(false);
  const [testingLlm, setTestingLlm] = useState(false);

  const [ocrForm] = Form.useForm();
  const [concurrencyForm] = Form.useForm();
  const [savingConcurrency, setSavingConcurrency] = useState(false);
  const [savingOcr, setSavingOcr] = useState(false);

  // 載入系統配置
  const fetchConfig = async () => {
    try {
      setLoading(true);
      const resp = await apiClient.get("admin/system-config");
      setConfig(resp.data);

      // 設置表單初始值
      if (resp.data.vector_config) {
        // 查詢參數表單
        queryForm.setFieldsValue({
          min_similarity_score: resp.data.vector_config.min_similarity_score,
          default_top_k: resp.data.vector_config.default_top_k,
          search_multiplier: resp.data.vector_config.search_multiplier,
        });

        // 向量化參數表單
        vectorForm.setFieldsValue({
          overlap_chars: resp.data.vector_config.overlap_chars,
          max_chars: resp.data.vector_config.max_chars,
          overlap_enabled: resp.data.vector_config.overlap_chars > 0,
        });
      }
    } catch (error) {
      message.error("載入系統配置失敗");
    } finally {
      setLoading(false);
    }
  };

  const fetchLlmProviderConfig = async () => {
    try {
      const resp = await apiClient.get("admin/llm-provider");
      setLlmProviderConfig(resp.data);
      llmProviderForm.setFieldsValue({
        llm_provider: resp.data.llm_provider || "ollama",
        llm_model: resp.data.llm_model || "",
        embedding_provider: resp.data.embedding_provider || "ollama",
        embedding_model: resp.data.embedding_model || "",
        vision_provider: resp.data.vision_provider || "ollama",
        vision_model: resp.data.vision_model || "",
      });
    } catch (e) {
      // silent — endpoint may be admin-only or backend unreachable
    }
  };

  const handleSaveLlmProvider = async (values) => {
    setSavingLlm(true);
    try {
      // embedding / vision 固定本地 Ollama，後端會強制寫回，這裡不送 provider
      const payload = {
        llm_provider: values.llm_provider,
        llm_model: values.llm_model || null,
        embedding_model: values.embedding_model || null,
        vision_model: values.vision_model || null,
      };
      await apiClient.put("admin/llm-provider", payload);
      message.success("LLM 設定已儲存,下一次呼叫將使用新後端");
      await fetchLlmProviderConfig();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "儲存失敗");
    } finally {
      setSavingLlm(false);
    }
  };

  const handleTestLlmProvider = async () => {
    setTestingLlm(true);
    try {
      const resp = await apiClient.post("admin/llm-provider/test");
      const d = resp.data || {};
      if (d.ok) {
        message.success(`測試成功 (provider=${d.provider}, version=${d.version || "n/a"})`);
      } else {
        message.error(`測試失敗：${d.error || "未知錯誤"}`);
      }
    } catch (e) {
      message.error("測試請求失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setTestingLlm(false);
    }
  };

  const handleTestVisionProvider = async () => {
    setTestingLlm(true);
    try {
      const resp = await apiClient.post("admin/llm-provider/test-vision");
      const d = resp.data || {};
      if (d.ok) {
        message.success(`VL 模型 '${d.model}' 已下載 (Ollama 共 ${d.available} 個模型)`);
      } else {
        const suggestion = d.suggestion?.length ? `\n相似名稱:${d.suggestion.join(", ")}` : "";
        message.error(`VL 測試失敗:${d.error || "未知錯誤"}${suggestion}`, 8);
      }
    } catch (e) {
      message.error("測試請求失敗:" + (e.response?.data?.detail || e.message));
    } finally {
      setTestingLlm(false);
    }
  };

  const fetchOcrConfig = async () => {
    try {
      const resp = await apiClient.get("admin/ocr-config");
      ocrForm.setFieldsValue({
        engine: resp.data.engine || "rapid",
        model_tier: resp.data.model_tier || "mobile",
        version: resp.data.version || "PP-OCRv4",
        device: resp.data.device || "cpu",
      });
    } catch (e) {
      // silent — admin-only or backend unreachable
    }
  };

  const [resettingInference, setResettingInference] = useState(false);

  const handleResetInference = () => {
    Modal.confirm({
      title: "重置推論引擎？",
      content: (
        <div>
          <p>將卸載 Ollama 上所有已載入的模型，下一次查詢會重新載入（約多等 15~30 秒）。</p>
          <p>適用時機：<strong>答案品質突然變樣</strong>（例如原本完整的表格塌成單張列表）。
            這是小顯存機器上模型重載偶發落入壞狀態的已知現象，乾淨重載即可恢復。</p>
          <p>請先確認目前沒有進行中的查詢。</p>
        </div>
      ),
      okText: "重置",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setResettingInference(true);
        try {
          const resp = await apiClient.post("admin/reset-inference");
          const names = (resp.data.unloaded || []).join("、") || "（沒有已載入的模型）";
          if (resp.data.ok) {
            message.success(`已卸載：${names}。下一次查詢將乾淨重載。`);
          } else {
            message.warning(`部分卸載失敗：${(resp.data.failed || []).join("、")}`);
          }
        } catch (error) {
          message.error(error.response?.data?.detail ?? "重置失敗");
        } finally {
          setResettingInference(false);
        }
      },
    });
  };

  const [showKg, setShowKg] = useState(true);
  const [savingUiConfig, setSavingUiConfig] = useState(false);

  const fetchUiConfig = async () => {
    try {
      const resp = await apiClient.get("admin/ui-config");
      setShowKg(resp.data.show_knowledge_graph !== false);
    } catch (e) {
      // silent
    }
  };

  const handleToggleKg = async (checked) => {
    setSavingUiConfig(true);
    try {
      await apiClient.put("admin/ui-config", { show_knowledge_graph: checked });
      setShowKg(checked);
      message.success(checked
        ? "已顯示知識圖譜。側邊欄入口將在下次載入頁面時出現。"
        : "已隱藏知識圖譜入口。KG 抽取仍在背景執行、資料持續累積，隨時可再打開。");
    } catch (error) {
      message.error(error.response?.data?.detail ?? "儲存失敗");
    } finally {
      setSavingUiConfig(false);
    }
  };

  const fetchLlmConcurrency = async () => {
    try {
      const resp = await apiClient.get("admin/llm-concurrency");
      concurrencyForm.setFieldsValue({ max_concurrency: resp.data.max_concurrency ?? 1 });
    } catch (e) {
      // silent — admin-only or backend unreachable
    }
  };

  const handleSaveLlmConcurrency = async (values) => {
    setSavingConcurrency(true);
    try {
      await apiClient.put("admin/llm-concurrency", {
        max_concurrency: values.max_concurrency,
      });
      message.success(
        values.max_concurrency === 1
          ? "已設為序列化：同時只允許一個 LLM 請求，後來者排隊"
          : `已允許 ${values.max_concurrency} 個 LLM 請求同時進行`
      );
      await fetchLlmConcurrency();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "儲存失敗");
    } finally {
      setSavingConcurrency(false);
    }
  };

  const handleSaveOcrConfig = async (values) => {
    setSavingOcr(true);
    try {
      await apiClient.put("admin/ocr-config", {
        engine: values.engine,
        model_tier: values.model_tier,
        version: values.version,
        device: values.device,
      });
      message.success("OCR 設定已儲存,下一份文件 OCR 將使用新設定");
      await fetchOcrConfig();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "儲存失敗");
    } finally {
      setSavingOcr(false);
    }
  };

  useEffect(() => {
    fetchConfig();
    fetchLlmProviderConfig();
    fetchOcrConfig();
    fetchLlmConcurrency();
    fetchUiConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 保存查詢參數配置（立即生效，無需重新向量化）
  const handleSaveQueryConfig = async (values) => {
    try {
      setSavingQuery(true);

      // 獲取當前的向量化參數
      const currentVectorConfig = config?.vector_config || {};

      // 合併配置：保持向量化參數不變，只更新查詢參數
      const configData = {
        overlap_chars: currentVectorConfig.overlap_chars || 0,
        max_chars: currentVectorConfig.max_chars || 1800,
        min_similarity_score: values.min_similarity_score,
        default_top_k: values.default_top_k,
        search_multiplier: values.search_multiplier,
      };

      await apiClient.put("admin/vector-config", configData);
      message.success("查詢參數已成功保存，立即生效！");
      fetchConfig();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "保存配置失敗");
    } finally {
      setSavingQuery(false);
    }
  };

  // 保存向量化參數配置（需要重新向量化才能生效）
  const handleSaveVectorConfig = async (values) => {
    try {
      setSavingVector(true);

      // 獲取當前的查詢參數
      const currentVectorConfig = config?.vector_config || {};

      // 根據開關決定 overlap_chars 的值
      const configData = {
        overlap_chars: values.overlap_enabled ? values.overlap_chars : 0,
        max_chars: values.max_chars,
        min_similarity_score: currentVectorConfig.min_similarity_score || 0.3,
        default_top_k: currentVectorConfig.default_top_k || 5,
        search_multiplier: currentVectorConfig.search_multiplier || 10,
      };

      await apiClient.put("admin/vector-config", configData);
      message.success("向量化參數已成功保存，請刪除向量並重新向量化文件！");
      fetchConfig();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "保存配置失敗");
    } finally {
      setSavingVector(false);
    }
  };

  // 刪除所有向量值
  const handleClearVectors = () => {
    Modal.confirm({
      title: "確認刪除所有向量值",
      icon: <WarningOutlined />,
      content: (
        <div>
          <p>此操作將刪除所有文件的向量數據。</p>
          <p><strong>注意事項：</strong></p>
          <ul>
            <li>保留所有文件和文本內容</li>
            <li>刪除所有向量（embeddings）</li>
            <li>刪除 FAISS 向量索引</li>
            <li>刪除後可使用「重新向量化」功能重建</li>
            <li>RAG 搜索功能將暫時不可用</li>
          </ul>
          <p>當前文件數量：<strong>{config?.total_documents || 0}</strong></p>
          <p>當前向量塊數量：<strong>{config?.total_chunks || 0}</strong></p>
        </div>
      ),
      okText: "確認刪除",
      cancelText: "取消",
      okType: "danger",
      onOk: async () => {
        try {
          setClearing(true);
          message.loading("正在刪除向量值，請稍候...", 0);

          const resp = await apiClient.post("admin/clear-vectors");

          message.destroy();
          message.success(
            `成功刪除所有向量值（共 ${resp.data.cleared_chunks} 個 chunks）`
          );

          fetchConfig();
        } catch (error) {
          message.destroy();
          message.error(error.response?.data?.detail ?? "刪除向量值失敗");
        } finally {
          setClearing(false);
        }
      },
    });
  };

  if (loading && !config) {
    return (
      <Card>
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin size="large" />
        </div>
      </Card>
    );
  }

  return (
    <div>
      {/* 系統狀態 */}
      <Card title="系統狀態" style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          <Col span={6}>
            <Statistic
              title="Embedding 模型"
              value={config?.embedding_model || "-"}
              prefix={<DatabaseOutlined />}
            />
          </Col>
          <Col span={6}>
            <Statistic
              title={
                <Space size={4}>
                  LLM 模型
                  {config?.llm_provider === "aihub" && <Tag color="blue">AiHub 雲端</Tag>}
                </Space>
              }
              value={config?.llm_model || "-"}
              prefix={<RobotOutlined />}
            />
          </Col>
          <Col span={4}>
            <Statistic
              title="文件總數"
              value={config?.total_documents || 0}
              prefix={<CheckCircleOutlined />}
            />
          </Col>
          <Col span={4}>
            <Statistic
              title="向量塊總數"
              value={config?.total_chunks || 0}
              prefix={<DatabaseOutlined />}
            />
          </Col>
          <Col span={4}>
            <Statistic
              title="FAISS 索引"
              value={config?.faiss_index_exists ? "已建立" : "未建立"}
              valueStyle={{
                color: config?.faiss_index_exists ? "#3f8600" : "#cf1322",
              }}
            />
          </Col>
        </Row>

        <Row gutter={16} style={{ marginTop: 16 }}>
          <Col span={6}>
            <Statistic
              title="Vision 模型"
              value={config?.vision_model || "-"}
              prefix={<EyeOutlined />}
            />
          </Col>
          <Col span={6}>
            <Statistic
              title="Ollama 版本"
              value={config?.ollama_version || "-"}
              prefix={<ToolOutlined />}
            />
          </Col>
        </Row>

        <Alert
          message="模型說明"
          description={
            <div>
              <p><strong>Embedding 模型：</strong>用於文本向量化（需要在 .env 中修改 OLLAMA_EMBED_MODEL）</p>
              <p><strong>LLM 模型：</strong>用於生成回答和分析（本地模型改 .env 的 OLLAMA_LLM_MODEL；雲端請在下方切換為 AiHub）</p>
              <p><strong>Vision 模型：</strong>用於處理 PDF 圖片辨識（需要在 .env 中修改 OLLAMA_VISION_MODEL）</p>
            </div>
          }
          type="info"
          showIcon
          style={{ marginTop: 16 }}
        />
      </Card>

      {/* LLM Provider 設定 */}
      <Card
        title={
          <span>
            <CloudOutlined style={{ marginRight: 8 }} />
            LLM Provider 設定
          </span>
        }
        style={{ marginBottom: 16 }}
        extra={
          llmProviderConfig && (
            llmProviderConfig.aihub_api_key_set
              ? <Tag color="green">AiHub Key 已設定 ({llmProviderConfig.aihub_api_key_preview})</Tag>
              : <Tag color="orange">AiHub Key 未填寫</Tag>
          )
        }
      >
        <Alert
          message="只有「文字模型」可切換後端"
          description={
            <div>
              <p><strong>文字模型:</strong> 給 Agent / RAG 回答用 — 本地 Ollama 私密但吃 GPU、AiHub 雲端快且不佔顯卡。</p>
              <p><strong>Embedding 與 VL:</strong> 固定使用本地 Ollama — AiHub 沒有這兩種端點,且換 embedding 需重建全部向量。</p>
              <p style={{ marginBottom: 0 }}>
                <strong>AiHub API Key:</strong> 只從後端 <code>.env</code> 的 <code>AIHUB_API_KEY</code> 讀取,
                不在此介面輸入或儲存(企業憑證不寫進資料庫)。修改後需重啟後端。
              </p>
            </div>
          }
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        {llmProviderConfig && !llmProviderConfig.aihub_api_key_set && (
          <Alert
            message="尚未設定 AIHUB_API_KEY"
            description={<span>要使用 AiHub 雲端模型,請在後端 <code>backend/.env</code> 加入 <code>AIHUB_API_KEY=...</code> 後重啟服務。未設定時只能選 Ollama。</span>}
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        <Form
          form={llmProviderForm}
          layout="vertical"
          onFinish={handleSaveLlmProvider}
          initialValues={{ llm_provider: "ollama" }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item label="文字模型後端" name="llm_provider">
                <Radio.Group size="small">
                  <Radio.Button value="ollama">Ollama(本地)</Radio.Button>
                  <Radio.Button value="aihub" disabled={!llmProviderConfig?.aihub_api_key_set}>
                    AiHub(雲端)
                  </Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Form.Item
                noStyle
                shouldUpdate={(prev, curr) => prev.llm_provider !== curr.llm_provider}
              >
                {({ getFieldValue }) => {
                  const isAihub = getFieldValue("llm_provider") === "aihub";
                  return (
                    <Form.Item
                      label="文字模型 ID"
                      name="llm_model"
                      extra={isAihub
                        ? "AiHub 模型代號,也可填 service/version(如 local/gpt-oss)"
                        : "可從下拉選或自行輸入。空 = 用 .env 預設"}
                    >
                      <AutoComplete
                        options={isAihub ? PRESET_LLM_AIHUB : PRESET_LLM_OLLAMA}
                        placeholder={isAihub ? "例:gpt-oss" : "例:gemma4:e2b"}
                        filterOption={filterOption}
                        allowClear
                      />
                    </Form.Item>
                  );
                }}
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item label="Embedding 後端">
                <Tag>Ollama(本地,固定)</Tag>
              </Form.Item>
              <Form.Item
                label="Embedding 模型 ID"
                name="embedding_model"
                extra="切換後舊向量與新模型不相容,需重跑向量化"
              >
                <AutoComplete
                  options={PRESET_EMBED_OLLAMA}
                  placeholder="例:bge-large-zh-v1.5"
                  filterOption={filterOption}
                  allowClear
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item label="VL 後端">
                <Tag>Ollama(本地,固定)</Tag>
              </Form.Item>
              <Form.Item
                label={<Space>VL 模型 ID <EyeOutlined /></Space>}
                name="vision_model"
                extra="圖片型 PDF 用。下拉內已含主流 vision 模型"
              >
                <AutoComplete
                  options={PRESET_VL_OLLAMA}
                  placeholder="例:gemma4:e2b"
                  filterOption={filterOption}
                  allowClear
                  popupMatchSelectWidth={420}
                />
              </Form.Item>
            </Col>
          </Row>
          <Divider />
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={savingLlm}>
                儲存設定
              </Button>
              <Button icon={<ExperimentOutlined />} onClick={handleTestLlmProvider} loading={testingLlm}>
                測試 LLM 連線
              </Button>
              <Button icon={<EyeOutlined />} onClick={handleTestVisionProvider} loading={testingLlm}>
                測試 VL 模型
              </Button>
              <Button onClick={() => llmProviderForm.resetFields()}>重置</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {/* OCR 引擎設定 */}
      <Card
        title={
          <span>
            <FileSearchOutlined style={{ marginRight: 8 }} />
            OCR 引擎設定
          </span>
        }
        style={{ marginBottom: 16 }}
      >
        <Alert
          message="圖片型 PDF 的文字／表格辨識引擎"
          description={
            <div>
              <p><strong>引擎:</strong> rapid = 輕量 onnx(RapidLayout+RapidTable+RapidOCR),快(~4s/頁)、無 paddle/segfault;pp_structure = PaddleOCR PP-StructureV3(重、保底)。</p>
              <p><strong>等級／版本:</strong> mobile+PP-OCRv4 快又乾淨(預設);server+PP-OCRv5 近乎完美但 CPU 上 ~87s/頁(GPU 才實用)。</p>
              <p style={{ marginBottom: 0 }}><strong>裝置:</strong> cpu 開發機用;gpu 需正式機裝 onnxruntime-gpu(如 5090)。設定只影響「之後新 OCR 的文件」,既有文件可用單份「高精度重跑」。</p>
            </div>
          }
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        <Form
          form={ocrForm}
          layout="vertical"
          onFinish={handleSaveOcrConfig}
          initialValues={{ engine: "rapid", model_tier: "mobile", version: "PP-OCRv4", device: "cpu" }}
        >
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item label="OCR 引擎" name="engine">
                <Radio.Group size="small">
                  <Radio.Button value="rapid">rapid</Radio.Button>
                  <Radio.Button value="pp_structure">pp_structure</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item label="模型等級" name="model_tier" extra="server 準但慢(CPU)">
                <Radio.Group size="small">
                  <Radio.Button value="mobile">mobile</Radio.Button>
                  <Radio.Button value="server">server</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item label="OCR 版本" name="version">
                <Radio.Group size="small">
                  <Radio.Button value="PP-OCRv4">v4</Radio.Button>
                  <Radio.Button value="PP-OCRv5">v5</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item label="裝置" name="device" extra="gpu 需 onnxruntime-gpu">
                <Radio.Group size="small">
                  <Radio.Button value="cpu">cpu</Radio.Button>
                  <Radio.Button value="gpu">gpu</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
          </Row>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={savingOcr}>
                儲存 OCR 設定
              </Button>
              <Button onClick={() => fetchOcrConfig()}>重置</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {/* 查詢參數配置（立即生效） */}
      <Card
        title={
          <span>
            <ThunderboltOutlined style={{ marginRight: 8 }} />
            查詢參數配置（立即生效）
          </span>
        }
        style={{ marginBottom: 16 }}
      >
        <Alert
          message="這些參數可以隨時調整，保存後立即生效，無需重新向量化文件"
          description={
            <div>
              <p><strong>適用場景：</strong>微調搜索效果、調整返回結果數量、過濾低相關結果</p>
              <p><strong>優點：</strong>調整方便，可以即時測試不同參數對搜索結果的影響</p>
            </div>
          }
          type="success"
          showIcon
          style={{ marginBottom: 16 }}
        />

        <Form
          form={queryForm}
          layout="vertical"
          onFinish={handleSaveQueryConfig}
          initialValues={{
            min_similarity_score: 0.3,
            default_top_k: 5,
            search_multiplier: 10,
          }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="向量匹配閾值"
                name="min_similarity_score"
                rules={[{ required: true, message: '請輸入閾值' }]}
                extra="低於此分數的結果將被過濾（0-1）。建議 0.2-0.4"
              >
                <InputNumber min={0} max={1} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>

            <Col span={8}>
              <Form.Item
                label="預設返回來源數"
                name="default_top_k"
                rules={[{ required: true, message: '請輸入數量' }]}
                extra="預設返回多少個相關來源。建議 3-10"
              >
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>

            <Col span={8}>
              <Form.Item
                label="搜索倍數"
                name="search_multiplier"
                rules={[{ required: true, message: '請輸入倍數' }]}
                extra="實際搜索數量 = 返回數 × 倍數。建議 5-15"
              >
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider />

          <Form.Item>
            <Space>
              <Button
                type="primary"
                htmlType="submit"
                icon={<SaveOutlined />}
                loading={savingQuery}
                size="large"
              >
                保存查詢參數
              </Button>
              <Button
                onClick={() => queryForm.resetFields()}
                size="large"
              >
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {/* 向量化參數配置（需重新向量化） */}
      <Card
        title={
          <span>
            <ToolOutlined style={{ marginRight: 8 }} />
            向量化參數配置（需重新向量化）
          </span>
        }
        style={{ marginBottom: 16 }}
      >
        <Alert
          message="修改這些參數後，必須刪除所有向量值並重新向量化所有文件才能生效"
          description={
            <div>
              <p><strong>適用場景：</strong>優化文本切塊方式、調整向量塊大小</p>
              <p><strong>注意：</strong>修改後需要執行以下步驟：</p>
              <ol style={{ marginBottom: 0 }}>
                <li>保存配置</li>
                <li>到下方「向量管理」點擊「刪除所有向量值」</li>
                <li>到各文件詳情頁點擊「重新向量化」按鈕</li>
              </ol>
            </div>
          }
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />

        <Form
          form={vectorForm}
          layout="vertical"
          onFinish={handleSaveVectorConfig}
          initialValues={{
            overlap_enabled: true,
            overlap_chars: 250,
            max_chars: 1800,
          }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="向量塊重疊 (Overlap)"
                name="overlap_enabled"
                valuePropName="checked"
              >
                <Switch
                  checkedChildren="啟用"
                  unCheckedChildren="停用"
                />
              </Form.Item>
              <Form.Item
                noStyle
                shouldUpdate={(prevValues, currentValues) =>
                  prevValues.overlap_enabled !== currentValues.overlap_enabled
                }
              >
                {({ getFieldValue }) =>
                  getFieldValue('overlap_enabled') ? (
                    <Form.Item
                      label="重疊字符數"
                      name="overlap_chars"
                      rules={[{ required: true, message: '請輸入重疊字符數' }]}
                      extra="建議 200-300 字。重疊可以避免重要資訊被切斷。"
                    >
                      <InputNumber min={0} max={500} style={{ width: '100%' }} />
                    </Form.Item>
                  ) : (
                    <Alert
                      message="已停用重疊功能"
                      description="向量塊之間不會有重疊內容，可能導致跨段落資訊遺失"
                      type="warning"
                      showIcon
                      style={{ marginBottom: 16 }}
                    />
                  )
                }
              </Form.Item>
            </Col>

            <Col span={12}>
              <Form.Item
                label="向量塊最大字符數"
                name="max_chars"
                rules={[{ required: true, message: '請輸入最大字符數' }]}
                extra="每個向量塊的最大長度。建議 1500-2000 字。"
              >
                <InputNumber min={500} max={3000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider />

          <Form.Item>
            <Space>
              <Button
                type="primary"
                htmlType="submit"
                icon={<SaveOutlined />}
                loading={savingVector}
                size="large"
              >
                保存向量化參數
              </Button>
              <Button
                onClick={() => vectorForm.resetFields()}
                size="large"
              >
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {/* 向量管理 */}
      <Card
        title={
          <span>
            <ThunderboltOutlined style={{ marginRight: 8 }} />
            LLM 併發控制（立即生效）
          </span>
        }
        style={{ marginBottom: 16 }}
      >
        <Alert
          message="小顯存機器請保持「1（序列化）」"
          description={
            <div>
              <p>
                兩個生成請求同時進行時，Ollama 會改變模型的 GPU/CPU 層切分。浮點運算路徑一變，
                第一個 token 的選擇就可能翻轉，整段答案隨之發散 ——
                <strong>即使溫度與亂數種子都固定也一樣</strong>，因為輸入雖同、運算路徑已不同。
              </p>
              <p>
                <strong>實測後果不是「偶爾答錯」，而是「掉進另一個穩定點並持續」：</strong>
                同一個問題原本回 1,435 字含 3 張表格，被並行干擾後變成 855 字、0 張表格，
                之後每次都一樣，看起來像系統本來就是那樣。必須重啟服務並卸載模型才會恢復。
              </p>
              <p>
                序列化的代價是併發時要排隊，但小顯存機器本來就沒有真正的併發能力 ——
                兩個請求並行只會一起變慢又互相污染。<strong>換用能讓模型完全常駐 GPU
                （不溢位到 CPU）的顯卡之後，才建議調高。</strong>
              </p>
            </div>
          }
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />

        <Form
          form={concurrencyForm}
          layout="vertical"
          onFinish={handleSaveLlmConcurrency}
          initialValues={{ max_concurrency: 1 }}
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="同時允許的 LLM 請求數"
                name="max_concurrency"
                rules={[{ required: true, message: '請輸入 1 到 8 的整數' }]}
                extra="1 = 完全序列化（建議）。超過的請求會排隊等待，不會失敗。"
              >
                <InputNumber min={1} max={8} step={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Space>
            <Button
              type="primary"
              htmlType="submit"
              icon={<SaveOutlined />}
              loading={savingConcurrency}
            >
              儲存
            </Button>
            <Button
              danger
              icon={<WarningOutlined />}
              loading={resettingInference}
              onClick={handleResetInference}
            >
              重置推論引擎
            </Button>
          </Space>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              答案品質突然變樣（完整表格塌成清單）時按「重置推論引擎」：卸載所有模型強制乾淨重載，
              下一次查詢約多等 15~30 秒。
            </Text>
          </div>
        </Form>
      </Card>

      <Card
        title={
          <span>
            <NodeIndexOutlined style={{ marginRight: 8 }} />
            介面顯示
          </span>
        }
        style={{ marginBottom: 16 }}
      >
        <Space align="center">
          <Switch
            checked={showKg}
            loading={savingUiConfig}
            onChange={handleToggleKg}
          />
          <Text>側邊欄顯示「知識圖譜」頁面</Text>
        </Space>
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            知識圖譜針對規範類文件（ISO / IEC / MIL-STD / IEEE…）自動抽取引用與取代關係；
            一般商管或教育訓練文件抽不出內容，圖譜會是空的。隱藏只收起入口 ——
            <strong>抽取仍在每次文件匯入後自動執行、資料持續累積</strong>，
            日後匯入規範文件再打開，全部看得到。
          </Text>
        </div>
      </Card>

      <Card title="向量管理">
        <Alert
          message="刪除所有向量值說明"
          description={
            <div>
              <p>刪除所有向量值將保留所有文件和文本內容，只刪除向量數據（embeddings）和 FAISS 索引。</p>
              <p><strong>何時需要刪除向量值：</strong></p>
              <ul style={{ marginBottom: 0 }}>
                <li>更改了 embedding 模型（例如從 bge-large-zh 換成 qwen3-embedding）</li>
                <li>修改了向量處理參數（overlap、max_chars 等）</li>
                <li>FAISS 索引損壞或向量維度不匹配</li>
                <li>想要重新開始建立向量索引</li>
              </ul>
              <p style={{ marginTop: 8 }}>
                <strong>刪除後：</strong>可以使用各文件詳情頁的「重新向量化」按鈕來重建向量（無需重新上傳 PDF）。
              </p>
            </div>
          }
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />

        <Button
          type="primary"
          danger
          icon={<DeleteOutlined />}
          onClick={handleClearVectors}
          loading={clearing}
          size="large"
        >
          刪除所有向量值
        </Button>
      </Card>
    </div>
  );
};

export default SystemSettings;
