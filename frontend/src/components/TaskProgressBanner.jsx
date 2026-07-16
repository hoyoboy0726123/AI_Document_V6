/**
 * 右下角浮動顯示背景任務進度的 Banner
 */
import { Button, Card, Progress, Space, Tag, Tooltip, Typography } from "antd";
import { CheckCircleOutlined, CloseOutlined, LoadingOutlined, WarningOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { useTaskStatus } from "../contexts/TaskStatusContext";
import useAuthStore from "../stores/authStore";

const statusIcon = (status) => {
  if (status === "completed") return <CheckCircleOutlined style={{ color: "#52c41a" }} />;
  if (status === "failed") return <WarningOutlined style={{ color: "#ff4d4f" }} />;
  return <LoadingOutlined style={{ color: "#1677ff" }} spin />;
};

const statusColor = (status) => {
  if (status === "completed") return "success";
  if (status === "failed") return "error";
  return "processing";
};

const TaskProgressBanner = () => {
  const navigate = useNavigate();
  const { tasks, taskStatuses, removeTask } = useTaskStatus();
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  // 未登入時不顯示 banner —— 登入頁不該被殘留任務卡住
  if (!isAuthenticated) return null;
  if (!tasks.length) return null;

  return (
    <div style={{
      position: "fixed",
      bottom: 24,
      right: 24,
      zIndex: 1000,
      display: "flex",
      flexDirection: "column",
      gap: 8,
      maxWidth: 340,
    }}>
      {tasks.map((entry) => {
        const st = taskStatuses[entry.task_id];
        const taskStatus = st?.status ?? "pending";
        const progress = st?.progress ?? 0;
        const message = st?.message ?? "等待開始...";
        const isDone = taskStatus === "completed" || taskStatus === "failed";

        return (
          <Card
            key={entry.task_id}
            size="small"
            style={{ boxShadow: "0 4px 12px rgba(0,0,0,0.15)", borderRadius: 8 }}
            styles={{ body: { padding: "10px 14px" } }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 6 }}>
              <Space size={6}>
                {statusIcon(taskStatus)}
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {st?.task_type === "vl_vectorize" ? "VL 視覺解析"
                   : st?.task_type === "pdf_analyze" ? "PDF 文件分析"
                   : "向量索引建立"}
                </Typography.Text>
                <Tag color={statusColor(taskStatus)} style={{ fontSize: 11 }}>
                  {taskStatus === "pending" ? "等待中" :
                   taskStatus === "running" ? "進行中" :
                   taskStatus === "completed" ? "完成" : "失敗"}
                </Tag>
              </Space>
              <Tooltip
                title={isDone ? "關閉提示" : "關閉提示框（背景任務仍會在伺服器跑完，不會中止）"}
              >
                <Button
                  type="text"
                  size="small"
                  icon={<CloseOutlined />}
                  onClick={() => removeTask(entry.task_id)}
                  style={{ padding: 0, height: 20 }}
                />
              </Tooltip>
            </div>

            {entry.document_title && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                ellipsis
              >
                {entry.document_title}
              </Typography.Text>
            )}

            <Typography.Text style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
              {taskStatus === "failed" && st?.error ? st.error : message}
            </Typography.Text>

            {!isDone && (
              <Progress
                percent={progress}
                size="small"
                status="active"
                style={{ marginBottom: 4 }}
              />
            )}

            {isDone && entry.document_id && (
              <Button
                size="small"
                type="link"
                style={{ padding: 0, fontSize: 12 }}
                onClick={() => {
                  navigate(`/documents/${entry.document_id}`);
                  if (taskStatus === "completed") removeTask(entry.task_id);
                }}
              >
                前往文件 →
              </Button>
            )}

            {isDone && !entry.document_id && st?.task_type === "pdf_analyze" && taskStatus === "completed" && st?.result && (
              <Button
                size="small"
                type="primary"
                style={{ padding: "0 8px", fontSize: 12, height: 24 }}
                onClick={() => {
                  removeTask(entry.task_id);
                  navigate("/documents/new", { state: { uploadResult: st.result } });
                }}
              >
                前往填寫表單 →
              </Button>
            )}
          </Card>
        );
      })}
    </div>
  );
};

export default TaskProgressBanner;
