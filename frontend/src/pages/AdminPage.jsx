import { useEffect, useState } from "react";
import {
  AppstoreOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  RollbackOutlined,
  SaveOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import AppLayout from "../components/Layout/AppLayout";
import ClassificationManagement from "../components/Admin/ClassificationManagement";
import SystemSettings from "../components/Admin/SystemSettings";
import TableExtractor from "../components/Admin/TableExtractor";
import apiClient from "../services/api";

const fieldTypeOptions = [
  { label: "單行文字", value: "text" },
  { label: "多行文字", value: "textarea" },
  { label: "數字", value: "number" },
  { label: "日期", value: "date" },
  { label: "下拉選單", value: "select" },
  { label: "多選下拉", value: "multi_select" },
];

const AdminPage = () => {
  const [fields, setFields] = useState([]);
  const [loading, setLoading] = useState(false);
  const [fieldModalVisible, setFieldModalVisible] = useState(false);
  const [fieldModalMode, setFieldModalMode] = useState("create");
  const [currentField, setCurrentField] = useState(null);
  const [fieldForm] = Form.useForm();
  const [optionModalVisible, setOptionModalVisible] = useState(false);
  const [optionForm] = Form.useForm();
  const [editingOption, setEditingOption] = useState(null);
  const [optionEditForm] = Form.useForm();

  // ── RAG Prompt ──
  const [ragPrompt, setRagPrompt] = useState(null);     // { system_prompt, user_template, is_default }
  const [ragSaving, setRagSaving] = useState(false);
  const [ragResetting, setRagResetting] = useState(false);
  const [ragSystemPrompt, setRagSystemPrompt] = useState("");
  const [ragUserTemplate, setRagUserTemplate] = useState("");

  const fetchRagPrompt = async () => {
    try {
      const resp = await apiClient.get("admin/rag-prompt");
      setRagPrompt(resp.data);
      setRagSystemPrompt(resp.data.system_prompt);
      setRagUserTemplate(resp.data.user_template);
    } catch {
      message.error("載入 RAG 提示詞失敗");
    }
  };

  const handleRagSave = async () => {
    setRagSaving(true);
    try {
      const resp = await apiClient.put("admin/rag-prompt", {
        system_prompt: ragSystemPrompt,
        user_template: ragUserTemplate,
      });
      setRagPrompt(resp.data);
      message.success("RAG 提示詞已儲存");
    } catch (err) {
      message.error(err.response?.data?.detail ?? "儲存失敗");
    } finally {
      setRagSaving(false);
    }
  };

  const handleRagReset = async () => {
    Modal.confirm({
      title: "確認載入預設提示詞？",
      content: "將捨棄目前的自訂提示詞，恢復為系統預設值。RAG 查詢行為將與原始版本完全相同。",
      okText: "確認重置",
      cancelText: "取消",
      onOk: async () => {
        setRagResetting(true);
        try {
          await apiClient.delete("admin/rag-prompt");
          await fetchRagPrompt();
          message.success("已恢復為預設提示詞");
        } catch {
          message.error("重置失敗");
        } finally {
          setRagResetting(false);
        }
      },
    });
  };

  const fetchFields = async () => {
    try {
      setLoading(true);
      const resp = await apiClient.get("admin/metadata-fields");
      setFields(resp.data);
    } catch (error) {
      message.error(error.response?.data?.detail ?? "載入元數據欄位失敗");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchFields();
    fetchRagPrompt();
  }, []);

  useEffect(() => {
    if (currentField) {
      const updated = fields.find((f) => f.id === currentField.id);
      if (updated && updated !== currentField) {
        setCurrentField(updated);
      }
    }
  }, [fields, currentField]);

  const openCreateField = () => {
    setFieldModalMode("create");
    setCurrentField(null);
    fieldForm.resetFields();
    fieldForm.setFieldsValue({ is_required: false, is_active: true, order_index: 0 });
    setFieldModalVisible(true);
  };

  const openEditField = (field) => {
    setFieldModalMode("edit");
    setCurrentField(field);
    fieldForm.setFieldsValue({
      name: field.name,
      display_name: field.display_name,
      field_type: field.field_type,
      is_required: field.is_required,
      is_active: field.is_active,
      order_index: field.order_index,
      description: field.description,
    });
    setFieldModalVisible(true);
  };

  const handleFieldSubmit = async () => {
    try {
      const values = await fieldForm.validateFields();
      if (fieldModalMode === "create") {
        await apiClient.post("admin/metadata-fields/", {
          name: values.name,
          display_name: values.display_name,
          field_type: values.field_type,
          is_required: values.is_required ?? false,
          order_index: values.order_index ?? 0,
          description: values.description ?? null,
        });
        message.success("欄位已建立");
      } else if (currentField) {
        await apiClient.put(`admin/metadata-fields/${currentField.id}/`, {
          display_name: values.display_name,
          field_type: values.field_type,
          is_required: values.is_required,
          is_active: values.is_active,
          order_index: values.order_index,
          description: values.description ?? null,
        });
        message.success("欄位已更新");
      }
      setFieldModalVisible(false);
      fetchFields();
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error.response?.data?.detail ?? "儲存欄位失敗");
    }
  };

  const handleDeleteField = (field) => {
    Modal.confirm({
      title: `刪除欄位 ${field.display_name}`,
      content: "刪除後將無法復原，仍要繼續嗎？",
      okText: "刪除",
      okType: "danger",
      cancelText: "取消",
      async onOk() {
        try {
          await apiClient.delete(`admin/metadata-fields/${field.id}/`);
          message.success("欄位已刪除");
          fetchFields();
        } catch (error) {
          message.error(error.response?.data?.detail ?? "刪除欄位失敗");
        }
      },
    });
  };

  const openOptionModal = (field) => {
    setCurrentField(field);
    optionForm.resetFields();
    setOptionModalVisible(true);
  };

  const handleAddOption = async () => {
    if (!currentField) return;
    try {
      const values = await optionForm.validateFields();
      await apiClient.post(`admin/metadata-fields/${currentField.id}/options/`, {
        value: values.value,
        display_value: values.display_value,
        order_index: values.order_index ?? 0,
      });
      message.success("選項已新增");
      optionForm.resetFields();
      fetchFields();
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error.response?.data?.detail ?? "新增選項失敗");
    }
  };

  const handleToggleOption = async (option) => {
    try {
      await apiClient.put(`admin/metadata-fields/options/${option.id}/`, {
        is_active: !option.is_active,
      });
      fetchFields();
    } catch (error) {
      message.error(error.response?.data?.detail ?? "更新選項狀態失敗");
    }
  };

  const handleDeleteOption = (option) => {
    Modal.confirm({
      title: `刪除選項 ${option.display_value}`,
      okText: "刪除",
      okType: "danger",
      cancelText: "取消",
      async onOk() {
        try {
          await apiClient.delete(`admin/metadata-fields/options/${option.id}/`);
          message.success("選項已刪除");
          fetchFields();
        } catch (error) {
          message.error(error.response?.data?.detail ?? "刪除選項失敗");
        }
      },
    });
  };

  const openEditOption = (option) => {
    setEditingOption(option);
    optionEditForm.setFieldsValue({
      display_value: option.display_value,
      order_index: option.order_index,
      is_active: option.is_active,
    });
  };

  const handleSubmitOptionEdit = async () => {
    if (!editingOption) return;
    try {
      const values = await optionEditForm.validateFields();
      await apiClient.put(`admin/metadata-fields/options/${editingOption.id}/`, {
        display_value: values.display_value,
        order_index: values.order_index,
        is_active: values.is_active,
      });
      message.success("選項已更新");
      setEditingOption(null);
      fetchFields();
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error.response?.data?.detail ?? "更新選項失敗");
    }
  };

  const columns = [
    {
      title: "顯示名稱",
      dataIndex: "display_name",
      key: "display_name",
      render: (text) => <strong>{text}</strong>,
    },
    {
      title: "欄位代碼",
      dataIndex: "name",
      key: "name",
    },
    {
      title: "欄位類型",
      dataIndex: "field_type",
      key: "field_type",
      render: (value) => fieldTypeOptions.find((item) => item.value === value)?.label ?? value,
    },
    {
      title: "必填",
      dataIndex: "is_required",
      key: "is_required",
      render: (value) => (value ? <Tag color="red">必填</Tag> : <Tag>選填</Tag>),
    },
    {
      title: "狀態",
      dataIndex: "is_active",
      key: "is_active",
      render: (value) =>
        value ? (
          <Tag color="green" icon={<CheckCircleOutlined />}>啟用中</Tag>
        ) : (
          <Tag icon={<CloseCircleOutlined />}>已停用</Tag>
        ),
    },
    {
      title: "排序",
      dataIndex: "order_index",
      key: "order_index",
      width: 80,
    },
    {
      title: "操作",
      key: "actions",
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => openEditField(record)}>
            編輯
          </Button>
          {(record.field_type === "select" || record.field_type === "multi_select") && (
            <Button size="small" icon={<SettingOutlined />} onClick={() => openOptionModal(record)}>
              管理選項
            </Button>
          )}
          <Button size="small" danger onClick={() => handleDeleteField(record)}>
            刪除
          </Button>
        </Space>
      ),
    },
  ];

  const tabItems = [
    {
      key: 'metadata',
      label: '元數據欄位管理',
      children: (
        <Card
          title="元數據欄位管理"
          extra={
            <Button type="primary" icon={<AppstoreOutlined />} onClick={openCreateField}>
              新增欄位
            </Button>
          }
        >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={fields}
          columns={columns}
          pagination={false}
          expandable={{
            expandedRowRender: (record) => (
              <div style={{ paddingLeft: 16 }}>
                <strong>欄位說明：</strong> {record.description || "—"}
                {record.options?.length ? (
                  <div style={{ marginTop: 8 }}>
                    <strong>選項：</strong>
                    <Space wrap style={{ marginTop: 8 }}>
                      {record.options.map((opt) => (
                        <Tag key={opt.id} color={opt.is_active ? "blue" : undefined}>
                          {opt.display_value}
                        </Tag>
                      ))}
                    </Space>
                  </div>
                ) : null}
              </div>
            ),
          }}
        />
        </Card>
      ),
    },
    {
      key: 'classifications',
      label: '分類管理',
      children: <ClassificationManagement />,
    },
    {
      key: 'system',
      label: '系統設置',
      children: <SystemSettings />,
    },
    {
      key: 'kg-tables',
      label: '表格關係抽取',
      children: <TableExtractor />,
    },
    {
      key: 'rag-prompt',
      label: 'RAG 提示詞',
      children: (
        <Card>
          <Space direction="vertical" style={{ width: "100%" }} size="large">
            {ragPrompt?.is_default === false && (
              <Alert
                type="info"
                showIcon
                message="目前使用自訂提示詞"
                description="RAG 查詢將使用下方已儲存的自訂提示詞。點擊「載入預設」可完整恢復原始行為。"
              />
            )}
            {ragPrompt?.is_default === true && (
              <Alert
                type="success"
                showIcon
                message="目前使用系統預設提示詞"
                description="尚未設定自訂提示詞，RAG 查詢行為與原始版本完全一致。"
              />
            )}

            <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 6 }}>
                系統提示詞（System Prompt）
              </Typography.Text>
              <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8, fontSize: 12 }}>
                定義 LLM 的角色與語言要求（如：繁體中文、禁止簡體等）
              </Typography.Text>
              <Input.TextArea
                rows={3}
                value={ragSystemPrompt}
                onChange={(e) => setRagSystemPrompt(e.target.value)}
              />
            </div>

            <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 6 }}>
                查詢提示詞模板（User Prompt Template）
              </Typography.Text>
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message="模板中必須包含以下佔位符（否則無法儲存）"
                description={
                  <span>
                    <code>{"{{question}}"}</code> — 使用者問題
                    <code>{"{{context}}"}</code> — 向量段落內容
                    <code>{"{{history}}"}</code> — 對話歷史（可留空）
                  </span>
                }
              />
              <Input.TextArea
                rows={18}
                value={ragUserTemplate}
                onChange={(e) => setRagUserTemplate(e.target.value)}
                style={{ fontFamily: "monospace", fontSize: 13 }}
              />
            </div>

            <Space>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={ragSaving}
                onClick={handleRagSave}
              >
                儲存提示詞
              </Button>
              <Button
                icon={<RollbackOutlined />}
                loading={ragResetting}
                onClick={handleRagReset}
              >
                載入預設
              </Button>
            </Space>
          </Space>
        </Card>
      ),
    },
  ];

  return (
    <AppLayout>
      <Tabs defaultActiveKey="metadata" items={tabItems} />

      <Modal
        title={fieldModalMode === "create" ? "新增欄位" : "編輯欄位"}
        open={fieldModalVisible}
        onCancel={() => setFieldModalVisible(false)}
        onOk={handleFieldSubmit}
        destroyOnHidden
      >
        <Form layout="vertical" form={fieldForm}>
          <Form.Item
            name="name"
            label="欄位代碼"
            rules={[{ required: true, message: "請輸入欄位代碼" }]}
          >
            <Input placeholder="只可輸入英文、數字或底線" disabled={fieldModalMode === "edit"} />
          </Form.Item>
          <Form.Item
            name="display_name"
            label="顯示名稱"
            rules={[{ required: true, message: "請輸入顯示名稱" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="field_type"
            label="欄位類型"
            rules={[{ required: true, message: "請選擇欄位類型" }]}
          >
            <Select options={fieldTypeOptions} />
          </Form.Item>
          <Form.Item name="is_required" label="是否必填" valuePropName="checked">
            <Switch />
          </Form.Item>
          {fieldModalMode === "edit" && (
            <Form.Item name="is_active" label="是否啟用" valuePropName="checked">
              <Switch />
            </Form.Item>
          )}
          <Form.Item name="order_index" label="排序">
            <Input type="number" placeholder="0" />
          </Form.Item>
          <Form.Item name="description" label="欄位說明">
            <Input.TextArea rows={3} placeholder="可填寫欄位用途或限制" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={currentField ? `管理選項 - ${currentField.display_name}` : "管理選項"}
        open={optionModalVisible}
        onCancel={() => setOptionModalVisible(false)}
        footer={null}
        destroyOnHidden
        width={640}
      >
        {currentField && (
          <>
            <Form layout="inline" form={optionForm} style={{ marginBottom: 16 }}>
              <Form.Item
                name="value"
                rules={[{ required: true, message: "請輸入選項代碼" }]}
              >
                <Input placeholder="選項代碼" />
              </Form.Item>
              <Form.Item
                name="display_value"
                rules={[{ required: true, message: "請輸入顯示名稱" }]}
              >
                <Input placeholder="顯示名稱" />
              </Form.Item>
              <Form.Item name="order_index">
                <Input type="number" placeholder="排序" style={{ width: 100 }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" onClick={handleAddOption}>
                  新增選項
                </Button>
              </Form.Item>
            </Form>

            <Table
              dataSource={fields.find((f) => f.id === currentField.id)?.options ?? []}
              rowKey="id"
              pagination={false}
              columns={[
                { title: "選項代碼", dataIndex: "value", key: "value" },
                { title: "顯示名稱", dataIndex: "display_value", key: "display_value" },
                { title: "排序", dataIndex: "order_index", key: "order_index", width: 80 },
                {
                  title: "狀態",
                  dataIndex: "is_active",
                  key: "is_active",
                  render: (value) => (value ? <Tag color="blue">啟用</Tag> : <Tag>停用</Tag>),
                },
                {
                  title: "操作",
                  key: "option_actions",
                  render: (_, option) => (
                    <Space>
                      <Button size="small" onClick={() => openEditOption(option)}>
                        編輯
                      </Button>
                      <Button size="small" onClick={() => handleToggleOption(option)}>
                        {option.is_active ? "停用" : "啟用"}
                      </Button>
                      <Button size="small" danger onClick={() => handleDeleteOption(option)}>
                        刪除
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          </>
        )}
      </Modal>

      <Modal
        title="編輯選項"
        open={Boolean(editingOption)}
        onCancel={() => setEditingOption(null)}
        onOk={handleSubmitOptionEdit}
        destroyOnHidden
      >
        <Form layout="vertical" form={optionEditForm}>
          <Form.Item
            name="display_value"
            label="顯示名稱"
            rules={[{ required: true, message: "請輸入顯示名稱" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="order_index" label="排序">
            <Input type="number" placeholder="0" />
          </Form.Item>
          <Form.Item name="is_active" label="是否啟用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </AppLayout>
  );
};

export default AdminPage;

