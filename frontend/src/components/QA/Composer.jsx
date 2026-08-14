import { memo, useCallback, useState } from "react";
import { Button, Input, Tag, Tooltip } from "antd";
import { SendOutlined, StopOutlined, SettingOutlined } from "@ant-design/icons";

const { TextArea } = Input;

/**
 * 合併後的單一輸入框（V6）。
 *
 * 取代原本「左側＝新問題、右下＝追問」的兩個框。
 *
 * 有歷史時預設為「追問」：完整歷史交給後端，由 resolve_query 無條件改寫
 * （不再前端分類 —— 分類有門檻、門檻會錯）。但改寫無條件不代表永遠正確，
 * 所以狀態要顯示成一個可以按掉的標籤。
 *
 * 為什麼標籤一定要能按掉：任何自動判斷都會有漏網。實測「冷凝測試方法與條件」
 * 曾被誤判成追問而繼承上一輪的「濕度」去查。使用者要能在送出前就看到並否決，
 * 而不是送出後才發現查錯對象。按成「新問題」時不帶歷史送出，後端就不會改寫。
 *
 * 沒有歷史時（對話的第一題）不顯示標籤，也一律以 isFollowup=false 送出 ——
 * 先前寫死 true，導致新對話的第一題也被標成「追問」。
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
  // 追問 / 新問題的使用者覆寫。預設為追問（有歷史時），送出後復位 ——
  // 「這一題換主題」是單題決定，不該黏著到後續每一題（換了主題之後，
  // 新主題的後續提問本來就該是追問）。
  const [followup, setFollowup] = useState(true);
  const isFollowup = hasHistory && followup;

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || loading) return;
    onSubmit(text, hasHistory && followup);
    setValue("");
    setFollowup(true);
  }, [value, loading, onSubmit, hasHistory, followup]);

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
        {/* 追問／新問題的可覆寫標籤。
            改寫是無條件的，但無條件不等於永遠正確 —— 實測「冷凝測試方法與條件」
            曾被當成追問而繼承上一輪的「濕度」去查。使用者要能在「送出前」就看到
            現在是哪個狀態並改掉，而不是送出後才發現查錯對象。 */}
        {hasHistory && (
          <Tooltip
            title={
              isFollowup
                ? "目前會承接前文：系統會用上一題的主體補全這一題再去查。點一下改成「新問題」，原字送出、不加工。"
                : "目前不承接前文：原字送出、不做改寫。點一下改回「追問」。"
            }
          >
            <Tag
              className="composer-tag composer-state"
              color={isFollowup ? "orange" : "green"}
              bordered={false}
              onClick={() => setFollowup((v) => !v)}
            >
              {isFollowup ? "追問（承接前文）" : "新問題（不加工）"}
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
          ? (isFollowup
              ? " · 目前為追問，系統會用前文補上主體；點上方標籤可改成「新問題」原字送出"
              : " · 目前為新問題，原字送出不加工；點上方標籤可改回「追問」")
          : " · 這是本對話的第一題"}
      </div>
    </div>
  );
});

export default Composer;
