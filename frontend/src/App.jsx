import { lazy, Suspense } from "react";
import { BrowserRouter as Router, Routes, Route, Navigate } from "react-router-dom";
import { ConfigProvider, App as AntdApp, Spin } from "antd";
import zhTW from "antd/locale/zh_TW";

import PrivateRoute from "./components/PrivateRoute";
import { TaskStatusProvider } from "./contexts/TaskStatusContext";
import TaskProgressBanner from "./components/TaskProgressBanner";
import ErrorBoundary from "./components/ErrorBoundary";

// Audit L：路由層 code-splitting —— react-pdf / react-force-graph 等重依賴
// 不再全部打進首屏 bundle，各頁面按需載入。
const LoginPage = lazy(() => import("./pages/LoginPage"));
const RegisterPage = lazy(() => import("./pages/RegisterPage"));
const DocumentsPage = lazy(() => import("./pages/DocumentsPage"));
const DocumentCreatePage = lazy(() => import("./pages/DocumentCreatePage"));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage"));
const DocumentEditPage = lazy(() => import("./pages/DocumentEditPage"));
const QAConsolePage = lazy(() => import("./pages/QAConsolePage"));
const AdminPage = lazy(() => import("./pages/AdminPage"));
const VectorSearchTestPage = lazy(() => import("./pages/VectorSearchTestPage"));
const VectorHealthPage = lazy(() => import("./pages/VectorHealthPage"));
const NotebookPage = lazy(() => import("./pages/NotebookPage"));
const KnowledgeGraphPage = lazy(() => import("./pages/KnowledgeGraphPage"));

const PageFallback = (
  <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "60vh" }}>
    <Spin size="large" />
  </div>
);

function App() {
  return (
    <ConfigProvider locale={zhTW}>
      <AntdApp>
        <TaskStatusProvider>
        <Router>
          <ErrorBoundary>
          <Suspense fallback={PageFallback}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />

            <Route path="/" element={<Navigate to="/documents" replace />} />

            <Route
              path="/documents"
              element={( 
                <PrivateRoute>
                  <DocumentsPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/documents/new"
              element={( 
                <PrivateRoute>
                  <DocumentCreatePage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/documents/:id"
              element={( 
                <PrivateRoute>
                  <DocumentDetailPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/documents/:id/edit"
              element={( 
                <PrivateRoute>
                  <DocumentEditPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/qa"
              element={( 
                <PrivateRoute>
                  <QAConsolePage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/notebook"
              element={(
                <PrivateRoute>
                  <NotebookPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/knowledge-graph"
              element={(
                <PrivateRoute>
                  <KnowledgeGraphPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/admin/metadata"
              element={(
                <PrivateRoute adminOnly>
                  <AdminPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/admin/vector-search"
              element={(
                <PrivateRoute adminOnly>
                  <VectorSearchTestPage />
                </PrivateRoute>
              )}
            />
            <Route
              path="/admin/vector-health"
              element={(
                <PrivateRoute adminOnly>
                  <VectorHealthPage />
                </PrivateRoute>
              )}
            />
          </Routes>
          </Suspense>
          <TaskProgressBanner />
          </ErrorBoundary>
        </Router>
        </TaskStatusProvider>
      </AntdApp>
    </ConfigProvider>
  );
}

export default App;
