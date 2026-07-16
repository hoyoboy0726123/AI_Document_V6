import { useEffect, useState } from 'react';
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  message,
} from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, ProjectOutlined } from '@ant-design/icons';
import apiClient from '../../services/api';

const ProjectManagement = () => {
  const [projects, setProjects] = useState([]);
  const [fieldId, setFieldId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [modalMode, setModalMode] = useState('create');
  const [currentProject, setCurrentProject] = useState(null);
  const [form] = Form.useForm();

  const ensureField = async () => {
    try {
      const resp = await apiClient.get('admin/metadata-fields');
      const fields = resp.data;
      const existing = fields.find((f) => f.name === 'project_id');
      if (existing) {
        setFieldId(existing.id);
        setProjects(existing.options ?? []);
      } else {
        const createResp = await apiClient.post('admin/metadata-fields', {
          name: 'project_id',
          display_name: '所屬專案',
          field_type: 'select',
          is_required: false,
          is_active: true,
        });
        setFieldId(createResp.data.id);
        setProjects([]);
      }
    } catch (error) {
      message.error(error.response?.data?.detail ?? '載入專案欄位失敗');
    }
  };

  const fetchProjects = async () => {
    try {
      setLoading(true);
      const resp = await apiClient.get('admin/metadata-fields');
      const fields = resp.data;
      const field = fields.find((f) => f.name === 'project_id');
      if (field) {
        setFieldId(field.id);
        setProjects(field.options ?? []);
      }
    } catch (error) {
      message.error(error.response?.data?.detail ?? '載入專案列表失敗');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await ensureField();
      setLoading(false);
    };
    init();
  }, []);

  const openCreateModal = () => {
    setModalMode('create');
    setCurrentProject(null);
    form.resetFields();
    setModalVisible(true);
  };

  const openEditModal = (project) => {
    setModalMode('edit');
    setCurrentProject(project);
    form.setFieldsValue({
      display_name: project.display_value,
      value: project.value,
      description: project.description ?? '',
    });
    setModalVisible(true);
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (modalMode === 'create') {
        if (!fieldId) {
          message.error('無法取得專案欄位 ID，請重新整理頁面');
          return;
        }
        await apiClient.post(`admin/metadata-fields/${fieldId}/options`, {
          display_value: values.display_name,
          value: values.value,
          order_index: 0,
        });
        message.success('專案已建立');
      } else if (currentProject) {
        await apiClient.put(`admin/metadata-fields/options/${currentProject.id}`, {
          display_value: values.display_name,
          order_index: currentProject.order_index ?? 0,
        });
        message.success('專案已更新');
      }
      setModalVisible(false);
      fetchProjects();
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error.response?.data?.detail ?? '儲存專案失敗');
    }
  };

  const handleDelete = (project) => {
    Modal.confirm({
      title: `刪除專案「${project.display_value}」`,
      content: '刪除後將無法復原，確定要繼續嗎？',
      okText: '刪除',
      okType: 'danger',
      cancelText: '取消',
      async onOk() {
        try {
          await apiClient.delete(`admin/metadata-fields/options/${project.id}`);
          message.success('專案已刪除');
          fetchProjects();
        } catch (error) {
          message.error(error.response?.data?.detail ?? '刪除專案失敗');
        }
      },
    });
  };

  const handleToggleActive = async (project) => {
    try {
      await apiClient.put(`admin/metadata-fields/options/${project.id}`, {
        is_active: !project.is_active,
      });
      message.success(project.is_active ? '專案已停用' : '專案已啟用');
      fetchProjects();
    } catch (error) {
      message.error(error.response?.data?.detail ?? '更新專案狀態失敗');
    }
  };

  const columns = [
    {
      title: '顯示名稱',
      dataIndex: 'display_value',
      key: 'display_value',
      render: (text) => <strong>{text}</strong>,
    },
    {
      title: '值/代碼',
      dataIndex: 'value',
      key: 'value',
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
      render: (text) => (text ? new Date(text).toLocaleString('zh-TW') : '-'),
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
      title="專案管理"
      extra={
        <Button type="primary" icon={<ProjectOutlined />} onClick={openCreateModal}>
          新增專案
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={loading}
        dataSource={projects}
        columns={columns}
        pagination={{ pageSize: 10 }}
      />

      <Modal
        title={modalMode === 'create' ? '新增專案' : '編輯專案'}
        open={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={handleSubmit}
        destroyOnHidden
        width={600}
      >
        <Form layout="vertical" form={form}>
          <Form.Item
            name="display_name"
            label="專案名稱"
            rules={[{ required: true, message: '請輸入專案名稱' }]}
          >
            <Input placeholder="例如：AI 研究專案、產品開發" />
          </Form.Item>

          <Form.Item
            name="value"
            label="代碼/英文ID"
            rules={[{ required: true, message: '請輸入代碼' }]}
            tooltip={modalMode === 'edit' ? '代碼建立後無法修改' : '建立後無法修改，請謹慎填寫'}
          >
            <Input
              placeholder="project_alpha"
              disabled={modalMode === 'edit'}
            />
          </Form.Item>

          <Form.Item name="description" label="說明" tooltip="選填，說明此專案的用途">
            <Input.TextArea rows={3} placeholder="說明此專案的背景與目標" />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
};

export default ProjectManagement;
