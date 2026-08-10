import { memo, useCallback, useState } from "react";
import { Button, Input, Tag, Tooltip } from "antd";
import { SendOutlined, StopOutlined, SettingOutlined } from "@ant-design/icons";

const { TextArea } = Input;

/**
 * 合併後的單一輸入框（V6）。
 *
 * 取代原本「左側＝新問題、右下＝追問」的兩個框。判斷交給後端的
 * /rag/followup-check（純規則、不呼叫 LLM），結果顯示成一個可以按掉的標籤。
 *
 * 為什麼標籤一定要能按掉：任何自動判斷都會有漏網。實測「冷凝測試方法與條件」
 * 曾被誤判成追問而繼承上一輪的「濕度」去查。使用者要能在送出前就看到並否決，
 * 而不是送出後才發現查錯對象。
 *
 * 自持輸入狀態（不把 value 提到父層）：串流生成時父層每個 token 都重繪，
 * 打字會嚴重延遲 —— 這是 V5 already 修過的問題，不要退回去。
 */
const Composer = memo(function Composer({
  loading,
  qaMode,
  onSubmit,
  onStop,
  onOpenSettings,
  scopeLabel,
  hasHistory,
}) {
  const [value, setValue] = useState("");
  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || loading) return;
    onSubmit(text);
    setValue("");
  }, [value, loading, onSubmit]);

  return (
    <div className="composer">
      <div className="composer-ctx">
        {/* 模式選擇移進設定抽屜。混合是預設且幾乎總是對的（路由是確定性規則，
            微秒級），三選一對使用者只是多餘的認知負擔。
            但「被鎖在非預設模式」必須看得見 —— 否則使用者會納悶為什麼每次都要
            十幾秒（Agent），卻找不到原因。預設模式安靜，非預設才出聲。 */}
        {qaMode !== "hybrid" && (
          <Tooltip title="已手動鎖定查詢模式。點一下開啟設定改回「混合（自動）」。">
            <Tag
              className="composer-tag composer-state"
              color="purple"
              bordered={false}
              onClick={onOpenSettings}
            >
              已鎖定：{qaMode === "agent" ? "Agent" : "純 RAG"}
            </Tag>
          </Tooltip>
        )}
        {scopeLabel && (
          <Tag className="composer-tag" color="blue" bordered={false}>
            範圍：{scopeLabel}
          </Tag>
        )}
        <span className="composer-spacer" />
        <Tooltip title="查詢設定">
          <Button size="small" type="text" icon={<SettingOutlined />} onClick={onOpenSettings} />
        </Tooltip>
      </div>

      <div className="composer-box">
        <TextArea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={hasHistory ? "接著問，或直接問新問題…" : "輸入問題…"}
          autoSize={{ minRows: 1, maxRows: 8 }}
          variant="borderless"
          onKeyDown={(e) => {
            // Enter 送出、Shift+Enter 換行。輸入法組字中（isComposing）不可送出，
            // 否則中文選字按 Enter 會直接把半成品送出去。
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {loading ? (
          <Button danger icon={<StopOutlined />} onClick={onStop} shape="circle" />
        ) : (
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={submit}
            disabled={!value.trim()}
            shape="circle"
          />
        )}
      </div>
      <div className="composer-hint">
        Enter 送出 · Shift+Enter 換行
        {hasHistory
          ? " · 承接前文時系統會自動補上主體，想重新開始請按「＋ 新對話」"
          : " · 這是本對話的第一題"}
      </div>
    </div>
  );
});

export default Composer;
