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
  const [form] = Form.useForm();

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

      <Form form={form} layout="vertical" onFinish={doPreview}>
        <Space wrap align="start" style={{ width: "100%" }}>
          <Form.Item label="文件" style={{ minWidth: 320 }}>
            <Select
              showSearch optionFilterProp="label" placeholder="先按右上角載入文件清單"
              value={docId} onChange={loadTables} loading={loading} allowClear
              options={docs.map((d) => ({ value: d.id, label: d.title }))}
            />
          </Form.Item>
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
