import { useMemo } from "react";
import { Layout, Menu } from "antd";
import { FileTextOutlined, AppstoreOutlined, BookOutlined, SettingOutlined, RobotOutlined, ThunderboltOutlined, HeartOutlined, NodeIndexOutlined } from "@ant-design/icons";
import { useLocation, useNavigate } from "react-router-dom";
import FolderTree from "../Folders/FolderTree";
import useAuthStore from "../../stores/authStore";

const { Sider } = Layout;

const AppSidebar = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const { user } = useAuthStore();
  const isAdmin = user?.role === "admin";

  const menuItems = [
    { key: "/documents", icon: <FileTextOutlined />, label: "文件列表" },
    ...(isAdmin ? [{ key: "/documents/new", icon: <AppstoreOutlined />, label: "建立文件" }] : []),
    { key: "/qa", icon: <RobotOutlined />, label: "RAG智慧問答" },
    { key: "/knowledge-graph", icon: <NodeIndexOutlined />, label: "知識圖譜" },
    { key: "/notebook", icon: <BookOutlined />, label: "我的筆記本" },
    ...(isAdmin ? [
      { key: "/admin/metadata", icon: <SettingOutlined />, label: "管理介面" },
      { key: "/admin/vector-search", icon: <ThunderboltOutlined />, label: "向量查詢測試" },
      { key: "/admin/vector-health", icon: <HeartOutlined />, label: "向量庫健康" },
    ] : []),
  ];

  const selectedKey = useMemo(() => {
    if (location.pathname.startsWith("/documents/new")) return "/documents/new";
    if (location.pathname.startsWith("/documents")) return "/documents";
    if (location.pathname.startsWith("/qa")) return "/qa";
    if (location.pathname.startsWith("/knowledge-graph")) return "/knowledge-graph";
    if (location.pathname.startsWith("/notebook")) return "/notebook";
    if (location.pathname.startsWith("/admin/vector-health")) return "/admin/vector-health";
    if (location.pathname.startsWith("/admin/vector-search")) return "/admin/vector-search";
    if (location.pathname.startsWith("/admin")) return "/admin/metadata";
    return "/documents";
  }, [location.pathname]);

  const isDocuments =
    location.pathname.startsWith("/documents") &&
    !location.pathname.startsWith("/documents/new");

  return (
    <Sider
      width={220}
      theme="dark"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        overflow: "hidden",
        position: "sticky",
        top: 0,
      }}
    >
      {/* 品牌標題 — 固定在頂部 */}
      <div
        style={{
          padding: "20px 16px 16px",
          color: "#fff",
          fontSize: 22,
          fontWeight: 800,
          letterSpacing: 0.5,
          lineHeight: 1.3,
          borderBottom: "1px solid rgba(255,255,255,0.1)",
          flexShrink: 0,
        }}
      >
        智慧文件管理系統
      </div>

      {/* 導航選單 — 固定高度 */}
      <div style={{ flexShrink: 0 }}>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ marginTop: 4 }}
        />
      </div>

      {/* 資料夾樹 — 限高可捲動區域（約10個資料夾高），版本號固定在其下方 */}
      {isDocuments && (
        <div
          style={{
            maxHeight: "calc(10 * 32px)",
            overflowY: "auto",
            overflowX: "hidden",
            flexShrink: 0,
          }}
        >
          <FolderTree />
        </div>
      )}

      {/* 彈性空白，讓版本號沉到底部 */}
      <div style={{ flex: 1 }} />

      {/* 版本號與授權 — 永遠固定在最底部。
          原始碼連結不是裝飾：本專案相依 PyMuPDF（AGPL 3.0），而 AGPL 第 13 條
          要求「透過網路使用本服務的人」必須能取得完整原始碼。同事在區網連進來
          用就屬於這個情形，所以介面上必須有一個明顯可見的取得管道。 */}
      <div
        style={{
          padding: "10px 16px",
          color: "rgba(255,255,255,0.3)",
          fontSize: 12,
          borderTop: "1px solid rgba(255,255,255,0.08)",
          flexShrink: 0,
          letterSpacing: 0.5,
          lineHeight: 1.7,
        }}
      >
        <div>版本號：V6</div>
        <div>
          AGPL-3.0 ·{" "}
          <a
            href="https://github.com/hoyoboy0726123/AI_Document_V6"
            target="_blank"
            rel="noreferrer"
            style={{ color: "rgba(255,255,255,0.55)" }}
          >
            原始碼
          </a>
        </div>
      </div>
    </Sider>
  );
};

export default AppSidebar;
