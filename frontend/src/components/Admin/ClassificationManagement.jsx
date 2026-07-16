import { useEffect, useState } from 'react';
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from 'antd';
import { AppstoreOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import apiClient from '../../services/api';

const ClassificationManagement = () => {
  const [classifications, setClassifications] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [modalMode, setModalMode] = useState('create');
  const [currentClassification, setCurrentClassification] = useState(null);
  const [form] = Form.useForm();

  const fetchClassifications = async () => {
    try {
      setLoading(true);
      const resp = await apiClient.get('admin/classifications');
      setClassifications(resp.data);
    } catch (error) {
      message.error(error.response?.data?.detail ?? '載入分類失敗');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchClassifications();
  }, []);

  const openCreateModal = () => {
    setModalMode('create');
    setCurrentClassification(null);
    form.resetFields();
    setModalVisible(true);
  };

  const openEditModal = (classification) => {
    setModalMode('edit');
    setCurrentClassification(classification);
    form.setFieldsValue({
      name: classification.name,
      code: classification.code,
      description: classification.description,
      is_active: classification.is_active,
    });
    setModalVisible(true);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (modalMode === 'create') {
        await apiClient.post('admin/classifications/', {
          name: values.name,
          code: values.code || null,
          description: values.description || null,
        });
        message.success('分類已建立');
      } else if (currentClassification) {
        await apiClient.put(`admin/classifications/${currentClassification.id}/`, {
          name: values.name,
          code: values.code || null,
          description: values.description || null,
          is_active: values.is_active,
        });
        message.success('分類已更新');
      }
      setModalVisible(false);
      fetchClassifications();
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error.response?.data?.detail ?? '儲存分類失敗');
    }
  };

  const handleDelete = (classification) => {
    Modal.confirm({
      title: `刪除分類「${classification.name}」`,
      content: '刪除後將無法復原，確定要繼續嗎？',
      okText: '刪除',
      okType: 'danger',
      cancelText: '取消',
      async onOk() {
        try {
          await apiClient.delete(`admin/classifications/${classification.id}/`);
          message.success('分類已刪除');
          fetchClassifications();
        } catch (error) {
          message.error(error.response?.data?.detail ?? '刪除分類失敗');
        }
      },
    });
  };

  const handleToggleActive = async (classification) => {
    try {
      await apiClient.put(`admin/classifications/${classification.id}/`, {
        is_active: !classification.is_active,
      });
      message.success(classification.is_active ? '分類已停用' : '分類已啟用');
      fetchClassifications();
    } catch (error) {
      message.error(error.response?.data?.detail ?? '更新分類狀態失敗');
    }
  };

  const columns = [
    {
      title: '分類名稱',
      dataIndex: 'name',
      key: 'name',
      render: (text) => <strong>{text}</strong>,
    },
    {
      title: '分類代碼',
      dataIndex: 'code',
      key: 'code',
      render: (code) => code || '-',
    },
    {
      title: '說明',
      dataIndex: 'description',
      key: 'description',
      render: (text) => text || '-',
      ellipsis: true,
    },
    {
      title: '狀態',
      dataIndex: 'is_active',
      key: 'is_active',
      render: (value) =>
        value ? (
          <Tag color="green" icon={<CheckCircleOutlined />}>
            啟用中
          </Tag>
        ) : (
          <Tag icon={<CloseCircleOutlined />}>已停用</Tag>
        ),
    },
    {
      title: '建立時間',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (text) => new Date(text).toLocaleString('zh-TW'),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => openEditModal(record)}>
            編輯
          </Button>
          <Button
            size="small"
            type={record.is_active ? 'default' : 'primary'}
            onClick={() => handleToggleActive(record)}
          >
            {record.is_active ? '停用' : '啟用'}
          </Button>
          <Button size="small" danger onClick={() => handleDelete(record)}>
            刪除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card
      title="分類管理"
      extra={
        <Button type="primary" icon={<AppstoreOutlined />} onClick={openCreateModal}>
          新增分類
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={classifications}
        columns={columns}
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={modalMode === 'create' ? '新增分類' : '編輯分類'}
        open={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={handleSubmit}
        destroyOnHidden
        width={600}
      >
        <Form layout="vertical" form={form}>
          <Form.Item
            name="name"
            label="分類名稱"
            rules={[{ required: true, message: '請輸入分類名稱' }]}
          >
            <Input placeholder="例如：財務報告、人資文件" />
          </Form.Item>

          <Form.Item
            name="code"
            label="分類代碼"
            tooltip="選填，用於快速識別，例如 FIN、HR"
          >
            <Input placeholder="例如：FIN、HR、CONTRACT" maxLength={20} />
          </Form.Item>

          <Form.Item name="description" label="說明" tooltip="選填，說明此分類的用途">
            <Input.TextArea rows={3} placeholder="說明此分類包含哪些類型的文件" />
          </Form.Item>

          {modalMode === 'edit' && (
            <Form.Item
              name="is_active"
              label="狀態"
              valuePropName="checked"
              tooltip="停用後此分類將不會出現在新增文件的下拉選單中"
            >
              <Switch checkedChildren="啟用" unCheckedChildren="停用" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </Card>
  );
};

export default ClassificationManagement;
