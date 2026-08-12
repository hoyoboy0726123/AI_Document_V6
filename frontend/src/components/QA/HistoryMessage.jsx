import { memo } from "react";
import { Alert, Button, Card, Divider, Space, Tag, Typography } from "antd";
import { SaveOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownComponents, renderAgentSteps, renderSources, renderThinking } from "./messageParts";

const { Text } = Typography;

/**
 * 對話記錄中「已完成」的一則訊息。
 *
 * memo 化的目的：答案串流生成時，streamingMsg 每收到一段文字就 setState 一次，
 * 上層每秒重渲染多次。若歷史訊息跟著重繪，累積上百則之後每次串流更新都要重跑
 * 上百次 ReactMarkdown ——「AI 正在回答時整頁很頓」就是這麼來的。
 *
 * 這裡的 props 必須全部保持穩定參考，memo 才有效：
 *   - msg 來自 conversationHistory 陣列，物件參考不變
 *   - 三個 callback 在上層以 useCallback([]) 包住（只呼叫 setState，無其他相依）
 *   - 渲染零件（renderSources 等）搬到模組層級，不再每次重建
 * expandedSnippets 是唯一會變的物件，但只在使用者手動展開來源時改變，
 * 不會在串流期間變動。
 */
const HistoryMessage = memo(function HistoryMessage({
  msg, index, expandedSnippets, onToggleSnippet, onPreviewPdf, onSaveNote, showDivider, onRequery,
}) {
  return (
    <div style={{ marginBottom: 32 }}>
      <div style={{ marginBottom: 16 }}>
        <Tag color="blue" style={{ fontSize: 14, padding: "4px 12px" }}>問題 {index + 1}</Tag>
        {msg.is_followup && <Tag color="orange" style={{ marginLeft: 8 }}>追問</Tag>}
        {msg.hybrid && msg.routedMode && (
          <Tag color={msg.routedMode === "agent" ? "geekblue" : "green"} style={{ marginLeft: 8 }}>
            混合→{msg.routedMode === "agent" ? "關係查詢(Agent)" : "內容查詢(RAG)"}
          </Tag>
        )}
        <Text strong style={{ fontSize: 16, marginLeft: 8 }}>{msg.question}</Text>
        {msg.optimized_query && msg.optimized_query !== msg.question && (
          <div style={{ marginTop: 8, paddingLeft: 12, borderLeft: "3px solid #1890ff" }}>
            <Text type="secondary" style={{ fontSize: 13 }}>AI 理解：{msg.optimized_query}</Text>
            {/* 改寫是在送出後才知道結果的（LLM 要 1-8 秒，沒辦法邊打字邊預覽），
                所以控制權放在事後：看到改寫不對，一鍵用原問句重查。
                比「送出前猜」準確得多，因為此時已經看得到實際查了什麼。 */}
            {onRequery && (
              <Button
                type="link"
                size="small"
                style={{ padding: "0 6px", fontSize: 12 }}
                onClick={() => onRequery(msg.question)}
              >
                改用原問句重查
              </Button>
            )}
          </div>
        )}
      </div>
      <Card
        size="small"
        style={{
          background: "#f9f9f9",
          borderLeft: msg.agentMode
            ? "4px solid #1677ff"
            : (msg.used_ai_fallback ? "4px solid #faad14" : "4px solid #52c41a"),
        }}
      >
        {msg.used_ai_fallback && (
          <Alert
            message="AI 一般知識回答"
            description="此答案由 AI 一般知識庫產生，可能不來自系統文件內容"
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
          />
        )}
        {msg.agentMode && renderAgentSteps(msg.agentSteps, false)}
        {!msg.agentMode && renderThinking(msg.thinking, false, true)}
        <div style={{ fontSize: 15, lineHeight: 1.8 }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {msg.answer}
          </ReactMarkdown>
        </div>
        {renderSources(msg.sources, index, expandedSnippets, onToggleSnippet, onPreviewPdf)}
        {msg.sources?.length > 0 && (
          <div style={{ marginTop: 12, textAlign: "right" }}>
            <Space>
              <Button size="small" icon={<SaveOutlined />} onClick={() => onSaveNote(msg)}>
                儲存筆記
              </Button>
            </Space>
          </div>
        )}
      </Card>
      {showDivider && <Divider />}
    </div>
  );
});

export default HistoryMessage;
