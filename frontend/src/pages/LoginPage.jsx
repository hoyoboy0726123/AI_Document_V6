import { useState } from 'react';
import { Form, Input, Button, Card, message, Typography } from 'antd';
import { Link, useNavigate } from 'react-router-dom';

import apiClient from '../services/api';
import useAuthStore from '../stores/authStore';

const LoginPage = () => {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { login, logout } = useAuthStore();

  const onFinish = async ({ username, password }) => {
    setLoading(true);
    try {
      // Audit H10：登入前先清掉上一位使用者的殘留身分/token，
      // 避免攔截器或殘留狀態把新登入綁到舊帳號。
      logout();
      const formData = new URLSearchParams();
      formData.append('username', username);
      formData.append('password', password);
      formData.append('scope', '');
      formData.append('client_id', '');
      formData.append('client_secret', '');

      const { data } = await apiClient.post('/auth/login', formData, {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      });

      const {access_token, refresh_token, expires_in } = data;

      // Fetch user info
      const userResponse = await apiClient.get('/auth/me', {
        headers: { Authorization: `Bearer ${access_token}` },
      });

      // Save tokens and user info (updated to include refresh_token and expires_in)
      login(access_token, refresh_token, expires_in, userResponse.data);

      message.success('登入成功，7 天內免登入！');
      navigate('/');
    } catch (error) {
      const errMsg = error.response?.data?.detail ?? '登入失敗，請確認帳號密碼。';
      message.error(errMsg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>
      <Card title="登入系統" style={{ width: 360 }}>
        <Form name="login" layout="vertical" onFinish={onFinish}>
          <Form.Item
            label="使用者名稱"
            name="username"
            rules={[{ required: true, message: '請輸入使用者名稱!' }]}
          >
            <Input />
          </Form.Item>

          <Form.Item
            label="密碼"
            name="password"
            rules={[{ required: true, message: '請輸入密碼!' }]}
          >
            <Input.Password />
          </Form.Item>

          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登入
            </Button>
          </Form.Item>
        </Form>
        <Typography.Paragraph style={{ marginTop: 16, textAlign: 'center' }}>
          還沒有帳號？ <Link to="/register">立即註冊</Link>
        </Typography.Paragraph>
      </Card>
    </div>
  );
};

export default LoginPage;
