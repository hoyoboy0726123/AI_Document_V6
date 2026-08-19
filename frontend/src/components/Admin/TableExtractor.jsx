import { useState } from "react";
import {
  Alert, Button, Card, Empty, Form, Input, Modal, Select, Space, Table, Tag, Typography, message,
} from "antd";
import { ExperimentOutlined, NodeIndexOutlined, UndoOutlined } from "@ant-design/icons";
import apiClient from "../../services/api";

const { Text, Paragraph } = Typography;

/**
 * 表格關係抽取：把文件裡的 markdown 表格逐列變成知識圖譜節點。
 *
 * 流程刻意是「偵測 → 乾跑預覽 → 確認才寫入 → 可整批回退」。抽取器寫錯會
 * 靜默污染整張圖，而「看不到結果就上線」是這個專案吃過最多虧的模式。
 *
 * 效益提醒（實測，不是猜測）：教育訓練文件（10 塊）上做過 A/B —— 有 KG 與
 * 無 KG 在 13 題 Unit0 題組與 6 題多跳題組上「逐題分數完全相同」。原因是
 * 小文件的表格會一起進 context，LLM 自己就完成 join。這個功能對「大到裝不
 * 進 context 的表格」才有意義，所以預設不啟用、由使用者自行判斷。
 */
