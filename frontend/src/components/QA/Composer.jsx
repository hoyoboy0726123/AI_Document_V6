import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Segmented, Tag, Tooltip } from "antd";
import { SendOutlined, StopOutlined, SettingOutlined } from "@ant-design/icons";
import apiClient from "../../services/api";

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
  onModeChange,
  onSubmit,
  onStop,
  onOpenSettings,
  scopeLabel,
  hasHistory,
  historyForCheck,
}) {
  const [value, setValue] = useState("");
  const [check, setCheck] = useState(null);   // { is_followup, inherited, reason }
  const [overridden, setOverridden] = useState(false); // 使用者按掉了標籤
  const timerRef = useRef(null);
  const seqRef = useRef(0);

  // 打字時去抖動查詢判斷結果。300ms 是「停下來想一下」的長度，
  // 比這短會在連續輸入時打出一堆無用請求。
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    const text = value.trim();
    if (!text || !hasHistory) { setCheck(null); return; }
    timerRef.current = setTimeout(async () => {
      const seq = ++seqRef.current;
      try {
        const res = await apiClient.post("rag/followup-check", {
          question: text,
          conversation_history: historyForCheck,
        });
        // 慢回的舊請求不可以蓋掉新結果
        if (seq === seqRef.current) setCheck(res.data);
      } catch { /* 判斷失敗就不顯示標籤，不影響送出 */ }
    }, 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [value, hasHistory, historyForCheck]);

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || loading) return;
    // 實際是否延續：以標籤當下的狀態為準（使用者按掉就是不延續）
    const isFollowup = Boolean(check?.is_followup) && !overridden;
    onSubmit(text, { isFollowup });
    setValue("");
    setCheck(null);
    setOverridden(false);
  }, [value, loading, check, overridden, onSubmit]);

  const showChip = check?.is_followup && !overridden;

  return (
    <div className="composer">
      <div className="composer-ctx">
        <Segmented
          size="small"
          value={qaMode}
          onChange={onModeChange}
          options={[
            { label: "純RAG", value: "rag" },
            { label: "混合", value: "hybrid" },
            { label: "Agent", value: "agent" },
          ]}
        />
        {scopeLabel && (
          <Tag className="composer-tag" color="blue" bordered={false}>
            範圍：{scopeLabel}
          </Tag>
        )}
        {showChip && (
          <Tooltip title={`${check.reason}。按 ✕ 改為獨立的新問題。`}>
            <Tag
              className="composer-tag"
              color="orange"
              bordered={false}
              closable
              onClose={(e) => { e.preventDefault(); setOverridden(true); }}
            >
              延續：{check.inherited}
            </Tag>
          </Tooltip>
        )}
        {overridden && (
          <Tag
            className="composer-tag"
            bordered={false}
            onClick={() => setOverridden(false)}
            style={{ cursor: "pointer" }}
          >
            已改為新問題（點此還原）
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
      <div className="composer-hint">Enter 送出 · Shift+Enter 換行</div>
    </div>
  );
});

export default Composer;
