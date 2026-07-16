import { useMemo } from "react";
import { Layout, Typography, Button } from "antd";
import { useLocation } from "react-router-dom";
import useAuthStore from "../../stores/authStore";

const { Header } = Layout;
const { Title, Text } = Typography;

const AppHeader = () => {
  // Audit L：細粒度 selector，避免 token refresh 觸發 header re-render
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const location = useLocation();

  const pageTitle = useMemo(() => {
    const path = location.pathname;
    if (path.startsWith("/documents/new")) return "建立文件";
    if (path.match(/\/documents\/[^/]+\/edit/)) return "編輯文件";
    if (path.match(/\/documents\/[^/]+/)) return "文件詳情";
    if (path.startsWith("/documents")) return "文件列表";
    if (path.startsWith("/qa")) return "RAG 智慧問答";
    if (path.startsWith("/admin/vector-health")) return "向量庫健康";
    if (path.startsWith("/admin/vector-search")) return "向量查詢測試";
    if (path.startsWith("/admin")) return "管理介面";
    return "";
  }, [location.pathname]);

  const handleLogout = () => {
    logout();
    window.location.href = "/login";
  };

  return (
    <Header
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        background: "#fff",
        padding: "0 24px",
        borderBottom: "1px solid #eee",
      }}
    >
      <Title level={4} style={{ margin: 0 }}>
        {pageTitle}
      </Title>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <Text strong>{user?.username}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {user?.role}
          </Text>
        </div>
        <Button onClick={handleLogout}>登出</Button>
      </div>
    </Header>
  );
};

export default AppHeader;