const TableExtractor = () => {
  const [docs, setDocs] = useState([]);
  const [docId, setDocId] = useState(null);
  const [tables, setTables] = useState([]);
  const [loading, setLoading] = useState(false);
  const [plan, setPlan] = useState(null);
  const [applying, setApplying] = useState(false);
  const [autoModel, setAutoModel] = useState("");
  const [autoLoading, setAutoLoading] = useState(false);
  const [autoResult, setAutoResult] = useState(null);
  const [form] = Form.useForm();

  // AI 自動建議：LLM 判斷每張表值不值得抽、母節點與欄位；規則引擎乾跑驗證。
  // 大模型逐表分析，一份文件可能要幾分鐘 —— 按鈕上有講，不做輪詢花俏功能。
  const doAutoSuggest = async () => {
    if (!docId) return;
    setAutoLoading(true); setAutoResult(null);
    try {
      const r = await apiClient.post(`kg/tables/${docId}/auto-suggest`,
        autoModel ? { model: autoModel } : {}, { timeout: 900000 });
      setAutoResult(r.data);
    } catch (e) {
      message.error(e.response?.data?.detail ?? "AI 建議失敗");
    } finally {
      setAutoLoading(false);
    }
  };

  const doAutoApply = async () => {
    if (!docId) return;
    setAutoLoading(true);
    try {
      const r = await apiClient.post(`kg/tables/${docId}/auto-apply`,
        autoModel ? { model: autoModel } : {}, { timeout: 900000 });
      const names = (r.data.applied ?? []).map((a) => `「${a.primary_name}」×${a.n_contains_total}`);
      message.success(names.length ? `已建立：${names.join("、")}` : "沒有可自動套用的表格");
      setAutoResult(null);
    } catch (e) {
      message.error(e.response?.data?.detail ?? "自動套用失敗");
    } finally {
      setAutoLoading(false);
    }
  };

  const loadDocs = async () => {
    try {
      const r = await apiClient.get("documents/", { params: { page: 1, page_size: 200 } });
      setDocs(r.data?.items ?? []);
    } catch {
      message.error("讀取文件清單失敗");
    }
  };

  const loadTables = async (id) => {
    setDocId(id); setTables([]); setPlan(null);
    if (!id) return;
    setLoading(true);
    try {
      const r = await apiClient.get(`kg/tables/${id}`);
      setTables(r.data?.tables ?? []);
      if (!(r.data?.tables ?? []).length) message.info("這份文件沒有偵測到可抽取的表格");
    } catch {
      message.error("偵測表格失敗");
    } finally {
      setLoading(false);
    }
  };

  const buildSpec = (values) => ({
    table_key: values.table_key,
    parent_name: (values.parent_name || "").trim(),
    entity_type: values.entity_type || "term",
    id_col: Number(values.id_col),
    label_col: Number(values.label_col),
  });

  const doPreview = async (values) => {
    setPlan(null);
    try {
      const r = await apiClient.post(`kg/tables/${docId}/preview`, buildSpec(values));
      setPlan(r.data);
    } catch (e) {
      message.error(e.response?.data?.detail ?? "預覽失敗");
    }
  };

  const doApply = async () => {
    const values = await form.validateFields();
    setApplying(true);
    try {
      const r = await apiClient.post(`kg/tables/${docId}/apply`, buildSpec(values));
      message.success(`已寫入：${r.data.n_entities} 個節點 / ${r.data.n_relations} 條關係`);
      setPlan(null);
    } catch (e) {
      message.error(e.response?.data?.detail ?? "寫入失敗");
    } finally {
      setApplying(false);
    }
  };

  const doRemove = () => {
    const name = (form.getFieldValue("parent_name") || "").trim();
    if (!name) { message.warning("請先填母節點名稱"); return; }
    Modal.confirm({
      title: `回退「${name}」的抽取結果？`,
      content: "會刪除該母節點與它底下所有子節點及關係。原始文件與向量不受影響。",
      okText: "回退", okButtonProps: { danger: true }, cancelText: "取消",
      onOk: async () => {
        try {
          const r = await apiClient.delete(`kg/tables/extraction/${encodeURIComponent(name)}`);
          message.success(`已回退：刪除 ${r.data.n_entities} 節點 / ${r.data.n_relations} 關係`);
        } catch (e) {
          message.error(e.response?.data?.detail ?? "回退失敗");
        }
      },
    });
  };

  const selectedTable = tables.find((t) => t.key === form.getFieldValue("table_key"));
  const colOptions = selectedTable
    ? Array.from({ length: selectedTable.n_cols }, (_, i) => ({
        value: i,
        label: `第 ${i + 1} 欄${selectedTable.headers?.[i] ? `（${selectedTable.headers[i]}）` : ""}`,
      }))
    : [];

  return (
    <Card
      title={<Space><NodeIndexOutlined />表格關係抽取（知識圖譜）</Space>}
      extra={<Button size="small" onClick={loadDocs}>載入文件清單</Button>}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="把表格逐列變成知識圖譜的節點與關係"
        description={
          <div>
            <Paragraph style={{ marginBottom: 6 }}>
              指定「哪張表、哪一欄是代號、哪一欄是名稱」，每一列會建成一個節點並掛在母節點底下
              （<code>contains</code> / <code>part_of</code>），Agent 的列舉工具即可完整列出。
            </Paragraph>
            <Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 13 }}>
              <strong>效益提醒：</strong>在 10 塊的小文件上實測，有無此功能的問答分數
              <strong>逐題完全相同</strong> —— 小表格本來就會整張進入檢索脈絡。
              它對「大到裝不進脈絡的表格」與「需要精確計數」才有價值，請先用預覽評估。
            </Paragraph>
          </div>
        }
      />

      <Card size="small" type="inner" style={{ marginBottom: 16, background: "#f6ffed" }}
            title={<Text strong>步驟 1：選擇要處理的文件（三種抽取方式共用）</Text>}>
        <Space wrap>
          <Select
            showSearch optionFilterProp="label" placeholder="先按右上角「載入文件清單」再選"
            value={docId} onChange={loadTables} loading={loading} allowClear
            style={{ minWidth: 380 }}
            options={docs.map((d) => ({ value: d.id, label: d.title }))}
          />
          {!docs.length && <Button size="small" onClick={loadDocs}>載入文件清單</Button>}
          {docId && <Tag color="green">已選定，下方三種方式皆對此文件操作</Tag>}
        </Space>
      </Card>

      <Card size="small" type="inner" style={{ marginBottom: 16 }}
            title={<Text strong>圖譜維護（本文件）</Text>}>
        <Space wrap>
          <Button danger disabled={!docId} onClick={() => {
            Modal.confirm({
              title: "刪除這份文件的全部圖譜關係？",
              content: "包含 regex 抽取、表格抽取、LLM 引用抽取建立的所有關係邊。規範實體（節點）保留 —— 它們跨文件共享。原始文件與向量不受影響。",
              okText: "刪除", okButtonProps: { danger: true }, cancelText: "取消",
              onOk: async () => {
                try {
                  await apiClient.delete(`kg/document/${docId}/relations`);
                  message.success("已刪除此文件的圖譜關係");
                } catch (e) {
                  message.error(e.response?.data?.detail ?? "刪除失敗");
                }
              },
            });
          }}>
            刪除此文件的圖譜關係
          </Button>
          <Button disabled={!docId} onClick={async () => {
            try {
              const r = await apiClient.post(`kg/extract/${docId}`);
              message.success(r.data.deduplicated ? "已有進行中的抽取任務" : "背景重跑已啟動（regex 結構＋引用），進度見任務橫幅");
            } catch (e) {
              message.error(e.response?.data?.detail ?? "啟動失敗");
            }
          }}>
            重跑 regex 抽取（背景）
          </Button>
          <Text type="secondary" style={{ fontSize: 12 }}>
            重跑會先清掉本文件既有關係再重建；表格/LLM 抽取的結果需在下方各自重做
          </Text>
        </Space>
      </Card>

      <Card size="small" type="inner" style={{ marginBottom: 16 }}
            title={<Space><ExperimentOutlined />AI 自動建議（零手動設定）</Space>}>
        <Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 8 }}>
          由本地 LLM 逐表判斷「值不值得抽、母節點叫什麼、哪欄是代號」，規則引擎逐列建節點並驗證；
          被 OCR 切散的同批表格會自動合併成一個母節點（其餘命名存為別名）。大模型逐表分析，
          一份文件約需 2~5 分鐘。模型欄：留空＝跟隨系統文字模型；填 Ollama tag（建議
          qwen3.8:27b，判斷力實測最佳）或 aihub / aihub:gpt-oss 走雲端 —— 雲端建議品質
          實測較弱，套用前請先用「產生建議」人工確認。
        </Paragraph>
        <Space wrap>
          <Input placeholder="留空＝跟隨系統文字模型；或填 ollama tag / aihub" value={autoModel}
                 onChange={(e) => setAutoModel(e.target.value)} style={{ width: 300 }} />
          <Button onClick={doAutoSuggest} loading={autoLoading} disabled={!docId}>
            產生建議（不寫入）
          </Button>
          <Button type="primary" onClick={doAutoApply} loading={autoLoading} disabled={!docId}>
            自動建議並套用
          </Button>
        </Space>
        {autoResult && (
          <div style={{ marginTop: 12 }}>
            {(autoResult.groups ?? []).map((g, i) => (
              <div key={i} style={{ fontSize: 13, marginBottom: 4 }}>
                <Tag color={g.verdict === "auto" ? "green" : "orange"}>{g.verdict}</Tag>
                <Text strong>{g.primary_name}</Text>
                {g.aliases?.length > 0 && <Text type="secondary">（別名：{g.aliases.join("、")}）</Text>}
                <Text type="secondary" style={{ marginLeft: 8 }}>{g.n_codes_union} 個項目，來自 {g.member_keys.length} 張表</Text>
              </div>
            ))}
            {(autoResult.suggestions ?? []).filter((s) => s.verdict === "skip").length > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                另有 {(autoResult.suggestions ?? []).filter((s) => s.verdict === "skip").length} 張表被判定不值得抽（排版表/清單快照等）
              </Text>
            )}
          </div>
        )}
      </Card>

      <Card size="small" type="inner" style={{ marginBottom: 16 }}
            title={<Space><NodeIndexOutlined />散文引用抽取（LLM，全書背景執行）</Space>}>
        <Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 8 }}>
          由 LLM 逐塊找出內文引用的外部規範編號（STANAG、AECTP、TOP 等 regex 不認得的格式也抽得到），
          每一筆都回原文字面查核，查核不過不入圖。大文件需數十分鐘，故入庫時不自動執行，
          由管理者在此手動啟動；可整批回退。實測 METHOD 510.7 一章：regex 5 個 → LLM 11 個，零幻覺。
        </Paragraph>
        <Space wrap>
          <Button onClick={async () => {
            if (!docId) return;
            setAutoLoading(true);
            try {
              const r = await apiClient.post(`kg/citations/${docId}`,
                autoModel ? { model: autoModel } : {});
              message.success(r.data.deduplicated ? "已有進行中的抽取任務" : "背景抽取已啟動，進度見上方任務橫幅");
            } catch (e) {
              message.error(e.response?.data?.detail ?? "啟動失敗");
            } finally { setAutoLoading(false); }
          }} loading={autoLoading} disabled={!docId} type="primary">
            開始全書引用抽取（背景）
          </Button>
          <Button danger onClick={() => {
            if (!docId) return;
            Modal.confirm({
              title: "回退這份文件的 LLM 引用抽取？",
              content: "刪除本功能建立的所有引用邊；regex 抽的與規範實體不受影響。",
              okText: "回退", okButtonProps: { danger: true }, cancelText: "取消",
              onOk: async () => {
                try {
                  const r = await apiClient.delete(`kg/citations/${docId}`);
                  message.success(`已回退 ${r.data.n_relations} 條引用邊`);
                } catch (e) {
                  message.error(e.response?.data?.detail ?? "回退失敗");
                }
              },
            });
          }} disabled={!docId}>
            回退
          </Button>
          <Text type="secondary" style={{ fontSize: 12 }}>模型沿用上方欄位（留空＝系統預設）</Text>
        </Space>
      </Card>

      <Form form={form} layout="vertical" onFinish={doPreview}>
        <Space wrap align="start" style={{ width: "100%" }}>
          <Form.Item label="表格" name="table_key" rules={[{ required: true, message: "請選表格" }]} style={{ minWidth: 340 }}>
            <Select
              placeholder={tables.length ? "選擇要抽取的表格" : "（選了文件才會列出）"}
              onChange={() => { form.setFieldsValue({ id_col: undefined, label_col: undefined }); setPlan(null); }}
              options={tables.map((t) => ({
                value: t.key,
                label: `頁${t.page}｜${t.n_cols}欄×${t.n_rows}列｜${(t.headers || []).slice(0, 3).join(" / ") || "(無表頭)"}`,
              }))}
            />
          </Form.Item>
        </Space>

        {selectedTable && (
          <Table
            size="small" bordered pagination={false} style={{ marginBottom: 16 }}
            title={() => <Text type="secondary">前 {selectedTable.sample_rows.length} 列樣本</Text>}
            dataSource={selectedTable.sample_rows.map((r, i) => ({ key: i, ...Object.fromEntries(r.map((c, j) => [`c${j}`, c])) }))}
            columns={Array.from({ length: selectedTable.n_cols }, (_, i) => ({
              title: `第${i + 1}欄${selectedTable.headers?.[i] ? `：${selectedTable.headers[i]}` : ""}`,
              dataIndex: `c${i}`, ellipsis: true,
            }))}
          />
        )}

        <Space wrap align="start">
          <Form.Item label="母節點名稱" name="parent_name" rules={[{ required: true, message: "必填" }]}
                     extra="子項目掛在它底下，也是回退時的識別名" style={{ minWidth: 220 }}>
            <Input placeholder="例：料件大類" />
          </Form.Item>
          <Form.Item label="代號欄" name="id_col" rules={[{ required: true, message: "必選" }]} style={{ minWidth: 220 }}>
            <Select options={colOptions} placeholder="當作節點識別碼" />
          </Form.Item>
          <Form.Item label="名稱欄" name="label_col" rules={[{ required: true, message: "必選" }]} style={{ minWidth: 220 }}>
            <Select options={colOptions} placeholder="當作節點名稱" />
          </Form.Item>
          <Form.Item label="節點型別" name="entity_type" initialValue="term" style={{ minWidth: 150 }}>
            <Input placeholder="term" />
          </Form.Item>
        </Space>

        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" icon={<ExperimentOutlined />} disabled={!docId}>
              預覽（不寫入）
            </Button>
            <Button onClick={doApply} disabled={!plan || !plan.n_entities} loading={applying}>
              確認寫入
            </Button>
            <Button danger icon={<UndoOutlined />} onClick={doRemove}>回退這個母節點</Button>
          </Space>
        </Form.Item>
      </Form>

      {plan && (
        <Card size="small" type="inner" title={
          <Space>
            <Text strong>預覽結果</Text>
            <Tag color="blue">{plan.n_entities} 節點</Tag>
            <Tag color="blue">{plan.n_relations} 關係</Tag>
            {plan.skipped?.length > 0 && <Tag color="orange">略過 {plan.skipped.length} 列</Tag>}
          </Space>
        }>
          {plan.n_entities === 0 ? (
            <Empty description="這組欄位抽不出任何節點，請換欄位再試" />
          ) : (
            <Table
              size="small" pagination={{ pageSize: 8, size: "small" }}
              dataSource={plan.entities.map((e, i) => ({ key: i, ...e }))}
              columns={[
                { title: "代號", dataIndex: "code", width: 110 },
                { title: "名稱", dataIndex: "name" },
              ]}
            />
          )}
          {plan.skipped?.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", fontSize: 13, color: "#888" }}>
                略過的 {plan.skipped.length} 列（空值、表頭殘留或重複代號）
              </summary>
              <ul style={{ fontSize: 12, color: "#888", marginTop: 6 }}>
                {plan.skipped.slice(0, 20).map((s, i) => <li key={i}>{s.row} — {s.why}</li>)}
              </ul>
            </details>
          )}
        </Card>
      )}
    </Card>
  );
};

export default TableExtractor;
