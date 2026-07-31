import { memo, useCallback, useState } from "react";
import { Button, Input, Space } from "antd";
import { QuestionCircleOutlined, SendOutlined, StopOutlined } from "@ant-design/icons";

/**
 * 追問輸入列。
 *
 * 自己持有輸入中的文字，只在送出時才把值交給上層 —— 這是刻意的：
 * 原本 followupQuestion 這個 state 住在 QAConsolePage，每打一個字就讓整頁重渲染，
 * 包含對話記錄裡每一則的 ReactMarkdown、來源片段與 Agent 步驟。累積到 100 則
 * 對話後，打字會明顯延遲。輸入狀態留在這個元件內，上層就不會因為打字而重繪。
 */
const FollowupInput = memo(function FollowupInput({ loading, onSubmit, onStop }) {
  const [value, setValue] = useState("");

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text) return;
    setValue("");
    onSubmit(text);
  }, [value, onSubmit]);

  return (
    <div style={{ marginTop: 16, borderTop: "2px solid #e8e8e8", paddingTop: 12 }}>
      <div style={{ marginBottom: 8, fontSize: 13, color: "#52c41a" }}>
        <QuestionCircleOutlined style={{ marginRight: 6 }} />
        追問輸入列 - AI 將延續上下文並優化您的提示
      </div>
      <Space.Compact style={{ width: "100%" }}>
        <Input.TextArea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="例如：想知道更多？更詳細說明？"
          rows={1}
          autoSize={{ minRows: 1, maxRows: 4 }}
          onPressEnter={(e) => {
            if (e.ctrlKey || e.metaKey) {
              e.preventDefault();
              submit();
            }
          }}
          disabled={loading}
        />
        <Button type="primary" icon={<SendOutlined />} onClick={submit} loading={loading}>
          追問
        </Button>
        {loading && <Button danger icon={<StopOutlined />} onClick={onStop}>停止</Button>}
      </Space.Compact>
    </div>
  );
});

export default FollowupInput;
